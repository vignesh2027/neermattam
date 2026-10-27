"""Photo (or short video) in, :class:`MeasureResponse` out.

Steps:
  1. The scene model reads the photo: water present, reference objects with a
     box and point on each, where the water meets them, its own depth
     estimate, floating debris, image quality.
  2. Masks for every candidate wheel, its rim and the water. On the hosted
     path SAM 3.1 segments them all in one request. With the verify-and-refine
     agent (the open-weights path) each mask is checked by the scene model on a
     crop and its prompt corrected up to twice (``agent.py``).
  3. The geometry measures depth on each accepted wheel; the most certain wins.
  4. The scene model's waterline, measured on the same fitted wheel, is
     compared with the mask waterline (``agreement_cm``); a disagreement widens
     the sigma.
  5. When no wheel can be measured, the scene model's own estimate is returned
     as ``method: model_estimate`` with a wide sigma. When both exist, both are
     reported and the geometry is used.
  6. With a video, SAM 3.1 tracks the floating debris and the wheel's scale
     turns the tracks into a surface velocity.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field

import cv2
import numpy as np

from .adapters import MaskModel, ModelError, SceneModel, SceneObject, SceneReading
from .agent import TraceStep, VerifyRefineAgent
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
from .models import (
    AgentStepOut,
    MeasureResponse,
    ModelEstimateOut,
    ModelsOut,
    ReferenceOut,
    WaterlineOut,
)
from .overlay import png_b64, render_overlay
from .references import WHEEL_KINDS, ReferenceChoice, RefKind, resolve_reference
from .sam import MAX_PROMPTED_OBJECTS, ObjectMask, ObjectPrompt, PointPrompt
from .velocity import VelocityEstimate, surface_velocity

KIND_TEXT = {
    RefKind.TWO_WHEELER_WHEEL: "two-wheeler wheel",
    RefKind.CAR_WHEEL: "car wheel",
    RefKind.AUTO_WHEEL: "auto-rickshaw wheel",
    RefKind.BUS_WHEEL: "bus wheel",
    RefKind.KERB: "kerb",
    RefKind.PERSON: "person",
}

# A scene-model estimate is a judgement by eye, not a measurement: its sigma is
# at least this many centimetres, or this share of the estimate, whichever is
# larger (and at least half of any range the model gave).
ESTIMATE_MIN_SIGMA_CM = 20.0
ESTIMATE_REL_SIGMA = 0.30
# Largest gap, in pixels, closed between a tyre and the water segmented just
# below it (SAM's mask edges are soft by a few pixels at this resolution).
WATER_SEAM_PX = 3


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
    index: int
    wheel_id: int
    rim_id: int | None
    is_first: bool
    wheel: ObjectMask | None = None
    rim: ObjectMask | None = None
    waterline_points: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True, slots=True)
class ModelEstimate:
    depth_cm: float
    sigma_cm: float
    basis: str


def _confidence(quality: float, sigma: float, diameter: float, agreement: float | None) -> float:
    """A 0 to 1 ranking score (not a probability): image quality, relative sigma, agreement."""
    rel = sigma / max(diameter, 1.0)
    c_meas = 1.0 / (1.0 + (rel / 0.05) ** 2)
    c_agree = 1.0
    if agreement is not None:
        c_agree = 1.0 / (1.0 + (agreement / max(2.0 * sigma, 2.0)) ** 2)
    return round(math.sqrt(max(0.0, min(1.0, quality))) * c_meas * (0.5 + 0.5 * c_agree), 2)


def estimate_sigma(depth_cm: float, low: float | None = None, high: float | None = None) -> float:
    sigma = max(ESTIMATE_MIN_SIGMA_CM, ESTIMATE_REL_SIGMA * abs(depth_cm))
    if low is not None and high is not None and high > low:
        sigma = max(sigma, (high - low) / 2.0)
    return sigma


def model_estimate(reading: SceneReading) -> ModelEstimate | None:
    """The scene model's own depth estimate for the street, when it gave one."""
    if reading.overall_depth_cm is not None and reading.overall_depth_cm >= 0:
        d = reading.overall_depth_cm
        basis = reading.overall_reason or "the scene model's overall visual estimate"
        return ModelEstimate(
            d, estimate_sigma(d, reading.overall_low_cm, reading.overall_high_cm), basis
        )
    per_ref = [r for r in reading.references if r.depth_cm is not None and r.depth_cm >= 0]
    if not per_ref:
        return None
    d = float(np.median([r.depth_cm for r in per_ref if r.depth_cm is not None]))
    basis = per_ref[0].depth_reason or f"visual estimate at a {KIND_TEXT[per_ref[0].kind]}"
    return ModelEstimate(d, estimate_sigma(d), basis)


def second_line(points: Sequence[tuple[int, int]], m: DepthMeasurement) -> Line | None:
    pts = [np.array(p, dtype=np.float64) for p in points]
    if len(pts) >= 2 and np.linalg.norm(pts[1] - pts[0]) > 3.0:
        d = pts[1] - pts[0]
        d /= np.linalg.norm(d)
        if d[0] < 0:
            d = -d
        return Line((pts[0] + pts[1]) / 2.0, d)
    if len(pts) == 1:  # one point: a level estimate, parallel to the mask waterline
        return Line(pts[0], m.waterline.line.direction)
    return None


def _scene_decision(reading: SceneReading, has_estimate: bool) -> str:
    if not reading.water_visible:
        return "stop: no water"
    n = sum(1 for r in reading.references if r.kind in WHEEL_KINDS)
    if n:
        return f"segment and verify {n} wheel{'s' if n > 1 else ''} and the water"
    return "no wheel: report own estimate" if has_estimate else "stop: no wheel"


def _ms(t0: float) -> int:
    return round(1000 * (time.perf_counter() - t0))


class Pipeline:
    def __init__(
        self,
        scene: SceneModel,
        masks: MaskModel,
        config: PipelineConfig | None = None,
        agent: VerifyRefineAgent | None = None,
    ):
        self.scene = scene
        self.masks = masks
        self.config = config or PipelineConfig()
        self.agent = agent

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

    # ------------------------------------------------------------------ helpers

    def _estimate_out(self, est: ModelEstimate | None) -> ModelEstimateOut | None:
        if est is None:
            return None
        return ModelEstimateOut(
            depth_cm=round(est.depth_cm, 1),
            depth_sigma_cm=round(est.sigma_cm, 1),
            basis=est.basis,
            model=self.model_names.scene,
        )

    def _response(
        self,
        key: ImageInput,
        trace: list[TraceStep],
        timings: dict[str, int],
        cost: float,
        **kw: object,
    ) -> MeasureResponse:
        return MeasureResponse(
            models=self.model_names,
            timings_ms=timings,
            cost_usd=round(cost, 6),
            agent_trace=[AgentStepOut(**asdict(t)) for t in trace],
            captured_at=key.captured_at,
            **kw,
        )

    def _unusable(
        self,
        key: ImageInput,
        reasons: Sequence[str],
        trace: list[TraceStep],
        timings: dict[str, int],
        cost: float,
        est: ModelEstimate | None = None,
    ) -> MeasureResponse:
        return self._response(
            key,
            trace,
            timings,
            cost,
            usable=False,
            reasons=list(dict.fromkeys(reasons)),
            model_estimate=self._estimate_out(est),
        )

    def _estimate_only(
        self,
        key: ImageInput,
        reasons: list[str],
        trace: list[TraceStep],
        timings: dict[str, int],
        cost: float,
        est: ModelEstimate,
        quality: float,
        water: ObjectMask | None = None,
        wheels: Sequence[ObjectMask] = (),
    ) -> MeasureResponse:
        """No wheel could be measured: the scene model's estimate, clearly labelled."""
        reasons = [
            *reasons,
            "no wheel could be measured; depth is the scene model's visual estimate, "
            "not a measurement",
            f"estimate based on: {est.basis}",
        ]
        trace.append(
            TraceStep(
                "estimate",
                "photo",
                self.model_names.scene,
                "no measurable wheel",
                f"{est.depth_cm:.0f} cm, sigma {est.sigma_cm:.0f} cm",
                "report model estimate",
                0,
            )
        )
        label = f"estimate {est.depth_cm:.0f} cm, sigma {est.sigma_cm:.0f} cm (not measured)"
        overlay = png_b64(
            render_overlay(
                key.rgb,
                None,
                label=label,
                water_mask=None if water is None else water.mask,
                object_masks=[w.mask for w in wheels],
            )
        )
        return self._response(
            key,
            trace,
            timings,
            cost,
            usable=True,
            reasons=list(dict.fromkeys(reasons)),
            depth_cm=round(est.depth_cm, 1),
            depth_sigma_cm=round(est.sigma_cm, 1),
            confidence=round(0.25 * math.sqrt(max(0.0, min(1.0, quality))), 2),
            overlay_png_b64=overlay,
            method="model_estimate",
            model_estimate=self._estimate_out(est),
        )

    # ------------------------------------------------------------------ measure

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
        trace: list[TraceStep] = []
        cost = 0.0
        reasons: list[str] = []
        hint_kind = RefKind(hint_reference) if hint_reference else None
        scene_name = self.model_names.scene

        # Model failures (no key, API down, weights not loaded) raise ModelError
        # to the caller: they are service faults, not a verdict on the photo.
        t0 = time.perf_counter()
        reading = await self.scene.read_scene(key)
        timings["scene"] = _ms(t0)
        cost += reading.cost_usd
        est = model_estimate(reading)
        kinds = ", ".join(r.kind.value for r in reading.references) or "none"
        trace.append(
            TraceStep(
                "scene",
                "photo",
                scene_name,
                f"photo {key.width}x{key.height} px",
                f"water {'visible' if reading.water_visible else 'not visible'}; references: "
                f"{kinds}" + (f"; own estimate {est.depth_cm:.0f} cm" if est is not None else ""),
                _scene_decision(reading, est is not None),
                timings["scene"],
            )
        )

        q = reading.quality
        if q.score < cfg.min_quality or (q.blurred and q.score < 0.5):
            return self._unusable(
                key, ["image is too blurred or dark to measure", *q.issues], trace, timings, cost
            )
        if not reading.water_visible:
            return self._unusable(key, ["no standing water visible"], trace, timings, cost)
        reasons.append("standing water visible")
        if hint_kind in (RefKind.KERB, RefKind.PERSON):
            if est is not None:
                return self._estimate_only(key, reasons, trace, timings, cost, est, q.score)
            return self._unusable(
                key,
                [
                    *reasons,
                    f"depth from a {KIND_TEXT[hint_kind]} is not supported yet; "
                    "photograph a vehicle wheel",
                ],
                trace,
                timings,
                cost,
            )

        wheels = [r for r in reading.references if r.kind in WHEEL_KINDS]
        if hint_kind is not None and any(r.kind == hint_kind for r in wheels):
            wheels = [r for r in wheels if r.kind == hint_kind]  # the reporter knows best
        wheels = wheels[: cfg.max_wheels]
        if not wheels:
            if est is not None:
                return self._estimate_only(
                    key,
                    [*reasons, "no wheel found to measure against"],
                    trace,
                    timings,
                    cost,
                    est,
                    q.score,
                )
            return self._unusable(
                key, [*reasons, "no wheel found to measure against"], trace, timings, cost
            )
        if not reading.water_points and reading.water_box is None:
            return self._unusable(
                key, [*reasons, "could not locate the water surface"], trace, timings, cost, est
            )

        cands: list[_Candidate] = []
        for i, wref in enumerate(wheels):
            rid = None
            if wref.rim_point is not None and wref.rim_point != wref.point:
                rid = 2 + 2 * i
            cands.append(_Candidate(wref, i + 1, 1 + 2 * i, rid, wref is reading.references[0]))
        water_id = MAX_PROMPTED_OBJECTS

        t0 = time.perf_counter()
        if self.agent is not None:
            water, failures, extra_cost = await self._agent_masks(
                key, reading, cands, water_id, trace
            )
            timings["agent"] = _ms(t0)
        else:
            water, failures, extra_cost = await self._single_request(
                key, reading, cands, water_id, trace
            )
            timings["masks"] = _ms(t0)
        cost += extra_cost
        if water is None or water.area == 0:
            if est is not None:
                return self._estimate_only(
                    key,
                    [*reasons, *failures, "water could not be segmented"],
                    trace,
                    timings,
                    cost,
                    est,
                    q.score,
                )
            return self._unusable(
                key, [*reasons, *failures, "water could not be segmented"], trace, timings, cost
            )

        camera = None
        if cfg.perspective == "exif" and key.focal_35mm:
            camera = PinholeCamera.from_35mm(key.focal_35mm, key.width, key.height)

        t0 = time.perf_counter()
        best: tuple[DepthMeasurement, _Candidate, ReferenceChoice] | None = None
        jobs: list[tuple[_Candidate, ReferenceChoice]] = []
        for cand in cands:
            wm = cand.wheel
            if wm is None or wm.area == 0:
                if self.agent is None:
                    failures.append(
                        f"{KIND_TEXT[cand.obj.kind]} could not be segmented "
                        "(it may be fully submerged)"
                    )
                continue
            ref = resolve_reference(
                cand.obj.kind,
                vehicle=cand.obj.vehicle,
                position=cand.obj.position,
                hint_diameter_cm=hint_diameter_cm,
            )
            jobs.append((cand, ref))

        async def measure_one(
            cand: _Candidate, ref: ReferenceChoice
        ) -> tuple[DepthMeasurement | DepthFailure, int]:
            assert cand.wheel is not None and water is not None
            tg = time.perf_counter()
            # CPU-bound: run it off the event loop so other requests and the
            # health checks are not stalled behind it. The wheels of one photo
            # are measured at the same time, on separate threads.
            res = await asyncio.to_thread(
                measure_wheel_depth,
                cand.wheel.mask,
                water.mask,
                ref.diameter_cm,
                ref.diameter_sigma_cm,
                cfg.geometry,
                camera=camera,
                rim_mask=cand.rim.mask if cand.rim is not None else None,
            )
            return res, _ms(tg)

        results = await asyncio.gather(*(measure_one(c, r) for c, r in jobs))
        for (cand, ref), (res, ms) in zip(jobs, results, strict=True):
            target = f"{cand.obj.kind.value.replace('_', ' ')} {cand.index}"
            if isinstance(res, DepthFailure):
                failures.append(f"{KIND_TEXT[cand.obj.kind]}: {res.text}")
                trace.append(
                    TraceStep(
                        "geometry",
                        target,
                        "geometry",
                        "wheel and water masks",
                        res.text,
                        "rejected",
                        ms,
                    )
                )
                continue
            trace.append(
                TraceStep(
                    "geometry",
                    target,
                    "geometry",
                    "wheel" + (", rim" if cand.rim is not None else "") + " and water masks",
                    f"depth {res.depth_cm:.1f} cm, sigma {res.depth_sigma_cm:.1f} cm, "
                    f"{res.arc_covered_deg:.0f} degrees of rim visible",
                    "candidate",
                    ms,
                )
            )
            if best is None or res.depth_sigma_cm < best[0].depth_sigma_cm:
                best = (res, cand, ref)
        timings["geometry"] = _ms(t0)
        if best is None:
            if est is not None:
                return self._estimate_only(
                    key,
                    [*reasons, *failures],
                    trace,
                    timings,
                    cost,
                    est,
                    q.score,
                    water,
                    [c.wheel for c in cands if c.wheel is not None],
                )
            return self._unusable(key, [*reasons, *failures], trace, timings, cost)

        m, cand, ref = best
        # A loaded tyre's fitted circle reaches below the road by its squash;
        # the correction carries its own uncertainty.
        depth_cm = max(0.0, m.depth_cm - ref.squash_cm)
        sigma = math.hypot(m.depth_sigma_cm, ref.squash_sigma_cm)
        reasons.append(f"{KIND_TEXT[cand.obj.kind]} found")
        if ref.source.startswith("catalogue"):
            reasons.append(f"wheel size from catalogue: {ref.label}")
        elif ref.source.startswith("prior"):
            reasons.append("wheel size unknown, generic prior used")
        if ref.squash_cm > 0:
            reasons.append(
                f"tyre squash of {ref.squash_cm:.1f} cm subtracted "
                f"(sigma {ref.squash_sigma_cm:.1f} cm)"
            )
        agreement = None
        wl_pts = cand.waterline_points or cand.obj.waterline_points
        if not wl_pts and cand.is_first:
            wl_pts = reading.waterline_points
        line2 = second_line(wl_pts, m) if wl_pts else None
        if line2 is not None:
            agreement = abs(depth_from_line(m, line2, camera) - m.depth_cm)
            # Two waterlines that disagree mean one of them is wrong and we
            # cannot tell which: the difference widens the sigma.
            sigma = math.hypot(sigma, agreement)
            reasons.append(f"scene model waterline agrees within {agreement:.1f} cm")
        confidence = _confidence(q.score, sigma, ref.diameter_cm, agreement)

        if est is not None:
            local = cand.obj.depth_cm if cand.obj.depth_cm is not None else est.depth_cm
            local_sigma = estimate_sigma(local)
            combined = math.hypot(sigma, local_sigma)
            if abs(depth_cm - local) > 2.0 * combined:
                confidence = round(confidence * 0.5, 2)
                reasons.append(
                    f"the scene model's own estimate ({local:.0f} cm) disagrees with the "
                    "measurement by more than two sigma; confidence lowered"
                )
            trace.append(
                TraceStep(
                    "estimate",
                    "photo",
                    "geometry",
                    f"measurement {depth_cm:.1f} cm and model estimate {local:.0f} cm",
                    f"difference {abs(depth_cm - local):.0f} cm, two combined sigma "
                    f"{2.0 * combined:.0f} cm",
                    "use the measurement",
                    0,
                )
            )

        velocity: VelocityEstimate | None = None
        if video is not None:
            t0 = time.perf_counter()
            velocity, why = await self._velocity(video, reading, m, ref)
            timings["velocity"] = _ms(t0)
            cost += why[1]
            if why[0]:
                reasons.append(why[0])

        label = f"depth {depth_cm:.1f} cm, sigma {sigma:.1f} cm"
        overlay = png_b64(
            await asyncio.to_thread(
                render_overlay,
                key.rgb,
                m,
                label=label,
                second_line=line2,
                water_mask=water.mask,
                object_masks=[c.wheel.mask for c in cands if c.wheel is not None],
            )
        )
        return self._response(
            key,
            trace,
            timings,
            cost,
            usable=True,
            reasons=reasons,
            depth_cm=round(depth_cm, 1),
            depth_sigma_cm=round(sigma, 1),
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
            confidence=confidence,
            overlay_png_b64=overlay,
            method="wheel_geometry",
            model_estimate=self._estimate_out(est),
        )

    async def _single_request(
        self,
        key: ImageInput,
        reading: SceneReading,
        cands: list[_Candidate],
        water_id: int,
        trace: list[TraceStep],
    ) -> tuple[ObjectMask | None, list[str], float]:
        """Every wheel, rim and the water in one mask request (the hosted path)."""
        prompts: list[ObjectPrompt] = []
        for c in cands:
            prompts.append(ObjectPrompt(c.wheel_id, (PointPrompt(*c.obj.point),), c.obj.box))
            if c.rim_id is not None and c.obj.rim_point is not None:
                prompts.append(ObjectPrompt(c.rim_id, (PointPrompt(*c.obj.rim_point),)))
        pts = reading.water_points or ()
        prompts.append(
            ObjectPrompt(water_id, tuple(PointPrompt(*p) for p in pts), reading.water_box)
        )
        t0 = time.perf_counter()
        seg = await self.masks.segment(key, prompts)
        for c in cands:
            c.wheel = seg.masks.get(c.wheel_id)
            c.rim = seg.masks.get(c.rim_id) if c.rim_id is not None else None
        water = seg.masks.get(water_id)
        trace.append(
            TraceStep(
                "segment",
                "all objects",
                self.model_names.masks,
                f"{len(prompts)} prompts in one request",
                f"{sum(1 for m in seg.masks.values() if m.area > 0)} masks",
                "measure",
                _ms(t0),
            )
        )
        return water, [], seg.cost_usd

    async def _agent_masks(
        self,
        key: ImageInput,
        reading: SceneReading,
        cands: list[_Candidate],
        water_id: int,
        trace: list[TraceStep],
    ) -> tuple[ObjectMask | None, list[str], float]:
        """Masks through the verify-and-refine agent; rims only inside accepted wheels."""
        assert self.agent is not None
        refs = [(c.index, c.obj, c.wheel_id) for c in cands]
        water_out, ref_outs = await self.agent.run(
            key, refs, reading.water_box, reading.water_points, water_id
        )
        trace.extend(water_out.trace)
        failures: list[str] = []
        rim_prompts: list[ObjectPrompt] = []
        for c, out in zip(cands, ref_outs, strict=True):
            trace.extend(out.trace)
            if not out.accepted or out.mask is None:
                failures.append(
                    f"{KIND_TEXT[c.obj.kind]} {c.index}: the scene model did not confirm the "
                    f"mask ({out.verdict or 'no mask'})"
                )
                continue
            c.wheel = out.mask
            c.waterline_points = out.waterline_points
            rp = c.obj.rim_point
            if c.rim_id is not None and rp is not None and out.mask.mask[rp[1], rp[0]]:
                rim_prompts.append(ObjectPrompt(c.rim_id, (PointPrompt(*rp),)))
        if rim_prompts:
            t0 = time.perf_counter()
            seg = await self.masks.segment(key, rim_prompts)
            for c in cands:
                if c.rim_id is not None and c.rim_id in seg.masks:
                    c.rim = seg.masks[c.rim_id]
            trace.append(
                TraceStep(
                    "segment",
                    "rims",
                    self.model_names.masks,
                    f"{len(rim_prompts)} rim points inside accepted wheels",
                    f"{len(seg.masks)} rim masks (used only inside the wheel mask)",
                    "measure",
                    _ms(t0),
                )
            )
        water = water_out.mask if water_out.accepted else None
        if water is None:
            failures.append(f"water mask not confirmed ({water_out.verdict})")
        else:
            seen = [
                c for c, o in zip(cands, ref_outs, strict=True) if c.wheel and o.waterline_visible
            ]
            if seen:
                water = await self._water_at_wheels(key, water, seen, trace)
        spent = water_out.cost_usd + sum(o.cost_usd for o in ref_outs)
        return water, failures, spent

    async def _water_at_wheels(
        self,
        key: ImageInput,
        water: ObjectMask,
        wheels: Sequence[_Candidate],
        trace: list[TraceStep],
    ) -> ObjectMask:
        """Extend the verified water mask up to the wheels it should touch.

        SAM tends to leave out the darker, reflective strip of water right at an
        object, so the verified water stops a few pixels short of a tyre and the
        geometry finds no contact. For each accepted wheel on which the verifier
        saw the waterline, water is segmented in a band just below the wheel
        (box from slightly above its lowest point to a third of its height below,
        positive points under it, a negative point on the wheel), kept inside that
        band, and joined to the verified water; a seam of up to
        ``WATER_SEAM_PX`` pixels between band and tyre is closed.
        """
        h, w = water.mask.shape
        prompts = []
        for i, c in enumerate(wheels):
            assert c.wheel is not None
            ys, xs = np.nonzero(c.wheel.mask)
            x0, x1, y0, y1 = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
            ww, hh = x1 - x0 + 1, y1 - y0 + 1
            band = (
                max(0, round(x0 - 0.2 * ww)),
                max(0, round(y1 - 0.05 * hh)),
                min(w, round(x1 + 0.2 * ww)),
                min(h, round(y1 + 0.35 * hh)),
            )
            below = min(h - 1, round(y1 + 0.12 * hh))
            pts = (
                PointPrompt(round(x0 + 0.3 * ww), below),
                PointPrompt(round(x0 + 0.7 * ww), below),
                PointPrompt(int(xs.mean()), int(ys.mean()), positive=False),
            )
            prompts.append((c, band, ObjectPrompt(40 + i, pts, band)))
        t0 = time.perf_counter()
        seg = await self.masks.segment(key, [p for _, _, p in prompts])
        out = water.mask.copy()
        added = 0
        kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (2 * WATER_SEAM_PX + 1, 2 * WATER_SEAM_PX + 1)
        )
        wheel_px = np.zeros_like(out)
        for c, _, _ in prompts:
            assert c.wheel is not None
            wheel_px |= c.wheel.mask
        for _c, band, p in prompts:
            m = seg.masks.get(p.object_id)
            if m is None:
                continue
            inside = np.zeros_like(out)
            inside[band[1] : band[3], band[0] : band[2]] = True
            local = m.mask & inside
            seam = cv2.dilate(local.astype(np.uint8), kernel).astype(bool) & inside & ~wheel_px
            new = (local | seam) & ~out
            added += int(new.sum())
            out |= local | seam
        trace.append(
            TraceStep(
                "segment",
                "water at wheels",
                self.model_names.masks,
                f"{len(prompts)} bands just below wheels where the waterline was seen",
                f"{added} px of water added next to the tyres",
                "join to the verified water",
                _ms(t0),
            )
        )
        return ObjectMask(water.object_id, 0, out, water.box, water.confidence)

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
