"""The verify-and-refine agent: Muse Spark Open checks SAM's masks and corrects the prompts.

A single grounding point from a reasoning model is not trustworthy on a busy
street photo: in the first probe the point for a scooter wheel landed on the
blue water tank next to it, and SAM duly segmented the tank. So no mask is used
until it has passed two judges:

1. SAM proposes masks for the scene model's box and tyre points: its three
   proposals for the box with the points, for the box alone and for the points
   alone (ring prompting: positive points on the rubber around the arc,
   negative points on the hub and on parts to leave out).
2. Each proposal is closed into a solid wheel shape and scored by how well its
   outline fits one ellipse over a long arc (``wheel_shape.py``). This judge
   costs milliseconds.
3. The two best are drawn side by side on a crop and the scene model picks one
   and judges it: is it the tyre, is the waterline visible, and if not, a
   corrected box and tyre points. Corrections go back to SAM, at most
   ``max_refinements`` times.
4. An outline is accepted only when the scene model calls it good and its shape
   score passes ``accept_shape``.

The water mask goes through a similar loop on the whole photo. Every step is
recorded in a compact, truthful trace (:class:`TraceStep`) and, when an
``emit`` callback is given, reported the moment it happens.
"""

from __future__ import annotations

import asyncio
import io
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Protocol, TypeVar

import cv2
import numpy as np
from numpy.typing import NDArray
from PIL import Image

from .adapters import AnswerRejected, MaskModel, ModelError, SceneObject, VehicleObject
from .media import ImageInput
from .muse_open import (
    VERIFY_WATER_VERSION,
    VERIFY_WHEEL_VERSION,
    JsonReply,
    grid_box,
    grid_point,
    verify_prompt,
    water_verify_prompt,
)
from .references import RefKind
from .sam import ObjectMask, ObjectPrompt, PointPrompt
from .sam_local import box_iou
from .validate import Checked, check_water_answer, check_wheel_answer
from .wheel_finder import (
    INSIDE_TYRE_RATIO,
    WheelScore,
    dark_ellipse_boxes,
    grid_points,
    grow_region,
    outside_inside_ratio,
    points_outside,
    score_wheel,
    tyre_evidence,
    tyre_failure,
    wheel_region,
)
from .wheel_shape import ShapeScore, dedupe, mask_iou, solid_wheel, wheel_shape

KIND_TEXT = {
    RefKind.TWO_WHEELER_WHEEL: "two-wheeler wheel and tyre",
    RefKind.CAR_WHEEL: "car wheel and tyre",
    RefKind.AUTO_WHEEL: "auto-rickshaw wheel and tyre",
    RefKind.BUS_WHEEL: "bus wheel and tyre",
    RefKind.KERB: "kerb",
    RefKind.PERSON: "person",
}

MAGENTA = (255, 0, 255)
YELLOW = (255, 214, 0)


class Verifier(Protocol):
    @property
    def scene_model_name(self) -> str: ...

    async def ask_image(
        self, jpeg: bytes | Sequence[bytes], prompt: str, *, what: str
    ) -> JsonReply: ...


@dataclass(frozen=True, slots=True)
class TraceStep:
    step: str  # scene, segment, verify, geometry, estimate
    target: str  # "scene", "water", "wheel 1", ...
    model: str
    input: str
    output: str
    decision: str
    latency_ms: int
    # The version of the prompt sent (muse_open.prompt_version) on steps that
    # called the language model; None otherwise.
    prompt_version: str | None = None


Box = tuple[int, int, int, int]
Emit = Callable[[TraceStep], None]


@dataclass(frozen=True, slots=True)
class Proposal:
    """A candidate wheel mask, closed into a solid shape, with its shape score."""

    mask: NDArray[np.bool_] = field(repr=False)  # solid wheel shape
    shape: ShapeScore
    source: str  # which prompt variant and which of SAM's proposals
    # Brightness just outside over just inside the outline; well below one when
    # the outline follows the rim, inside the dark tyre (wheel_finder).
    edge_ratio: float = 1.0
    # Why the numbers say this is not a tyre's outer edge (wheel_finder.tyre_failure).
    not_tyre: str | None = None


@dataclass
class RefOutcome:
    index: int
    obj: SceneObject
    accepted: bool
    mask: ObjectMask | None
    box: Box | None
    refinements: int
    waterline_points: tuple[tuple[int, int], ...] = ()
    waterline_visible: bool = False
    verdict: str = ""
    trace: list[TraceStep] = field(default_factory=list)
    cost_usd: float = 0.0
    shape: ShapeScore | None = None
    proposals_shown: int = 0


@dataclass
class Judgement:
    decision: str
    summary: str
    verdict: str
    waterline: tuple[tuple[int, int], ...]
    new_box: Box | None
    new_pts: tuple[PointPrompt, ...]
    waterline_visible: bool = False


@dataclass
class WaterOutcome:
    accepted: bool
    mask: ObjectMask | None
    refinements: int
    verdict: str = ""
    trace: list[TraceStep] = field(default_factory=list)
    cost_usd: float = 0.0


# --------------------------------------------------------------------------- rendering


def _count(n: int, noun: str) -> str:
    """'1 dark blob', '2 dark blobs'."""
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _jpeg(rgb: NDArray[np.uint8], quality: int = 90) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def _near_each_other(shown: list[Proposal], spread: float = 2.0) -> list[Proposal]:
    """The two best proposals when they are near each other, else only the best:
    near means the box around both is at most ``spread`` times the larger one's side."""
    if len(shown) < 2:
        return shown
    bbs = [mask_bbox(p.mask) for p in shown[:2]]
    if bbs[0] is None or bbs[1] is None:
        return shown[:1]
    a, b = bbs[0], bbs[1]
    union = max(max(a[2], b[2]) - min(a[0], b[0]), max(a[3], b[3]) - min(a[1], b[1]))
    larger = max(a[2] - a[0], a[3] - a[1], b[2] - b[0], b[3] - b[1])
    return shown[:2] if union <= spread * larger else shown[:1]


def crop_rect(
    w: int,
    h: int,
    box: Box | None,
    mask_bb: Box | None,
    *,
    context: float = 2.0,
    min_side: int = 256,
) -> Box:
    """A square crop around the prompt box and the mask, with context on every side.

    ``context`` is the margin on each side as a multiple of the region's larger
    side, so a box that missed its object by about its own size still shows it.
    """
    boxes = [b for b in (box, mask_bb) if b is not None]
    if not boxes:
        return 0, 0, w, h
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[2] for b in boxes)
    y1 = max(b[3] for b in boxes)
    side = max(x1 - x0, y1 - y0)
    half = max(min_side / 2.0, side * (0.5 + context))
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    left = max(0, round(cx - half))
    top = max(0, round(cy - half))
    right = min(w, round(cx + half))
    bottom = min(h, round(cy + half))
    return left, top, max(right, left + 2), max(bottom, top + 2)


def render_crop(
    rgb: NDArray[np.uint8],
    mask: NDArray[np.bool_] | None,
    box: Box | None,
    rect: Box,
    out_side: int = 768,
) -> bytes:
    """The crop, scaled so its longer side is ``out_side``, with the outline and box drawn."""
    return _jpeg(render_crop_rgb(rgb, mask, box, rect, out_side), quality=80)


def render_crop_rgb(
    rgb: NDArray[np.uint8],
    mask: NDArray[np.bool_] | None,
    box: Box | None,
    rect: Box,
    out_side: int = 768,
) -> NDArray[np.uint8]:
    left, top, right, bottom = rect
    crop = np.ascontiguousarray(rgb[top:bottom, left:right])
    s = out_side / max(crop.shape[:2])
    big: NDArray[np.uint8] = np.ascontiguousarray(
        cv2.resize(
            crop,
            (max(1, round(crop.shape[1] * s)), max(1, round(crop.shape[0] * s))),
            interpolation=cv2.INTER_CUBIC if s > 1 else cv2.INTER_AREA,
        ),
        dtype=np.uint8,
    )
    if box is not None:
        p0 = (round((box[0] - left) * s), round((box[1] - top) * s))
        p1 = (round((box[2] - left) * s), round((box[3] - top) * s))
        cv2.rectangle(big, p0, p1, YELLOW, 2, cv2.LINE_AA)
    if mask is not None and mask.any():
        m = np.ascontiguousarray(mask[top:bottom, left:right], dtype=np.uint8)
        contours, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        scaled = [np.rint(c.astype(np.float64) * s).astype(np.int32) for c in contours]
        cv2.drawContours(big, scaled, -1, MAGENTA, 2, cv2.LINE_AA)
    return big


def render_choice(
    rgb: NDArray[np.uint8],
    masks: Sequence[NDArray[np.bool_]],
    box: Box | None,
    rect: Box,
    out_side: int = 640,
) -> tuple[list[bytes], bytes]:
    """The same crop once per proposal (A, B), each with its outline, as separate images.

    Separate images, not one side-by-side picture: with two panels in one image
    the model mixed up which grid its coordinates were on. Returns the images and
    a side-by-side copy for the evaluation's debug folder.
    """
    panels = []
    for i, m in enumerate(masks):
        one = render_crop_rgb(rgb, m, box, rect, out_side)
        cv2.putText(one, "AB"[i], (8, 34), cv2.FONT_HERSHEY_SIMPLEX, 1.1, YELLOW, 3, cv2.LINE_AA)
        panels.append(one)
    images = [_jpeg(p, quality=80) for p in panels]
    if len(panels) > 1:
        gap = np.full((panels[0].shape[0], 8, 3), 255, dtype=np.uint8)
        both = _jpeg(np.ascontiguousarray(np.hstack([panels[0], gap, panels[1]])), quality=70)
    else:
        both = images[0]
    return images, both


def render_water(
    rgb: NDArray[np.uint8], mask: NDArray[np.bool_] | None, box: Box | None, max_side: int = 1024
) -> bytes:
    img = rgb.astype(np.float32)
    if mask is not None and mask.any():
        img[mask] = 0.6 * img[mask] + 0.4 * np.array(MAGENTA, dtype=np.float32)
    out: NDArray[np.uint8] = np.ascontiguousarray(img.astype(np.uint8))
    if mask is not None and mask.any():
        contours, _ = cv2.findContours(
            np.ascontiguousarray(mask, dtype=np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE
        )
        th = max(2, round(max(rgb.shape[:2]) / 500))
        cv2.drawContours(out, contours, -1, MAGENTA, th, cv2.LINE_AA)
    if box is not None:
        th = max(2, round(max(rgb.shape[:2]) / 500))
        cv2.rectangle(out, box[:2], (box[2], box[3]), YELLOW, th, cv2.LINE_AA)
    s = min(1.0, max_side / max(out.shape[:2]))
    if s < 1.0:
        out = np.asarray(
            cv2.resize(
                out,
                (round(out.shape[1] * s), round(out.shape[0] * s)),
                interpolation=cv2.INTER_AREA,
            ),
            dtype=np.uint8,
        )
    return _jpeg(np.ascontiguousarray(out), quality=80)


@dataclass(frozen=True, slots=True)
class View:
    """How the model's 0 to 1000 grid over an image it was shown maps to the photo.

    ``rect`` is the crop shown. With two panels side by side, panel A spans the
    first ``panel_frac`` of the image width and panel B starts at ``b_start``;
    a point in either panel maps to the same place in the crop.
    """

    rect: Box
    panel_frac: float = 1.0
    b_start: float = 1.0


def from_crop_grid(p: tuple[float, float], view: View | Box) -> tuple[int, int]:
    v = view if isinstance(view, View) else View(view)
    left, top, right, bottom = v.rect
    gx = p[0] / 1000.0
    if gx >= v.b_start:  # a point given in panel B
        gx -= v.b_start
    fx = min(1.0, gx / v.panel_frac)
    return (
        round(left + fx * (right - left)),
        round(top + p[1] / 1000.0 * (bottom - top)),
    )


def crop_box_to_image(b: tuple[float, float, float, float], view: View | Box) -> Box:
    x0, y0 = from_crop_grid((b[0], b[1]), view)
    x1, y1 = from_crop_grid((b[2], b[3]), view)
    x0, x1 = min(x0, x1), max(x0, x1)
    return x0, y0, max(x1, x0 + 2), max(y1, y0 + 2)


def _crop_points(v: Any, view: View | Box, limit: int) -> list[tuple[int, int]]:
    if not isinstance(v, list):
        return []
    out = []
    for p in v[:limit]:
        g = grid_point(p)
        if g is not None:
            out.append(from_crop_grid(g, view))
    return out


def _fmt_points(pts: Sequence[PointPrompt]) -> str:
    pos = [f"[{p.x},{p.y}]" for p in pts if p.positive]
    neg = [f"[{p.x},{p.y}]" for p in pts if not p.positive]
    text = ""
    if pos:
        text += " + point" + ("s " if len(pos) > 1 else " ") + ", ".join(pos)
    if neg:
        text += " - point" + ("s " if len(neg) > 1 else " ") + ", ".join(neg)
    return text


def mask_bbox(mask: NDArray[np.bool_]) -> Box | None:
    ys, xs = np.nonzero(mask)
    if xs.size == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def _initial_points(obj: SceneObject, box: Box | None) -> tuple[PointPrompt, ...]:
    """The scene model's ring of tyre points (or its one point); the hub and the parts
    next to the tyre (exhaust, mudguard, wall) as negative points."""

    def near(p: tuple[int, int]) -> bool:
        if box is None:
            return True
        mx, my = 0.25 * (box[2] - box[0]), 0.25 * (box[3] - box[1])
        return box[0] - mx <= p[0] < box[2] + mx and box[1] - my <= p[1] < box[3] + my

    ring = [p for p in obj.tyre_points if near(p)]
    pts = [PointPrompt(*p) for p in ring]
    if not pts and obj.point is not None and near(obj.point):
        pts = [PointPrompt(*obj.point)]
    if not pts:
        return ()
    negatives = [p for p in obj.exclude_points if near(p) and p not in ring]
    if obj.rim_point is not None and near(obj.rim_point) and obj.rim_point not in ring:
        negatives.insert(0, obj.rim_point)  # the hub
    pts += [PointPrompt(*p, positive=False) for p in dict.fromkeys(negatives)]
    return tuple(pts)


# A proposal whose bounding box is this many times the area of the tyre box it
# was prompted with is the vehicle or the street around the tyre, not the tyre.
SPILL_AREA = 4.0


def spills_far_beyond(mask: NDArray[np.bool_], box: Box) -> bool:
    """True when the mask's bounding box is more than ``SPILL_AREA`` times the
    prompt box's area. On the Dzire photo (p_26202090) a box around the tyre
    above the water gave the whole side of the car as SAM's second proposal; shown
    to the verifier as B, it also widened the crop until the tyre was small."""
    bb = mask_bbox(mask)
    if bb is None:
        return False
    area = (bb[2] - bb[0]) * (bb[3] - bb[1])
    return area > SPILL_AREA * max(1, (box[2] - box[0]) * (box[3] - box[1]))


def _chosen(d: dict[str, Any], shown: Sequence[Proposal]) -> Proposal | None:
    """The proposal the verifier picked (A or B); the only one if just one was shown."""
    if not shown:
        return None
    pick = str(d.get("choice") or "").strip().upper()
    if pick.startswith("B") and len(shown) > 1:
        return shown[1]
    if pick.startswith("A") or (not pick and len(shown) == 1):
        return shown[0]
    if pick.startswith("N"):
        return None
    return shown[0] if len(shown) == 1 else None


def _fmt_box(b: Box | None) -> str:
    return "none" if b is None else f"[{b[0]},{b[1]},{b[2]},{b[3]}] px"


# The geometry needs at least 100 degrees of rim outline (about 0.28 of the
# edge); below a fifth there is no point in segmenting further.
MIN_EDGE_VISIBLE = 0.2


def _fraction(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return min(1.0, max(0.0, f)) if f == f else None


def _truthy(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1")
    return bool(v)


# --------------------------------------------------------------------------- time budget

# The moment (``time.perf_counter()``) by which this request's checks must be
# done; set by :meth:`VerifyRefineAgent.run` for the tasks it starts.
_DEADLINE: ContextVar[float | None] = ContextVar("agent_deadline", default=None)
T = TypeVar("T")
# A check is not started with less time than this left: it would not finish.
MIN_CALL_S = 6.0


class BudgetSpent(ModelError):
    """The request's time budget ran out before a check could be made."""


def _failed(exc: Exception, otherwise: str) -> str:
    if isinstance(exc, AnswerRejected):
        return "the scene model's answers failed their checks"
    return "time budget reached" if isinstance(exc, BudgetSpent) else otherwise


class _Costed(Protocol):
    cost_usd: float


# --------------------------------------------------------------------------- agent


@dataclass
class VerifyRefineAgent:
    verifier: Verifier
    masks: MaskModel
    max_refinements: int = 2
    # Panels are sent at this size: the request time on the development
    # network grew with the upload, and 512 px still shows a tyre's edge.
    crop_side: int = 512
    # Re-check every "accept" with a full-reasoning call (when the verifier offers
    # ``confirm_image``). Off by default: the shape score is the second judge.
    confirm: bool = False
    # Smallest wheel-shape score (``wheel_shape.py``) an accepted outline needs.
    accept_shape: float = 0.35
    # Smallest IoU between an accepted outline's bounding box and the tight box
    # the scene model draws around the tyre on the same crop.
    min_box_agreement: float = 0.7
    first_context: float = 2.0
    close_context: float = 0.8
    # When set, every image shown to the verifier is also written here (evaluation only).
    debug_dir: Path | None = None

    def _keep(self, jpeg: bytes, name: str) -> None:
        if self.debug_dir is not None:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            (self.debug_dir / name).write_bytes(jpeg)

    @staticmethod
    def _log(trace: list[TraceStep], step: TraceStep, emit: Emit | None) -> None:
        trace.append(step)
        if emit is not None:
            emit(step)

    @property
    def mask_name(self) -> str:
        return str(getattr(self.masks, "mask_model_name", self.masks.name))

    @property
    def scene_name(self) -> str:
        return str(getattr(self.verifier, "scene_model_name", "scene model"))

    async def _ask(
        self, images: bytes | Sequence[bytes], prompt: str, *, what: str, confirm: bool = False
    ) -> JsonReply:
        """One verifier call, inside the request's time budget when there is one."""
        name = "confirm_image" if confirm else "ask_image"
        fn: Callable[..., Awaitable[JsonReply]] = getattr(self.verifier, name)
        deadline = _DEADLINE.get()
        if deadline is None:
            return await fn(images, prompt, what=what)
        left = deadline - time.perf_counter()
        if left < MIN_CALL_S:
            raise BudgetSpent("the time budget ran out before this check")
        try:
            return await asyncio.wait_for(fn(images, prompt, what=what), left)
        except TimeoutError as exc:
            raise BudgetSpent("the time budget ran out during this check") from exc

    async def _in_budget(self, aw: Awaitable[T]) -> T:
        """A SAM request inside the request's time budget: not started after the
        deadline, and abandoned when still running at it (BudgetSpent)."""
        deadline = _DEADLINE.get()
        if deadline is None:
            return await aw
        left = deadline - time.perf_counter()
        if left <= 0:
            if asyncio.iscoroutine(aw):
                aw.close()
            raise BudgetSpent("the time budget ran out before this segmentation")
        try:
            return await asyncio.wait_for(aw, left)
        except TimeoutError as exc:
            raise BudgetSpent("the time budget ran out during this segmentation") from exc

    async def _ask_checked(
        self,
        images: bytes | Sequence[bytes],
        prompt: str,
        *,
        what: str,
        target: str,
        check: Callable[[Any], Checked],
        trace: list[TraceStep],
        emit: Emit | None,
        bill: _Costed,
        confirm: bool = False,
    ) -> JsonReply:
        """One verifier call whose answer has passed the checks (``validate.py``).

        When a field the decision rests on fails, a ``validate`` step says so and
        the same question is asked once more; failing again is logged as ``give
        up`` and raises :class:`AnswerRejected`. Other fields that fail are left
        out of the answer returned and named in a ``validate`` step. The cost of
        every answer is added to ``bill``."""
        reply = await self._ask(images, prompt, what=what, confirm=confirm)
        bill.cost_usd += reply.usage.cost_usd
        checked = check(reply.data)
        latency = reply.latency_ms
        if checked.fatal is not None:
            first = checked.fatal
            self._log(
                trace,
                TraceStep("validate", target, "validator", f"answer to {what}", first, "retry", 0),
                emit,
            )
            reply = await self._ask(images, prompt, what=what, confirm=confirm)
            bill.cost_usd += reply.usage.cost_usd
            latency += reply.latency_ms
            checked = check(reply.data)
            if checked.fatal is not None:
                self._log(
                    trace,
                    TraceStep(
                        "validate",
                        target,
                        "validator",
                        f"second answer to {what}",
                        checked.fatal,
                        "give up",
                        0,
                    ),
                    emit,
                )
                raise AnswerRejected(what, [first, checked.fatal], 0.0)
        if checked.problems:
            n = len(checked.problems)
            self._log(
                trace,
                TraceStep(
                    "validate",
                    target,
                    "validator",
                    f"answer to {what}",
                    "; ".join(checked.problems),
                    f"drop {n} field{'s' if n > 1 else ''} that failed, use the rest",
                    0,
                ),
                emit,
            )
        return JsonReply(
            checked.data, reply.usage, latency, reply.reasoning, reply.repaired, reply.cached
        )

    async def _segment(
        self, image: ImageInput, prompt: ObjectPrompt
    ) -> tuple[ObjectMask | None, int]:
        t0 = time.perf_counter()
        res = await self._in_budget(self.masks.segment(image, [prompt]))
        ms = round(1000 * (time.perf_counter() - t0))
        m = res.masks.get(prompt.object_id)
        if m is None or m.area == 0:
            return None, ms
        return m, ms

    @staticmethod
    def _proposal(
        image: ImageInput,
        solid: NDArray[np.bool_],
        shape: ShapeScore,
        source: str,
        vehicle: VehicleObject | None,
    ) -> Proposal:
        ratio = outside_inside_ratio(image.rgb, solid)
        ev = tyre_evidence(image.rgb, solid)
        why = tyre_failure(
            ev,
            vehicle.kind if vehicle is not None else None,
            vehicle.box[2] - vehicle.box[0] if vehicle is not None else None,
        )
        return Proposal(solid, shape, source, ratio, why)

    async def _proposals(
        self,
        image: ImageInput,
        box: Box | None,
        pts: tuple[PointPrompt, ...],
        object_id: int,
        vehicle: VehicleObject | None = None,
    ) -> tuple[list[Proposal], int, int]:
        """Candidate masks for one prompt, best wheel shape first; and how many SAM gave."""
        t0 = time.perf_counter()
        raw: list[tuple[NDArray[np.bool_], str]] = []
        many = getattr(self.masks, "candidates", None)
        if many is not None:
            for c in await self._in_budget(many(image, box, pts)):
                raw.append((c.mask, f"{c.variant} #{c.index + 1}"))
        else:  # a mask model with one answer per prompt
            m, _ = await self._segment(image, ObjectPrompt(object_id, pts, box))
            if m is not None:
                raw.append((m.mask, "prompt"))

        def score() -> list[Proposal]:
            keep = dedupe([m for m, _ in raw])
            out = []
            for i in keep:
                if box is not None and spills_far_beyond(raw[i][0], box):
                    continue
                solid, _ = solid_wheel(raw[i][0])
                out.append(self._proposal(image, solid, wheel_shape(raw[i][0]), raw[i][1], vehicle))
            return sorted(out, key=lambda p: -p.shape.score)

        props = await self._in_budget(asyncio.to_thread(score))
        return props, len(raw), round(1000 * (time.perf_counter() - t0))

    async def refine_reference(
        self,
        image: ImageInput,
        obj: SceneObject,
        index: int,
        object_id: int,
        emit: Emit | None = None,
        seed: list[Proposal] | None = None,
        target: str | None = None,
        trace: list[TraceStep] | None = None,
        vehicle: VehicleObject | None = None,
    ) -> RefOutcome:
        """Verify and refine one wheel. ``seed`` replaces SAM's first proposals (the
        vehicle-box search found them already); ``trace`` continues an earlier one."""
        target = target or f"{obj.kind.value.replace('_', ' ')} {index}"
        kind_text = KIND_TEXT[obj.kind]
        box: Box | None = obj.box
        out = RefOutcome(index, obj, False, None, box, 0)
        if trace is not None:
            out.trace = trace
        pts = _initial_points(obj, box)
        for attempt in range(self.max_refinements + 1):
            if attempt == 0 and seed is not None:
                props = seed
            else:
                props, n_raw, ms = await self._proposals(image, box, pts, object_id, vehicle)
            shown = props[:2]
            if not (attempt == 0 and seed is not None):
                self._log(
                    out.trace,
                    TraceStep(
                        "segment",
                        target,
                        self.mask_name,
                        f"box {_fmt_box(box)}" + _fmt_points(pts),
                        (
                            f"{n_raw} proposals, {len(props)} distinct; best shapes "
                            + ", ".join(f"{p.shape.score:.2f}" for p in shown)
                        )
                        if props
                        else "no mask",
                        "verify the best " + ("two" if len(shown) == 2 else "one")
                        if shown
                        else "verify",
                        ms,
                    ),
                    emit,
                )
            # The first look is wide, so a box that missed its object still shows
            # it, unless the best proposal already has a wheel's shape; later looks
            # are close, so the outline can be judged in detail.
            wide = attempt == 0 and not (shown and shown[0].shape.score >= self.accept_shape)
            # Proposals found by searching a vehicle's box: that box is the lower half
            # of the vehicle, not a wheel's, so a wide look around it showed the whole
            # photo (x1: crop [516,1,2048,1536] for a tyre about 200 px wide). The
            # look is close around the proposals, and when the two are far apart (two
            # different wheels, or a wheel and the grille) only the best is shown.
            searched = attempt == 0 and seed is not None
            if searched:
                wide = False
                shown = _near_each_other(shown)
            context = self.first_context if wide else self.close_context
            crop_box = None if searched else box
            union_bb = None
            if shown:
                bbs = [b for b in (mask_bbox(p.mask) for p in shown) if b is not None]
                if bbs:
                    union_bb = (
                        min(b[0] for b in bbs),
                        min(b[1] for b in bbs),
                        max(b[2] for b in bbs),
                        max(b[3] for b in bbs),
                    )
            rect = crop_rect(image.width, image.height, crop_box, union_bb, context=context)
            view = View(rect)
            if shown:
                images, both = await asyncio.to_thread(
                    render_choice,
                    image.rgb,
                    [p.mask for p in shown],
                    crop_box,
                    rect,
                    self.crop_side,
                )
            else:
                both = await asyncio.to_thread(
                    render_crop, image.rgb, None, box, rect, self.crop_side
                )
                images = [both]
            self._keep(both, f"{target.replace(' ', '_')}_try{attempt}.jpg")
            panels = max(1, len(shown))
            prompt = verify_prompt(kind_text, panels=panels)
            try:
                reply = await self._ask_checked(
                    images,
                    prompt,
                    what=f"verify {target}",
                    target=target,
                    check=partial(check_wheel_answer, panels=panels),
                    trace=out.trace,
                    emit=emit,
                    bill=out,
                )
            except ModelError as exc:
                if not isinstance(exc, AnswerRejected):
                    self._log(
                        out.trace,
                        TraceStep(
                            "verify",
                            target,
                            self.scene_name,
                            f"crop {_fmt_box(rect)}",
                            f"error: {exc}",
                            "give up",
                            0,
                            VERIFY_WHEEL_VERSION,
                        ),
                        emit,
                    )
                out.verdict = _failed(exc, "verification failed")
                return out
            chosen = _chosen(reply.data, shown)
            j = self._judge(reply.data, view, box, chosen, attempt)
            self._log(
                out.trace,
                TraceStep(
                    "verify",
                    target,
                    self.scene_name,
                    f"crop {_fmt_box(rect)} with {len(shown) or 'no'} outline"
                    + ("s A and B" if len(shown) == 2 else ""),
                    j.summary,
                    j.decision,
                    reply.latency_ms,
                    VERIFY_WHEEL_VERSION,
                ),
                emit,
            )
            if j.decision == "accept" and context > self.close_context and chosen is not None:
                # A wide view finds the object but hides the outline's detail: an
                # outline is only accepted from a close look at it alone.
                bb = mask_bbox(chosen.mask)
                rect = crop_rect(image.width, image.height, None, bb, context=self.close_context)
                jpeg = await asyncio.to_thread(
                    render_crop, image.rgb, chosen.mask, box, rect, self.crop_side
                )
                self._keep(jpeg, f"{target.replace(' ', '_')}_try{attempt}_close.jpg")
                try:
                    reply = await self._ask_checked(
                        jpeg,
                        verify_prompt(kind_text, 1),
                        what=f"look closer {target}",
                        target=target,
                        check=partial(check_wheel_answer, panels=1),
                        trace=out.trace,
                        emit=emit,
                        bill=out,
                    )
                except ModelError as exc:
                    if not isinstance(exc, AnswerRejected):
                        self._log(
                            out.trace,
                            TraceStep(
                                "verify",
                                target,
                                self.scene_name,
                                f"close crop {_fmt_box(rect)}",
                                f"error: {exc}",
                                "give up",
                                0,
                                VERIFY_WHEEL_VERSION,
                            ),
                            emit,
                        )
                    out.verdict = _failed(exc, "verification failed")
                    return out
                j = self._judge(reply.data, View(rect), box, chosen, attempt)
                self._log(
                    out.trace,
                    TraceStep(
                        "verify",
                        target,
                        self.scene_name,
                        f"close crop {_fmt_box(rect)} of the chosen outline",
                        j.summary,
                        j.decision,
                        reply.latency_ms,
                        VERIFY_WHEEL_VERSION,
                    ),
                    emit,
                )
            confirm = getattr(self.verifier, "confirm_image", None)
            if j.decision == "accept" and self.confirm and confirm is not None:
                # A quick check said yes; the same question with full reasoning
                # must agree before the outline is measured.
                assert chosen is not None
                one = await asyncio.to_thread(
                    render_crop, image.rgb, chosen.mask, box, rect, self.crop_side
                )
                try:
                    reply2 = await self._ask_checked(
                        one,
                        verify_prompt(kind_text, 1),
                        what=f"confirm {target}",
                        target=target,
                        check=partial(check_wheel_answer, panels=1),
                        trace=out.trace,
                        emit=emit,
                        bill=out,
                        confirm=True,
                    )
                except ModelError as exc:
                    if not isinstance(exc, AnswerRejected):
                        self._log(
                            out.trace,
                            TraceStep(
                                "confirm",
                                target,
                                self.scene_name,
                                "chosen outline, full reasoning",
                                f"error: {exc}",
                                "give up",
                                0,
                                VERIFY_WHEEL_VERSION,
                            ),
                            emit,
                        )
                    out.verdict = _failed(exc, "confirmation failed")
                    return out
                j2 = self._judge(reply2.data, View(rect), box, chosen, attempt)
                if not j2.waterline and j.waterline:
                    j2.waterline = j.waterline
                self._log(
                    out.trace,
                    TraceStep(
                        "confirm",
                        target,
                        self.scene_name,
                        "chosen outline, full reasoning",
                        j2.summary,
                        j2.decision,
                        reply2.latency_ms,
                        VERIFY_WHEEL_VERSION,
                    ),
                    emit,
                )
                j = j2
            out.box, out.refinements, out.proposals_shown = box, attempt, len(shown)
            if chosen is not None:
                out.shape = chosen.shape
                mb = mask_bbox(chosen.mask) or (0, 0, 0, 0)
                out.mask = ObjectMask(str(object_id), 0, chosen.mask, mb, chosen.shape.score)
            if j.decision == "accept":
                out.accepted = True
                out.waterline_points = j.waterline
                out.waterline_visible = j.waterline_visible
                out.verdict = "good"
                return out
            if j.decision.startswith("give up"):
                out.verdict = j.verdict
                out.mask = None
                return out
            # The corrected prompt replaces the old one: the scene model's first
            # points were the suspect part.
            box, pts = j.new_box, j.new_pts
        out.mask = None
        return out

    def _judge(
        self,
        d: dict[str, Any],
        rect: View,
        box: Box | None,
        chosen: Proposal | None,
        attempt: int,
    ) -> Judgement:
        """The decision for one verifier answer: accept, refine (with what) or give up."""
        is_target = _truthy(d.get("is_target")) and chosen is not None
        quality = str(d.get("outline_quality") or ("good" if is_target else "wrong")).lower()
        reason = str(d.get("reason") or "").strip()[:160]
        wl = [grid_point(p) for p in (d.get("waterline") or [])[:2]]
        wl_px = tuple(from_crop_grid(p, rect) for p in wl if p is not None)
        gb = grid_box(d.get("corrected_box"))
        corrected = crop_box_to_image(gb, rect) if gb is not None else None
        positive = _crop_points(d.get("tyre_points"), rect, 4)
        negative = _crop_points(d.get("exclude_points"), rect, 3)
        new_box = corrected if corrected is not None else box
        new_pts = tuple(PointPrompt(*p) for p in positive) + tuple(
            PointPrompt(*p, positive=False) for p in negative
        )
        # The scene model's own tight box around the tyre is a numeric check of
        # the outline: an outline that misses part of the tyre (or takes in more)
        # has a bounding box that disagrees with it.
        tb = grid_box(d.get("tyre_box"))
        tyre_box = crop_box_to_image(tb, rect) if tb is not None else None
        box_agree = None
        if chosen is not None and tyre_box is not None:
            cb = mask_bbox(chosen.mask)
            box_agree = box_iou(cb, tyre_box) if cb is not None else 0.0
        if corrected is None and box_agree is not None and box_agree < self.min_box_agreement:
            corrected = tyre_box
            new_box = corrected
        changed = bool(new_pts) or (
            corrected is not None and (box is None or box_iou(corrected, box) <= 0.9)
        )
        can_refine = attempt < self.max_refinements and changed
        # The numbers that tell a tyre's edge from the rim or a cut outline, and
        # the verifier's own points on the rubber, which must be inside the outline.
        not_tyre = chosen.not_tyre if chosen is not None else None
        if chosen is not None and not_tyre is None and len(positive) >= 2:
            n_out = points_outside(chosen.mask, positive)
            if 2 * n_out >= len(positive):
                not_tyre = (
                    f"{n_out} of {len(positive)} points the scene model put on the tyre rubber "
                    "lie outside the outline: it sits inside the tyre"
                )
        view = str(d.get("view") or "").lower()
        edge = _fraction(d.get("tyre_edge_visible"))
        # A tyre seen edge-on, or with almost none of its edge in view: there is
        # no outline to measure, so stop rather than spend more calls on it.
        # (The "standing" answer is not used: at low effort it flagged a parked
        # motorcycle as not standing. The scene prompt already skips vehicles on
        # their side.)
        unmeasurable = None
        if is_target and view == "head_on":
            unmeasurable = "tyre seen head-on, its outline cannot be measured"
        elif is_target and edge is not None and edge < MIN_EDGE_VISIBLE:
            unmeasurable = f"only {100 * edge:.0f}% of the tyre edge is visible"
        verdict = reason or "not the named object"
        if unmeasurable is not None:
            decision = f"give up: {unmeasurable}"
            verdict = unmeasurable
        elif (
            chosen is not None
            and is_target
            and quality == "good"
            and chosen.shape.score >= self.accept_shape
            and (box_agree is None or box_agree >= self.min_box_agreement)
            and chosen.edge_ratio >= INSIDE_TYRE_RATIO
            and not_tyre is None
        ):
            decision = "accept"
        elif can_refine:
            decision = "refine with box " + _fmt_box(new_box) + _fmt_points(new_pts)
        elif (
            chosen is not None
            and is_target
            and quality == "good"
            and box_agree is not None
            and box_agree < self.min_box_agreement
        ):
            decision = (
                f"give up: the outline covers a different area than the tyre ({box_agree:.2f})"
            )
            verdict = "outline and the tyre's box disagree"
        elif (
            chosen is not None
            and is_target
            and quality == "good"
            and chosen.edge_ratio < INSIDE_TYRE_RATIO
        ):
            decision = (
                "give up: the outline sits inside the tyre (darker outside than inside, "
                f"{chosen.edge_ratio:.2f})"
            )
            verdict = "outline follows the rim, inside the tyre"
        elif chosen is not None and is_target and quality == "good" and not_tyre is not None:
            decision = f"give up: {not_tyre}"
            verdict = "outline is not the tyre's outer edge"
        elif chosen is not None and is_target and quality == "good":
            decision = f"give up: the outline does not fit a wheel ({chosen.shape.text()})"
            verdict = "outline accepted by the scene model but not wheel-shaped"
        elif is_target and quality == "partial":
            # Only an outline the verifier calls good is measured: a partial one
            # (a mudguard or exhaust taken in) would bias the depth.
            decision = "give up: outline only partly right"
            verdict = f"outline only partly right: {reason}"
        else:
            decision = "give up"
        pick = str(d.get("choice") or "").strip().upper()[:4]
        summary = (
            (f"chose {pick}, " if pick else "")
            + (
                f"{chosen.shape.text()}, edge ratio {chosen.edge_ratio:.2f}, "
                if chosen is not None
                else ""
            )
            + (f"not a tyre edge: {not_tyre}, " if not_tyre is not None else "")
            + (f"box agreement {box_agree:.2f}, " if box_agree is not None else "")
            + f"is_target={str(is_target).lower()}, outline {quality}, "
            + (f"view {view}, " if view else "")
            + (f"edge visible {edge:.2f}, " if edge is not None else "")
            + f"waterline {'visible' if _truthy(d.get('waterline_visible')) else 'not visible'}"
            + (f"; {reason}" if reason else "")
        )
        return Judgement(
            decision,
            summary,
            verdict,
            wl_px if len(wl_px) == 2 else (),
            new_box,
            new_pts,
            _truthy(d.get("waterline_visible")),
        )

    async def refine_water(
        self,
        image: ImageInput,
        box: Box | None,
        points: Sequence[tuple[int, int]],
        object_id: int,
        emit: Emit | None = None,
    ) -> WaterOutcome:
        out = WaterOutcome(False, None, 0)
        pts = tuple(points)
        # Regions the verifier confirmed as water but called partial; the final
        # mask is their union with the accepted one, so a refinement towards
        # other water never throws away water already confirmed.
        confirmed: list[ObjectMask] = []
        for attempt in range(self.max_refinements + 1):
            pp = tuple(PointPrompt(*p) for p in pts)
            if box is None and not pp:
                out.verdict = "no prompt on the water"
                return out
            mask, ms = await self._segment(image, ObjectPrompt(object_id, pp, box))
            prompt_text = f"box {_fmt_box(box)}" + (
                f" + points {[list(p) for p in pts]}" if pts else ""
            )
            share = 0.0 if mask is None else mask.area / float(image.width * image.height)
            self._log(
                out.trace,
                TraceStep(
                    "segment",
                    "water",
                    self.mask_name,
                    prompt_text,
                    "empty mask"
                    if mask is None
                    else f"mask {mask.area} px ({100 * share:.0f}% of photo)",
                    "verify",
                    ms,
                ),
                emit,
            )
            jpeg = await asyncio.to_thread(
                render_water, image.rgb, mask.mask if mask else None, box, 768
            )
            self._keep(jpeg, f"water_try{attempt}.jpg")
            try:
                reply = await self._ask_checked(
                    jpeg,
                    water_verify_prompt(),
                    what="verify water",
                    target="water",
                    check=check_water_answer,
                    trace=out.trace,
                    emit=emit,
                    bill=out,
                )
            except ModelError as exc:
                budget = isinstance(exc, BudgetSpent)
                if not isinstance(exc, AnswerRejected):
                    self._log(
                        out.trace,
                        TraceStep(
                            "verify",
                            "water",
                            self.scene_name,
                            "photo with the region tinted",
                            f"error: {exc}",
                            "give up: the region an earlier check confirmed as water is used"
                            if confirmed and not budget
                            else "give up",
                            0,
                            VERIFY_WATER_VERSION,
                        ),
                        emit,
                    )
                out.verdict = _failed(exc, "verification failed")
                if confirmed:
                    # A later check failed (out of time, no answer, or answers that
                    # failed their checks): the water an earlier check already
                    # confirmed, with partial coverage, is used, as it would have
                    # been had no refinement been asked for. On u_w8MXURXvqjQ a
                    # timed-out refinement check threw away confirmed water and
                    # with it an accepted wheel.
                    union = confirmed[0].mask.copy()
                    for c in confirmed[1:]:
                        union |= c.mask
                    first = confirmed[0]
                    out.accepted = True
                    out.mask = ObjectMask(
                        first.object_id, 0, union, mask_bbox(union) or first.box, first.confidence
                    )
                    why = "time budget reached" if budget else "a later check failed"
                    out.verdict = f"partial ({why})"
                return out
            d = reply.data
            ok = _truthy(d.get("is_water"))
            coverage = str(d.get("coverage") or ("good" if ok else "wrong")).lower()
            reason = str(d.get("reason") or "").strip()[:160]
            gb = grid_box(d.get("corrected_box"))
            gp = grid_point(d.get("corrected_point"))
            nbox = None
            if gb is not None:
                x0, y0 = round(gb[0] * image.width / 1000), round(gb[1] * image.height / 1000)
                x1, y1 = round(gb[2] * image.width / 1000), round(gb[3] * image.height / 1000)
                nbox = (x0, y0, max(x1, x0 + 2), max(y1, y0 + 2))
            npt = None
            if gp is not None:
                npt = (
                    min(round(gp[0] * image.width / 1000), image.width - 1),
                    min(round(gp[1] * image.height / 1000), image.height - 1),
                )
            can_refine = attempt < self.max_refinements and (nbox is not None or npt is not None)
            if mask is not None and ok and (coverage == "good" or not can_refine):
                decision = "accept" if coverage == "good" else "accept (coverage partial)"
            elif can_refine:
                decision = f"refine with box {_fmt_box(nbox)}" + (
                    f" + point {list(npt)}" if npt else ""
                )
            else:
                decision = "give up"
            self._log(
                out.trace,
                TraceStep(
                    "verify",
                    "water",
                    self.scene_name,
                    "photo with the proposed region tinted",
                    f"is_water={str(ok).lower()}, coverage {coverage}"
                    + (f"; {reason}" if reason else ""),
                    decision,
                    reply.latency_ms,
                    VERIFY_WATER_VERSION,
                ),
                emit,
            )
            out.refinements = attempt
            if decision.startswith("accept") and mask is not None:
                if confirmed:
                    union = mask.mask.copy()
                    for c in confirmed:
                        union |= c.mask
                    mask = ObjectMask(
                        mask.object_id, 0, union, mask_bbox(union) or mask.box, mask.confidence
                    )
                out.accepted, out.mask, out.verdict = True, mask, coverage
                return out
            if ok and mask is not None:
                confirmed.append(mask)
            if decision == "give up":
                out.verdict = reason or "not water"
                return out
            box = nbox
            pts = (npt,) if npt is not None else ()
        return out

    async def find_vehicle_wheel(
        self,
        image: ImageInput,
        vehicle: VehicleObject,
        kind: RefKind,
        index: int,
        object_id: int,
        emit: Emit | None = None,
        hints: Sequence[SceneObject] = (),
    ) -> RefOutcome:
        """A wheel of ``vehicle`` found by SAM from a point grid and dark blobs in the
        lower part of its box, ranked by wheel score, then verified as usual.

        ``hints`` are the scene model's own wheels inside this vehicle's box. Their
        boxes and tyre points are prompted too, alongside the grid, and the search
        region is grown to hold them: on a close-up of a car cut by the frame the
        tyre was above the lower part of the box (u_ic8V3A59gpg), and the scene
        model's box around it was the best prompt there was."""
        target = f"{vehicle.kind.replace('_', ' ')} {index} wheel"
        lower = wheel_region(vehicle.box, image.width, image.height)
        hint_boxes = [h.box for h in hints if h.box is not None]
        region = grow_region(lower, hint_boxes, image.width, image.height)
        vw = vehicle.box[2] - vehicle.box[0]
        t0 = time.perf_counter()
        grid = grid_points(lower)
        blobs = await self._in_budget(asyncio.to_thread(dark_ellipse_boxes, image.rgb, region, vw))
        prompts: list[tuple[str, Box | None, Sequence[PointPrompt]]] = [
            ("grid", None, (PointPrompt(*p),)) for p in grid
        ] + [("dark blob", b, ()) for b in blobs]
        for h in hints:
            if h.box is None:
                continue
            ring = _initial_points(h, h.box)
            prompts.append(("scene wheel", h.box, ()))
            if ring:
                prompts.append(("scene wheel", h.box, ring))
        trace: list[TraceStep] = []
        many = getattr(self.masks, "region_candidates", None)
        raw: list[NDArray[np.bool_]] = []
        if many is not None:
            raw = [c.mask for c in await self._in_budget(many(image, region, prompts))]
        else:  # one request per prompt
            for _, b, p in prompts:
                one, _ = await self._segment(image, ObjectPrompt(object_id, tuple(p), b))
                if one is not None:
                    raw.append(one.mask)

        def rank() -> list[tuple[WheelScore, NDArray[np.bool_]]]:
            keep = dedupe(raw)
            scored = [(score_wheel(image.rgb, raw[i], region, vw), raw[i]) for i in keep]
            scored = [t for t in scored if t[0].score > 0]
            scored.sort(key=lambda t: -t[0].score)
            # Outlines the numbers already rule out (rim, cut oval, wrong size) go
            # last, so the verifier sees the plausible tyres first.
            failed = {
                id(m)
                for _, m in scored[:12]
                if tyre_failure(tyre_evidence(image.rgb, solid_wheel(m)[0]), vehicle.kind, vw)
            }
            scored.sort(key=lambda t: id(t[1]) in failed)
            best: list[tuple[WheelScore, NDArray[np.bool_]]] = []
            for sc, m in scored:  # two distinct wheels or outlines
                if all(mask_iou(m, b) < 0.5 for _, b in best):
                    best.append((sc, m))
                if len(best) == 2:
                    break
            return best

        best = await self._in_budget(asyncio.to_thread(rank))
        out_text = (
            f"{_count(len(raw), 'proposal')} from {_count(len(grid), 'grid point')}"
            + (
                f", {len(hint_boxes)} scene model wheel box" + ("es" if len(hint_boxes) > 1 else "")
                if hint_boxes
                else ""
            )
            + f" and {_count(len(blobs), 'dark blob')}; "
            + ("best: " + "; ".join(sc.text() for sc, _ in best) if best else "none wheel-like")
        )
        self._log(
            trace,
            TraceStep(
                "segment",
                target,
                self.mask_name,
                f"lower part of the vehicle box {_fmt_box(region)}",
                out_text,
                "verify the best " + ("two" if len(best) == 2 else "one") if best else "give up",
                round(1000 * (time.perf_counter() - t0)),
            ),
            emit,
        )
        if not best:
            obj = SceneObject(kind, (0, 0), vehicle.box, vehicle=vehicle.vehicle)
            out = RefOutcome(index, obj, False, None, None, 0, trace=trace)
            out.verdict = "no wheel-like proposal in the vehicle box"
            return out
        seed = []
        for sc, wheel in best:
            solid, _ = solid_wheel(wheel)
            seed.append(self._proposal(image, solid, sc.shape, "vehicle search", vehicle))
        bb = mask_bbox(seed[0].mask) or vehicle.box
        obj = SceneObject(
            kind, ((bb[0] + bb[2]) // 2, (bb[1] + bb[3]) // 2), bb, vehicle=vehicle.vehicle
        )
        return await self.refine_reference(
            image,
            obj,
            index,
            object_id,
            emit,
            seed=seed,
            target=target,
            trace=trace,
            vehicle=vehicle,
        )

    async def run(
        self,
        image: ImageInput,
        refs: Sequence[tuple[int, SceneObject, int]],
        water_box: Box | None,
        water_points: Sequence[tuple[int, int]],
        water_id: int,
        emit: Emit | None = None,
        vehicles: Sequence[tuple[int, VehicleObject, RefKind, int]] = (),
        deadline: float | None = None,
        vehicle_hints: Mapping[int, Sequence[SceneObject]] | None = None,
    ) -> tuple[WaterOutcome, list[RefOutcome]]:
        """Water, every vehicle's wheel and every other reference, verified
        concurrently (SAM itself runs one request at a time). Outcomes come back in
        the order: vehicles, then references. With ``deadline`` (a
        ``time.perf_counter()`` value), no check runs past it: what is not
        verified by then is not accepted (verdict "time budget reached").
        ``vehicle_hints`` maps a vehicle's index to the scene model's wheels inside
        its box (see :meth:`find_vehicle_wheel`)."""
        token = _DEADLINE.set(deadline)
        try:
            return await self._run(
                image, refs, water_box, water_points, water_id, emit, vehicles, vehicle_hints or {}
            )
        finally:
            _DEADLINE.reset(token)

    async def _run(
        self,
        image: ImageInput,
        refs: Sequence[tuple[int, SceneObject, int]],
        water_box: Box | None,
        water_points: Sequence[tuple[int, int]],
        water_id: int,
        emit: Emit | None,
        vehicles: Sequence[tuple[int, VehicleObject, RefKind, int]],
        vehicle_hints: Mapping[int, Sequence[SceneObject]],
    ) -> tuple[WaterOutcome, list[RefOutcome]]:
        spent = "time budget reached"

        async def water_job() -> WaterOutcome:
            try:
                return await self.refine_water(image, water_box, water_points, water_id, emit)
            except BudgetSpent:
                return WaterOutcome(False, None, 0, spent)

        async def vehicle_job(idx: int, v: VehicleObject, kind: RefKind, oid: int) -> RefOutcome:
            try:
                return await self.find_vehicle_wheel(
                    image, v, kind, idx, oid, emit, hints=vehicle_hints.get(idx, ())
                )
            except BudgetSpent:
                centre = ((v.box[0] + v.box[2]) // 2, (v.box[1] + v.box[3]) // 2)
                obj = SceneObject(kind, centre, v.box, vehicle=v.vehicle)
                return RefOutcome(idx, obj, False, None, v.box, 0, verdict=spent)

        async def ref_job(idx: int, obj: SceneObject, oid: int) -> RefOutcome:
            try:
                return await self.refine_reference(image, obj, idx, oid, emit)
            except BudgetSpent:
                return RefOutcome(idx, obj, False, None, obj.box, 0, verdict=spent)

        jobs = [vehicle_job(idx, v, kind, oid) for idx, v, kind, oid in vehicles]
        jobs += [ref_job(idx, obj, oid) for idx, obj, oid in refs]
        water, *outs = await asyncio.gather(water_job(), *jobs)
        assert isinstance(water, WaterOutcome)
        return water, [r for r in outs if isinstance(r, RefOutcome)]
