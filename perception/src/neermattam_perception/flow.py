"""Flow speed from a short video: find floating debris, track it, scale it.

1. Muse Spark Open boxes up to eight floating objects, and one fixed anchor
   off the water, on the video's key frame (``muse_open.DEBRIS_PROMPT``).
2. SAM 2.1's video predictor follows every box forward from the key frame
   (``Sam21LocalAdapter.track_boxes``), memory carried from frame to frame.
3. Each track is checked before it is used: a track lost on most frames, or
   whose mask changes size wildly (it slid onto a wall, a wave or a
   neighbour), is set aside with the reason; single frames with an out of
   range mask are dropped.
4. When the anchor was tracked cleanly, its motion is subtracted from every
   track, which removes camera pans and shake (``velocity.track_velocity``).
5. ``velocity.surface_velocity`` turns the tracks into a surface speed with a
   sigma, using the scale of the reference measured on the key frame: the
   wheel's ellipse, or a person's head-to-shoulder length.

Limits, stated plainly: only motion across the image is seen; the scale is
right at the reference's distance and wrong for debris much nearer or
farther; the anchor removes image shifts, not changes of zoom or a camera
that walks along the street.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

import cv2
import numpy as np
from numpy.typing import NDArray

from .adapters import ModelError
from .agent import TraceStep
from .geometry import DepthMeasurement
from .media import ImageInput, VideoInput
from .muse_open import DEBRIS_VERSION, JsonReply, box_to_px, grid_box, grid_point, to_px
from .person import HEAD_TO_SHOULDER, HEAD_TO_SHOULDER_SD, POSTURE_REL_SD, PersonMeasurement
from .references import ReferenceChoice
from .sam_local import BoxTracks, ObjectTrack
from .velocity import (
    Track,
    TrackVelocity,
    VelocityEstimate,
    VelocityFailure,
    pixel_speed,
    surface_velocity,
)

MAX_DEBRIS = 8
# A floating object larger than this share of the frame's longer side is not
# debris (a car, a patch of water); smaller than MIN_SIDE_PX cannot be tracked.
MAX_DEBRIS_SIDE = 0.2
MIN_SIDE_PX = 3
# A box must hold an object distinct from the water: at least MIN_DISTINCT_PX
# connected pixels more than DISTINCT_SIGMA robust standard deviations from the
# median grey level around it. Muse Spark Open's boxes on small debris were often
# a few pixels off the object, on plain water (eval/RESULTS_VIDEO.md), so each box
# is snapped to the nearest such object within one box size, or dropped.
DISTINCT_SIGMA = 4.0
MIN_DISTINCT_PX = 4
# The tracking window: frames are sampled at about TARGET_FPS for WINDOW_S
# seconds from the key frame.
TARGET_FPS = 10.0
WINDOW_S = 4.0
# Track checks.
MAX_LOST_SHARE = 0.5
AREA_RANGE = 3.0  # a frame's mask area outside [median / 3, median * 3] is dropped
MAX_BAD_AREA_SHARE = 0.3
# The anchor is used for camera motion only when it was found on nearly every frame.
ANCHOR_MIN_SHARE = 0.9
ANCHOR_ID = "anchor"


class DebrisLocator(Protocol):
    async def locate_debris_json(self, image: ImageInput) -> JsonReply: ...


class BoxTracker(Protocol):
    @property
    def mask_model_name(self) -> str: ...

    async def track_boxes(
        self,
        video: VideoInput,
        start: int,
        boxes: Any,
        *,
        points: Any = None,
        stride: int = 1,
        max_frames: int = 150,
    ) -> BoxTracks: ...


@dataclass(frozen=True, slots=True)
class Prompted:
    box: tuple[int, int, int, int]  # key-frame pixels, right and bottom half-open
    point: tuple[int, int] | None
    what: str


@dataclass(frozen=True, slots=True)
class DebrisReading:
    debris: tuple[Prompted, ...]
    anchor: Prompted | None
    flow: str
    dropped: tuple[str, ...] = ()


def _box_iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _prompted(item: Any, w: int, h: int, label: str) -> tuple[Prompted | None, str | None]:
    if not isinstance(item, dict):
        return None, f"{label}: not an object"
    gb = grid_box(item.get("box"))
    if gb is None:
        return None, f"{label}: no valid box"
    box = box_to_px(gb, w, h)
    side = max(box[2] - box[0], box[3] - box[1])
    if side > MAX_DEBRIS_SIDE * max(w, h):
        return None, f"{label}: box too large to be one floating object"
    if side < MIN_SIDE_PX:
        return None, f"{label}: box too small to track"
    gp = grid_point(item.get("point"))
    point = to_px(gp, w, h) if gp is not None else None
    if point is not None and not (box[0] <= point[0] < box[2] and box[1] <= point[1] < box[3]):
        point = None  # a point outside its own box is not used, the box still is
    what = str(item.get("what") or "").strip()[:40] or "object"
    return Prompted(box, point, what), None


def parse_debris(data: dict[str, Any], w: int, h: int) -> DebrisReading:
    """The locator's answer, checked: bad, oversized or duplicate boxes are dropped and named."""
    dropped: list[str] = []
    debris: list[Prompted] = []
    raw = data.get("debris")
    if not isinstance(raw, list):
        dropped.append("debris: not a list")
        raw = []
    for i, item in enumerate(raw):
        p, why = _prompted(item, w, h, f"debris {i + 1}")
        if p is None:
            dropped.append(str(why))
            continue
        if any(_box_iou(p.box, q.box) > 0.5 for q in debris):
            dropped.append(f"debris {i + 1}: duplicate of an earlier box")
            continue
        if len(debris) >= MAX_DEBRIS:
            dropped.append(f"debris {i + 1}: more than {MAX_DEBRIS}")
            continue
        debris.append(p)
    anchor = None
    if data.get("anchor") is not None:
        anchor, why = _prompted(data.get("anchor"), w, h, "anchor")
        if anchor is None:
            dropped.append(str(why))
    flow = str(data.get("flow") or "").strip()[:200]
    return DebrisReading(tuple(debris), anchor, flow, tuple(dropped))


def snap_to_object(grey: NDArray[np.uint8], p: Prompted) -> tuple[Prompted | None, str | None]:
    """The box moved onto the nearest distinct small object, or None if there is none.

    The search window is the box plus its own size on every side. A pixel is
    distinct when it is more than DISTINCT_SIGMA robust sigmas (1.4826 times the
    median absolute deviation, at least 2 grey levels) from the window's median;
    distinct pixels are grouped 8-connected, and groups smaller than
    MIN_DISTINCT_PX or larger than a quarter of the window are not objects. The
    note says what was done (None when the box already sat on the object).
    """
    h, w = grey.shape[:2]
    x0, y0, x1, y1 = p.box
    side = max(x1 - x0, y1 - y0)
    wx0, wy0 = max(0, x0 - side), max(0, y0 - side)
    wx1, wy1 = min(w, x1 + side), min(h, y1 + side)
    win = grey[wy0:wy1, wx0:wx1].astype(np.float64)
    if win.size == 0:
        return None, "box outside the frame: dropped"
    med = float(np.median(win))
    sigma = max(1.4826 * float(np.median(np.abs(win - med))), 2.0)
    distinct = (np.abs(win - med) > DISTINCT_SIGMA * sigma).astype(np.uint8)
    n, _lab, stats, cents = cv2.connectedComponentsWithStats(distinct, connectivity=8)
    cx, cy = (x0 + x1) / 2 - wx0, (y0 + y1) / 2 - wy0
    best, best_d = -1, math.inf
    for j in range(1, n):
        area = int(stats[j, cv2.CC_STAT_AREA])
        if area < MIN_DISTINCT_PX or area > 0.25 * win.size:
            continue
        d = math.hypot(float(cents[j][0]) - cx, float(cents[j][1]) - cy)
        if d < best_d:
            best, best_d = j, d
    if best < 0:
        return None, "nothing distinct from the water within one box size: dropped"
    bx, by = int(stats[best, cv2.CC_STAT_LEFT]), int(stats[best, cv2.CC_STAT_TOP])
    bw, bh = int(stats[best, cv2.CC_STAT_WIDTH]), int(stats[best, cv2.CC_STAT_HEIGHT])
    pad = 2
    box = (
        max(0, wx0 + bx - pad),
        max(0, wy0 + by - pad),
        min(w, wx0 + bx + bw + pad),
        min(h, wy0 + by + bh + pad),
    )
    point = (round(wx0 + float(cents[best][0])), round(wy0 + float(cents[best][1])))
    inside = x0 <= point[0] < x1 and y0 <= point[1] < y1
    note = (
        None
        if inside
        else f"snapped to a distinct object {best_d:.0f} px from the box centre, box "
        f"{_fmt_box(box)}"
    )
    return Prompted(box, point, p.what), note


# --------------------------------------------------------------------------- scale


@dataclass(frozen=True, slots=True)
class FlowScale:
    """Centimetres per key-frame pixel at the reference, its relative sigma, and its source."""

    cm_per_px: float
    rel_sigma: float
    source: str
    at_xy: tuple[float, float] | None = None  # where in the key frame the scale holds


def wheel_scale(ref: ReferenceChoice, m: DepthMeasurement) -> FlowScale:
    """The wheel's scale, D / (2a): the diameter's sigma and 3 percent for the ellipse fit."""
    rel = math.hypot(ref.diameter_sigma_cm / ref.diameter_cm, 0.03)
    e = m.ellipse
    return FlowScale(
        m.cm_per_px,
        rel,
        f"{ref.kind.value.replace('_', ' ')}, {ref.diameter_cm:.1f} cm diameter ({ref.source})",
        (float(e.cx), float(e.cy)),
    )


def person_scale(pm: PersonMeasurement) -> FlowScale | None:
    """A person's scale: the head-to-shoulder length is 0.182 of stature.

    The relative sigma adds, in quadrature, the stature prior's, the spread of
    that proportion between adults, and the posture term of ``person.py``.
    """
    s_px = pm.shoulder_y - pm.top_y
    if not (s_px > 0 and pm.height_cm > 0):
        return None
    cm = HEAD_TO_SHOULDER * pm.height_cm / s_px
    rel = math.sqrt(
        (pm.height_sigma_cm / pm.height_cm) ** 2
        + (HEAD_TO_SHOULDER_SD / HEAD_TO_SHOULDER) ** 2
        + POSTURE_REL_SD**2
    )
    return FlowScale(
        cm,
        rel,
        f"person, head to shoulder is {HEAD_TO_SHOULDER:.3f} of a {pm.height_cm:.0f} cm stature",
        (float(pm.x_centre), float(pm.waterline_y)),
    )


# --------------------------------------------------------------------------- track checks


@dataclass
class TrackVerdict:
    track_id: str
    what: str
    n_frames: int  # frames with a position
    n_lost: int
    kept: bool
    reason: str | None = None
    px_per_s: float | None = None
    speed_mps: float | None = None
    sigma_mps: float | None = None
    median_y_px: float | None = None


def check_track(
    obj: ObjectTrack, n_frames: int
) -> tuple[list[tuple[int, float, float]], str | None]:
    """The positions to use from one object's track, or why the whole track is set aside."""
    if n_frames == 0 or not obj.points:
        return [], "never found by the tracker"
    if len(obj.lost) > MAX_LOST_SHARE * n_frames:
        return [], f"lost on {len(obj.lost)} of {n_frames} frames"
    areas = np.asarray(obj.areas, dtype=np.float64)
    med = float(np.median(areas))
    ok = (areas >= med / AREA_RANGE) & (areas <= med * AREA_RANGE)
    if (~ok).sum() > MAX_BAD_AREA_SHARE * len(areas):
        return [], (
            f"mask size changed more than {AREA_RANGE:.0f} times on "
            f"{int((~ok).sum())} of {len(areas)} frames (it slid onto something else)"
        )
    return [p for p, good in zip(obj.points, ok, strict=True) if good], None


# --------------------------------------------------------------------------- result


@dataclass
class FlowResult:
    estimate: VelocityEstimate | None
    reason: str
    fps: float
    scale: FlowScale | None = None
    reading: DebrisReading | None = None
    tracks: BoxTracks | None = None
    verdicts: list[TrackVerdict] = field(default_factory=list)
    # Key-frame pixels: the positions actually used, per track id.
    used_tracks: dict[str, list[tuple[int, float, float]]] = field(default_factory=dict)
    # How many floating objects the scene model boxed, before the distinct-object check.
    n_located: int = 0
    # Without a scale: the same statistics in pixels per second (fields named _mps).
    pixel_estimate: VelocityEstimate | None = None
    anchor_used: bool = False
    anchor_shift_px: float | None = None  # the anchor's largest distance from its start
    cost_usd: float = 0.0
    timings_ms: dict[str, int] = field(default_factory=dict)
    steps: list[TraceStep] = field(default_factory=list)


def _ms(t0: float) -> int:
    return round(1000 * (time.perf_counter() - t0))


def _fmt_box(b: tuple[int, int, int, int]) -> str:
    return "[" + ",".join(str(v) for v in b) + "]"


async def measure_flow(
    video: VideoInput,
    locator: DebrisLocator,
    tracker: BoxTracker,
    scale: FlowScale | None,
    *,
    window_s: float = WINDOW_S,
    target_fps: float = TARGET_FPS,
    scene_model: str = "muse-spark-open",
) -> FlowResult:
    """Surface speed of the water in ``video``, or the reason there is none.

    With ``scale`` None the tracks are still found and reported in pixels per
    second, but no speed in metres per second is given.
    """
    res = FlowResult(None, "", video.fps, scale)
    key = video.key_frame
    if video.fps <= 0:
        res.reason = "the video has no frame rate"
        return res
    # 1. Debris and anchor on the key frame.
    t0 = time.perf_counter()
    try:
        reply = await locator.locate_debris_json(key)
    except ModelError as exc:
        res.reason = f"the debris locator failed: {exc}"
        res.steps.append(
            TraceStep(
                "debris",
                "video",
                scene_model,
                "key frame",
                str(exc),
                "give up",
                _ms(t0),
                DEBRIS_VERSION,
            )
        )
        return res
    res.timings_ms["debris"] = _ms(t0)
    res.cost_usd += reply.usage.cost_usd
    reading = parse_debris(reply.data, key.width, key.height)
    res.reading = reading
    res.n_located = len(reading.debris)
    found = ", ".join(f"{p.what} {_fmt_box(p.box)}" for p in reading.debris) or "none"
    anchor_text = (
        f"; anchor {reading.anchor.what} {_fmt_box(reading.anchor.box)}" if reading.anchor else ""
    )
    res.steps.append(
        TraceStep(
            "debris",
            "video",
            scene_model,
            f"key frame {video.key_index} ({key.width}x{key.height} px)",
            f"{len(reading.debris)} floating objects: {found}{anchor_text}; flow: "
            f"{reading.flow or 'not stated'}",
            "track" if reading.debris else "stop: nothing floating to track",
            res.timings_ms["debris"],
            DEBRIS_VERSION,
        )
    )
    for d in reading.dropped:
        res.steps.append(TraceStep("validate", "video", "validator", "debris answer", d, "drop", 0))
    if not reading.debris:
        res.reason = "the scene model found nothing floating to track"
        return res

    # 1b. Each box must hold something distinct from the water: snap or drop.
    grey = np.asarray(cv2.cvtColor(key.rgb, cv2.COLOR_RGB2GRAY), dtype=np.uint8)
    kept_debris: list[Prompted] = []
    for i, p in enumerate(reading.debris):
        q, note = snap_to_object(grey, p)
        if q is not None and any(_box_iou(q.box, r.box) > 0.5 for r in kept_debris):
            q, note = None, "snapped onto the same object as an earlier box: dropped"
        if note is not None:
            res.steps.append(
                TraceStep(
                    "validate",
                    "video",
                    "validator",
                    f"debris {i + 1} ({p.what}) {_fmt_box(p.box)}",
                    note,
                    "drop" if q is None else "snap",
                    0,
                )
            )
        if q is not None:
            kept_debris.append(q)
    anchor = reading.anchor
    if anchor is not None:
        q, note = snap_to_object(grey, anchor)
        if note is not None:
            res.steps.append(
                TraceStep(
                    "validate",
                    "video",
                    "validator",
                    f"anchor ({anchor.what}) {_fmt_box(anchor.box)}",
                    note,
                    "drop" if q is None else "snap",
                    0,
                )
            )
        anchor = q
    reading = replace(reading, debris=tuple(kept_debris), anchor=anchor)
    res.reading = reading
    if not reading.debris:
        res.reason = "no box from the scene model held an object distinct from the water"
        return res

    # 2. Track every box (and the anchor) forward from the key frame.
    k = key.width / video.width  # key-frame px per video px
    prompts = [*reading.debris, *([reading.anchor] if reading.anchor else [])]
    boxes = [
        (
            int(p.box[0] / k),
            int(p.box[1] / k),
            math.ceil(p.box[2] / k),
            math.ceil(p.box[3] / k),
        )
        for p in prompts
    ]
    points = [
        None if p.point is None else (round(p.point[0] / k), round(p.point[1] / k)) for p in prompts
    ]
    stride = max(1, round(video.fps / target_fps))
    max_frames = max(5, int(window_s * video.fps / stride))
    t0 = time.perf_counter()
    try:
        tracks = await tracker.track_boxes(
            video, video.key_index, boxes, points=points, stride=stride, max_frames=max_frames
        )
    except ModelError as exc:
        res.reason = f"tracking failed: {exc}"
        res.steps.append(
            TraceStep(
                "track",
                "video",
                tracker.mask_model_name,
                f"{len(boxes)} boxes",
                str(exc),
                "give up",
                _ms(t0),
            )
        )
        return res
    res.timings_ms["track"] = _ms(t0)
    res.tracks = tracks
    n = len(tracks.frames)
    ids = list(tracks.objects)
    anchor_id = ids[-1] if reading.anchor else None
    res.steps.append(
        TraceStep(
            "track",
            "video",
            tracker.mask_model_name,
            f"{len(reading.debris)} debris boxes"
            + (" and the anchor" if reading.anchor else "")
            + f" on frame {video.key_index}, every {stride} frame(s) for up to {window_s:.0f} s",
            f"{n} frames segmented ({n * stride / video.fps:.1f} s); found on "
            + ", ".join(f"{oid}: {len(o.points)}/{n}" for oid, o in tracks.objects.items()),
            "check the tracks",
            res.timings_ms["track"],
        )
    )

    # 3. Track checks, in key-frame pixels.
    def to_key(pts: list[tuple[int, float, float]]) -> list[tuple[int, float, float]]:
        return [(f, x * k, y * k) for f, x, y in pts]

    reference: Track | None = None
    if anchor_id is not None:
        a = tracks.objects[anchor_id]
        pts, why = check_track(a, n)
        share = len(pts) / n if n else 0.0
        if why is None and share >= ANCHOR_MIN_SHARE:
            reference = to_key(pts)
            x0, y0 = reference[0][1], reference[0][2]
            res.anchor_shift_px = max(math.hypot(x - x0, y - y0) for _, x, y in reference)
            res.anchor_used = True
            out = f"anchor found on {len(pts)}/{n} frames, moved up to {res.anchor_shift_px:.0f} px"
            dec = "subtract its motion from every track"
        else:
            out = f"anchor not usable ({why or f'found on {len(pts)}/{n} frames'})"
            dec = "camera motion not removed"
        res.steps.append(TraceStep("velocity", "anchor", "geometry", "anchor track", out, dec, 0))
    elif reading.anchor is None:
        res.steps.append(
            TraceStep(
                "velocity",
                "anchor",
                "geometry",
                "no anchor given",
                "no fixed point to follow",
                "camera motion not removed",
                0,
            )
        )

    candidates: dict[str, list[tuple[int, float, float]]] = {}
    for oid, prompt in zip(ids, prompts, strict=False):
        if oid == anchor_id:
            continue
        obj = tracks.objects[oid]
        pts, why = check_track(obj, n)
        v = TrackVerdict(oid, prompt.what, len(obj.points), len(obj.lost), why is None, why)
        if pts:
            kp = to_key(pts)
            v.median_y_px = float(np.median([p[2] for p in kp]))
            ps: TrackVelocity | None = pixel_speed(oid, kp, video.fps, reference)
            if ps is not None:
                v.px_per_s = ps.speed_mps
        if why is None:
            candidates[oid] = to_key(pts)
        res.verdicts.append(v)

    # 4. Speed.
    if scale is None:
        # The same filters in pixel units (100 cm per px makes the "m/s" fields
        # px/s), so an unscaled clip still says which tracks are flow tracers.
        px = surface_velocity(candidates, video.fps, 100.0, scale_rel_sigma=0.0,
                              reference=reference)  # fmt: skip
        px_used = {t.track_id for t in px.tracks} if isinstance(px, VelocityEstimate) else set()
        for v in res.verdicts:
            if v.track_id in px_used:
                v.kept = False
                v.reason = "no scale (passed every check)"
            elif v.kept:
                v.kept = False
                v.reason = px.rejected.get(v.track_id, "not used")
        res.pixel_estimate = px if isinstance(px, VelocityEstimate) else None
        if isinstance(px, VelocityEstimate):
            res.reason = (
                "no reference was measured on the key frame, so the motion cannot be scaled: "
                f"{px.surface_mps:.0f} px/s (sigma {px.surface_sigma_mps:.0f}) from "
                f"{px.n_used} tracks that passed every check"
            )
        else:
            res.reason = (
                "no reference was measured on the key frame, and no track gave a motion "
                f"({px.reason})"
            )
        res.steps.append(
            TraceStep(
                "velocity", "video", "geometry", "tracks in pixels", res.reason, "no speed", 0
            )
        )
        return res
    est = surface_velocity(
        candidates,
        video.fps,
        scale.cm_per_px,
        scale_rel_sigma=scale.rel_sigma,
        reference=reference,
    )
    rejected = est.rejected
    used = {t.track_id: t for t in est.tracks} if isinstance(est, VelocityEstimate) else {}
    for v in res.verdicts:
        if v.track_id in used:
            v.speed_mps = used[v.track_id].speed_mps
            v.sigma_mps = used[v.track_id].sigma_mps
        elif v.kept:
            v.kept = False
            v.reason = rejected.get(v.track_id, "not used")
    res.used_tracks = {tid: candidates[tid] for tid in used}
    n_rej = sum(1 for v in res.verdicts if not v.kept)
    if isinstance(est, VelocityFailure):
        res.reason = f"velocity unavailable: {est.reason}"
        res.steps.append(
            TraceStep(
                "velocity",
                "video",
                "geometry",
                f"{len(candidates)} tracks passed their checks, scale {scale.source}",
                est.reason,
                f"no speed ({n_rej} tracks rejected)",
                0,
            )
        )
        return res
    res.estimate = est
    res.reason = (
        f"surface velocity {est.surface_mps:.2f} m/s (sigma {est.surface_sigma_mps:.2f}) from "
        f"{est.n_used} tracked objects, {n_rej} rejected; depth-averaged "
        f"{est.mean_mps:.2f} m/s (sigma {est.mean_sigma_mps:.2f})"
    )
    res.steps.append(
        TraceStep(
            "velocity",
            "video",
            "geometry",
            f"{len(candidates)} tracks passed their checks, scale {scale.cm_per_px:.3f} cm/px "
            f"(sigma {100 * scale.rel_sigma:.0f}%) from the {scale.source}",
            res.reason,
            "report velocity",
            0,
        )
    )
    return res
