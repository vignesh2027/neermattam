"""Photo (or short video) in, :class:`MeasureResponse` out.

Steps:
  1. Muse Spark reads the scene: water present, reference wheels with a point
     and box on each, a second waterline, floating debris, image quality.
  2. SAM 3.1 segments every candidate wheel, its rim and the water in one
     request, prompted with Muse Spark's points and boxes.
  3. The geometry measures depth on each wheel; the most certain wins.
  4. Muse Spark's waterline, measured on the same fitted wheel, is compared
     with the mask waterline and reported as ``agreement_cm``.
  5. With a video, SAM 3.1 tracks the floating debris and the wheel's scale
     turns the tracks into a surface velocity.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np

from .adapters import MaskModel, ModelError, SceneModel, SceneObject, SceneReading
from .geometry import (
    DepthFailure,
    DepthMeasurement,
    GeometryConfig,
    Line,
    PinholeCamera,
    depth_from_line,
    measure_wheel_depth,
)
from .media import ImageInput, VideoInput
from .models import MeasureResponse, ModelsOut, ReferenceOut, WaterlineOut
from .overlay import png_b64, render_overlay
from .references import WHEEL_KINDS, ReferenceChoice, RefKind, resolve_reference
from .sam import MAX_PROMPTED_OBJECTS, ObjectPrompt, PointPrompt
from .velocity import VelocityEstimate, surface_velocity

KIND_TEXT = {
    RefKind.TWO_WHEELER_WHEEL: "two-wheeler wheel",
    RefKind.CAR_WHEEL: "car wheel",
    RefKind.AUTO_WHEEL: "auto-rickshaw wheel",
    RefKind.BUS_WHEEL: "bus wheel",
    RefKind.KERB: "kerb",
    RefKind.PERSON: "person",
}


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    geometry: GeometryConfig = field(default_factory=lambda: GeometryConfig(mc_draws=100))
    # "off": affine depth model; "exif": exact perspective correction when the
    # photo carries a 35 mm equivalent focal length. The synthetic evaluation
    # (docs/GEOMETRY.md) did not show a gain from the correction on noisy masks,
    # so it is off by default.
    perspective: str = "off"
    max_wheels: int = 4
    min_quality: float = 0.25


@dataclass
class _Candidate:
    obj: SceneObject
    wheel_id: int
    rim_id: int | None
    is_first: bool


def _confidence(quality: float, sigma: float, diameter: float, agreement: float | None) -> float:
    """A 0 to 1 ranking score (not a probability): image quality, relative sigma, agreement."""
    rel = sigma / max(diameter, 1.0)
    c_meas = 1.0 / (1.0 + (rel / 0.05) ** 2)
    c_agree = 1.0
    if agreement is not None:
        c_agree = 1.0 / (1.0 + (agreement / max(2.0 * sigma, 2.0)) ** 2)
    return round(math.sqrt(max(0.0, min(1.0, quality))) * c_meas * (0.5 + 0.5 * c_agree), 2)


def _second_line(reading: SceneReading, m: DepthMeasurement) -> Line | None:
    pts = [np.array(p, dtype=np.float64) for p in reading.waterline_points]
    if len(pts) >= 2 and np.linalg.norm(pts[1] - pts[0]) > 3.0:
        d = pts[1] - pts[0]
        d /= np.linalg.norm(d)
        if d[0] < 0:
            d = -d
        return Line((pts[0] + pts[1]) / 2.0, d)
    if len(pts) == 1:  # one point: a level estimate, parallel to the mask waterline
        return Line(pts[0], m.waterline.line.direction)
    return None


class Pipeline:
    def __init__(self, scene: SceneModel, masks: MaskModel, config: PipelineConfig | None = None):
        self.scene = scene
        self.masks = masks
        self.config = config or PipelineConfig()

    @property
    def model_names(self) -> ModelsOut:
        return ModelsOut(
            scene=getattr(self.scene, "scene_model_name", self.scene.name),
            masks=getattr(self.masks, "mask_model_name", self.masks.name),
        )

    async def ready(self) -> tuple[bool, str]:
        ok_s, why_s = await self.scene.ready()
        if not ok_s:
            return False, why_s
        if id(self.masks) != id(self.scene):
            ok_m, why_m = await self.masks.ready()
            if not ok_m:
                return False, why_m
        return True, "ok"

    def _unusable(
        self, reasons: Sequence[str], timings: dict[str, int], cost: float, **extra: object
    ) -> MeasureResponse:
        return MeasureResponse(
            usable=False,
            reasons=list(dict.fromkeys(reasons)),
            models=self.model_names,
            timings_ms=timings,
            cost_usd=round(cost, 6),
            **extra,
        )

    async def measure(
        self,
        *,
        image: ImageInput | None = None,
        video: VideoInput | None = None,
        hint_reference: str | None = None,
        hint_diameter_cm: float | None = None,
    ) -> MeasureResponse:
        cfg = self.config
        key = image if image is not None else (video.key_frame if video is not None else None)
        if key is None:
            raise ValueError("an image or a video is required")
        timings: dict[str, int] = {}
        cost = 0.0
        reasons: list[str] = []
        hint_kind = RefKind(hint_reference) if hint_reference else None

        # Model failures (no key, API down, open weights not deployed) raise
        # ModelError to the caller: they are service faults, not a verdict on the photo.
        t0 = time.perf_counter()
        reading = await self.scene.read_scene(key)
        timings["scene"] = round(1000 * (time.perf_counter() - t0))
        cost += reading.cost_usd

        q = reading.quality
        if q.score < cfg.min_quality or (q.blurred and q.score < 0.5):
            return self._unusable(
                ["image is too blurred or dark to measure", *q.issues], timings, cost
            )
        if not reading.water_visible:
            return self._unusable(["no standing water visible"], timings, cost)
        reasons.append("standing water visible")
        if hint_kind in (RefKind.KERB, RefKind.PERSON):
            return self._unusable(
                [
                    *reasons,
                    f"depth from a {KIND_TEXT[hint_kind]} is not supported yet; "
                    "photograph a vehicle wheel",
                ],
                timings,
                cost,
            )

        wheels = [r for r in reading.references if r.kind in WHEEL_KINDS]
        if hint_kind is not None and any(r.kind == hint_kind for r in wheels):
            wheels = [r for r in wheels if r.kind == hint_kind]  # the reporter knows best
        wheels = wheels[: cfg.max_wheels]
        if not wheels:
            return self._unusable([*reasons, "no wheel found to measure against"], timings, cost)
        if not reading.water_points:
            return self._unusable([*reasons, "could not locate the water surface"], timings, cost)

        prompts: list[ObjectPrompt] = []
        cands: list[_Candidate] = []
        for i, wref in enumerate(wheels):
            wid = 1 + 2 * i
            prompts.append(ObjectPrompt(wid, (PointPrompt(*wref.point),), wref.box))
            rid = None
            if wref.rim_point is not None and wref.rim_point != wref.point:
                rid = 2 + 2 * i
                prompts.append(ObjectPrompt(rid, (PointPrompt(*wref.rim_point),)))
            cands.append(_Candidate(wref, wid, rid, wref is reading.references[0]))
        water_id = MAX_PROMPTED_OBJECTS
        prompts.append(ObjectPrompt(water_id, tuple(PointPrompt(*p) for p in reading.water_points)))

        t0 = time.perf_counter()
        seg = await self.masks.segment(key, prompts)
        timings["masks"] = round(1000 * (time.perf_counter() - t0))
        cost += seg.cost_usd
        water = seg.masks.get(water_id)
        if water is None or water.area == 0:
            return self._unusable([*reasons, "water could not be segmented"], timings, cost)

        camera = None
        if cfg.perspective == "exif" and key.focal_35mm:
            camera = PinholeCamera.from_35mm(key.focal_35mm, key.width, key.height)

        t0 = time.perf_counter()
        best: tuple[DepthMeasurement, _Candidate, ReferenceChoice] | None = None
        failures: list[str] = []
        for cand in cands:
            wm = seg.masks.get(cand.wheel_id)
            if wm is None or wm.area == 0:
                failures.append(
                    f"{KIND_TEXT[cand.obj.kind]} could not be segmented (it may be fully submerged)"
                )
                continue
            ref = resolve_reference(
                cand.obj.kind,
                vehicle=cand.obj.vehicle,
                position=cand.obj.position,
                hint_diameter_cm=hint_diameter_cm,
            )
            rim = seg.masks.get(cand.rim_id) if cand.rim_id is not None else None
            # CPU-bound for seconds: run it off the event loop so other requests
            # and the health checks are not stalled behind it.
            res = await asyncio.to_thread(
                measure_wheel_depth,
                wm.mask,
                water.mask,
                ref.diameter_cm,
                ref.diameter_sigma_cm,
                cfg.geometry,
                camera=camera,
                rim_mask=rim.mask if rim is not None else None,
            )
            if isinstance(res, DepthFailure):
                failures.append(f"{KIND_TEXT[cand.obj.kind]}: {res.text}")
                continue
            if best is None or res.depth_sigma_cm < best[0].depth_sigma_cm:
                best = (res, cand, ref)
        timings["geometry"] = round(1000 * (time.perf_counter() - t0))
        if best is None:
            return self._unusable([*reasons, *failures], timings, cost)

        m, cand, ref = best
        # A loaded tyre's fitted circle reaches below the road by its squash.
        depth_cm = max(0.0, m.depth_cm - ref.squash_cm)
        reasons.append(f"{KIND_TEXT[cand.obj.kind]} found")
        if ref.source.startswith("catalogue"):
            reasons.append(f"wheel size from catalogue: {ref.label}")
        elif ref.source.startswith("prior"):
            reasons.append("wheel size unknown, generic prior used")
        agreement = None
        line2 = _second_line(reading, m) if cand.is_first else None
        if line2 is not None:
            agreement = abs(depth_from_line(m, line2, camera) - m.depth_cm)
            reasons.append(f"scene model waterline agrees within {agreement:.1f} cm")

        velocity: VelocityEstimate | None = None
        if video is not None:
            t0 = time.perf_counter()
            velocity, why = await self._velocity(video, reading, m, ref)
            timings["velocity"] = round(1000 * (time.perf_counter() - t0))
            cost += why[1]
            if why[0]:
                reasons.append(why[0])

        label = f"depth {depth_cm:.1f} cm, sigma {m.depth_sigma_cm:.1f} cm"
        overlay = png_b64(
            await asyncio.to_thread(render_overlay, key.rgb, m, label=label, second_line=line2)
        )
        return MeasureResponse(
            usable=True,
            reasons=reasons,
            depth_cm=round(depth_cm, 1),
            depth_sigma_cm=round(m.depth_sigma_cm, 1),
            reference=ReferenceOut(
                kind=ref.kind.value,
                diameter_cm=ref.diameter_cm,
                diameter_sigma_cm=ref.diameter_sigma_cm,
                source=ref.source,
            ),
            waterline=WaterlineOut(
                method=m.waterline.method,
                y_px=round(m.waterline_y_px, 1),
                agreement_cm=None if agreement is None else round(agreement, 1),
            ),
            velocity_mps=None if velocity is None else round(velocity.mean_mps, 2),
            velocity_sigma_mps=None if velocity is None else round(velocity.mean_sigma_mps, 2),
            confidence=_confidence(q.score, m.depth_sigma_cm, ref.diameter_cm, agreement),
            overlay_png_b64=overlay,
            models=self.model_names,
            timings_ms=timings,
            cost_usd=round(cost, 6),
        )

    async def _velocity(
        self, video: VideoInput, reading: SceneReading, m: DepthMeasurement, ref: ReferenceChoice
    ) -> tuple[VelocityEstimate | None, tuple[str, float]]:
        concept = reading.floating_debris or "floating debris"
        try:
            tr = await self.masks.track(video, concept)
        except (ModelError, NotImplementedError) as exc:
            return None, (f"velocity unavailable: {exc}", 0.0)
        # Tracks are in video pixels; the wheel scale is in key-frame pixels.
        k = video.key_frame.width / video.width
        tracks = {tid: [(f, x * k, y * k) for f, x, y in pts] for tid, pts in tr.tracks.items()}
        scale_rel = math.hypot(ref.diameter_sigma_cm / ref.diameter_cm, 0.03)
        est = surface_velocity(tracks, video.fps, m.cm_per_px, scale_rel_sigma=scale_rel)
        if isinstance(est, VelocityEstimate):
            return est, (f"surface velocity from {est.n_used} tracked objects", tr.cost_usd)
        return None, (f"velocity unavailable: {est.reason}", tr.cost_usd)
