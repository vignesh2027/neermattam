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
from collections.abc import Callable, Sequence
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from typing import Any, cast

import cv2
import numpy as np
from numpy.typing import NDArray

from .adapters import (
    AnswerRejected,
    MaskModel,
    ModelError,
    SceneModel,
    SceneObject,
    SceneReading,
    VehicleObject,
    hosted_scene_prompt_sha256,
)
from .agent import TraceStep, VerifyRefineAgent
from .certificate import FORMAT, Signer, utc_now
from .geometry import (
    DepthFailure,
    DepthMeasurement,
    GeometryConfig,
    Line,
    PinholeCamera,
    depth_from_line,
    measure_wheel_depth,
)
from .media import ImageInput, VideoInput, image_from_rgb
from .models import (
    AgentStepOut,
    CertificateOut,
    MeasureResponse,
    ModelEstimateOut,
    ModelsOut,
    ReferenceOut,
    WaterlineOut,
)
from .muse_open import prompt_digest
from .overlay import png_b64, render_overlay
from .person import (
    PersonDecline,
    PersonFailure,
    PersonMeasurement,
    below_band,
    blur_heads,
    box_head_region,
    face_box_region,
    fits_box,
    head_region,
    measure_person_depth,
    water_below,
)
from .references import WHEEL_KINDS, ReferenceChoice, RefKind, resolve_reference
from .sam import MAX_PROMPTED_OBJECTS, ObjectMask, ObjectPrompt, PointPrompt
from .validate import RULES
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
# A wheel measurement is not used when the scene model's estimate is deeper than
# the measurement's two-sigma upper bound and by more than this share of the
# estimate's own sigma (at least 20 cm, so at least 10 cm; see measure()). A few
# centimetres between a precise wheel and a coarse estimate are not a conflict.
SHALLOW_EST_SIGMA_SHARE = 0.5
FINISH_RESERVE_S = 5.0
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
    max_vehicles: int = 2
    max_persons: int = 3
    min_quality: float = 0.25
    # Seconds from the request's start after which no further check by the scene
    # model is started or waited for; the best answer reached by then is
    # returned, labelled. None: no budget. The last FINISH_RESERVE_S are kept for
    # the steps after the checks (geometry, people, overlay).
    budget_s: float | None = 55.0


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
    vehicle: VehicleObject | None = None  # its wheel is searched for inside the box


@dataclass(frozen=True, slots=True)
class ModelEstimate:
    depth_cm: float
    sigma_cm: float
    basis: str
    low_cm: float | None = None  # the range the scene model gave, when it gave one
    high_cm: float | None = None


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


def wheel_final(
    depth_cm: float,
    sigma_cm: float,
    squash_cm: float,
    squash_sigma_cm: float,
    agreement_cm: float | None,
) -> tuple[float, float]:
    """The published depth and sigma from a wheel's own measurement.

    A loaded tyre's fitted circle reaches below the road by its squash, which is
    subtracted with its own uncertainty. Two waterlines that disagree mean one of
    them is wrong and we cannot tell which, so the difference widens the sigma.
    The certificate replay calls this too."""
    depth = max(0.0, depth_cm - squash_cm)
    sigma = math.hypot(sigma_cm, squash_sigma_cm)
    if agreement_cm is not None:
        sigma = math.hypot(sigma, agreement_cm)
    return depth, sigma


def person_final_sigma(sigma_cm: float, agreement_cm: float | None) -> float:
    """A person measurement's sigma, widened by the landmark disagreement when the
    scene model's body landmark lies more than two sigma away."""
    if agreement_cm is not None and agreement_cm > 2.0 * sigma_cm:
        return math.hypot(sigma_cm, agreement_cm)
    return sigma_cm


def shallow_conflict(depth_cm: float, sigma_cm: float, est_cm: float, est_sigma_cm: float) -> bool:
    """True when a measurement is too shallow to publish against the scene model's
    estimate: the estimate lies above the measurement's two-sigma upper bound,
    and by more than half the estimate's own sigma. Only the shallow side counts."""
    gap = est_cm - depth_cm
    return gap > max(2.0 * sigma_cm, SHALLOW_EST_SIGMA_SHARE * est_sigma_cm)


def model_estimate(reading: SceneReading) -> ModelEstimate | None:
    """The scene model's own depth estimate for the street, when it gave one."""
    if reading.overall_depth_cm is not None and reading.overall_depth_cm >= 0:
        d = reading.overall_depth_cm
        basis = reading.overall_reason or "the scene model's overall visual estimate"
        lo, hi = reading.overall_low_cm, reading.overall_high_cm
        return ModelEstimate(d, estimate_sigma(d, lo, hi), basis, lo, hi)
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


# The wheel each kind of vehicle the scene model names stands on. Trucks and
# other vehicles have no catalogue wheel, so their wheels are not measured.
VEHICLE_WHEEL: dict[str, RefKind] = {
    "car": RefKind.CAR_WHEEL,
    "two_wheeler": RefKind.TWO_WHEELER_WHEEL,
    "auto_rickshaw": RefKind.AUTO_WHEEL,
    "bus": RefKind.BUS_WHEEL,
}


def _inside(p: tuple[int, int], box: tuple[int, int, int, int]) -> bool:
    return box[0] <= p[0] < box[2] and box[1] <= p[1] < box[3]


def _scene_decision(reading: SceneReading, has_estimate: bool) -> str:
    if not reading.water_visible:
        return "stop: no water"
    n = sum(1 for r in reading.references if r.kind in WHEEL_KINDS)
    nv = sum(1 for v in reading.vehicles if v.wheels_visible and v.kind in VEHICLE_WHEEL)
    if nv:
        return (
            f"search {nv} vehicle box{'es' if nv > 1 else ''} for wheels"
            + (f", check {n} wheel{'s' if n > 1 else ''}" if n else "")
            + " and segment the water"
        )
    if n:
        return f"segment and verify {n} wheel{'s' if n > 1 else ''} and the water"
    npers = sum(1 for r in reading.references if r.kind == RefKind.PERSON)
    if npers:
        return (
            f"no wheel: measure {npers} person{'s' if npers > 1 else ''} against the stature "
            "prior and segment the water"
        )
    return "no wheel: report own estimate" if has_estimate else "stop: no wheel"


# Mask ids of the people (the wheels use 1 to 2*n, the water bands 40 and up).
PERSON_ID_BASE = 30
WATER_BELOW_ID_BASE = 50
PERSON_SOURCE = "prior:adult stature, NCD-RisC 2016 India (sex unknown mixture)"


def _clean_person_mask(
    mask: NDArray[np.bool_],
    water: NDArray[np.bool_],
    box: tuple[int, int, int, int] | None,
) -> NDArray[np.bool_] | None:
    """The visible person: cut to the box (wider by 10%, higher by 15% for hair),
    water removed, the largest connected piece kept."""
    m = mask & ~water
    if box is not None:
        h, w = m.shape
        x0, y0, x1, y1 = box
        bw, bh = x1 - x0, y1 - y0
        keep = np.zeros_like(m)
        keep[
            max(0, round(y0 - 0.15 * bh)) : min(h, round(y1 + 0.05 * bh)),
            max(0, round(x0 - 0.1 * bw)) : min(w, round(x1 + 0.1 * bw)),
        ] = True
        m &= keep
    if not m.any():
        return None
    n, labels, stats, _ = cv2.connectedComponentsWithStats(m.astype(np.uint8), connectivity=8)
    if n <= 1:
        return None
    big = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    return np.asarray(labels == big)


def draw_person_axis(rgb: NDArray[np.uint8], pm: PersonMeasurement) -> NDArray[np.uint8]:
    """The body axis from head top to the waterline, with the shoulder row marked."""
    out = rgb.copy()
    t = max(2, out.shape[1] // 400)
    x = round(pm.x_centre)
    half = max(8, round(0.6 * (pm.shoulder_y - pm.top_y)))
    cv2.line(out, (x, round(pm.top_y)), (x, round(pm.waterline_y)), (255, 210, 0), t)
    cv2.line(
        out, (x - half, round(pm.shoulder_y)), (x + half, round(pm.shoulder_y)), (255, 210, 0), t
    )
    cv2.line(
        out,
        (x - 2 * half, round(pm.waterline_y)),
        (x + 2 * half, round(pm.waterline_y)),
        (0, 220, 255),
        t,
    )
    return out


# A measured person: the measurement, the scene model's object, the outline and
# the trace target ("person 1").
_PersonBest = tuple[PersonMeasurement, SceneObject, NDArray[np.bool_], str]


class LiveTrace(list[TraceStep]):
    """The trace list; each appended step is also passed to ``on_step`` at once."""

    def __init__(self, on_step: Callable[[TraceStep], None] | None = None) -> None:
        super().__init__()
        self._on_step = on_step

    def append(self, step: TraceStep) -> None:
        super().append(step)
        if self._on_step is not None:
            self._on_step(step)


def _ms(t0: float) -> int:
    return round(1000 * (time.perf_counter() - t0))


@dataclass
class _Record:
    """What one request's certificate records beyond the response itself: the
    input and the numbers the depth was computed from (CONTRACTS.md 1.2)."""

    input: dict[str, Any] = field(default_factory=dict)
    wheel: dict[str, Any] | None = None
    person: dict[str, Any] | None = None
    estimate: dict[str, Any] | None = None


# The record of the request being measured. Pipeline.measure sets a fresh one for
# each request (each runs in its own task, so concurrent requests never share it).
_RECORD: ContextVar[_Record | None] = ContextVar("certificate_record", default=None)


def _pair(v: Any) -> list[float]:
    return [float(v[0]), float(v[1])]


def wheel_record(
    target: str,
    ref: ReferenceChoice,
    m: DepthMeasurement,
    line2: Line | None,
    agreement: float | None,
) -> dict[str, Any]:
    """A wheel measurement's numbers for the certificate, at full precision."""
    e, wl = m.ellipse, m.waterline
    cam = m.camera
    return {
        "target": target,
        "reference": {
            "kind": ref.kind.value,
            "diameter_cm": float(ref.diameter_cm),
            "diameter_sigma_cm": float(ref.diameter_sigma_cm),
            "source": ref.source,
            "squash_cm": float(ref.squash_cm),
            "squash_sigma_cm": float(ref.squash_sigma_cm),
        },
        "ellipse": {
            "cx": float(e.cx),
            "cy": float(e.cy),
            "a": float(e.a),
            "b": float(e.b),
            "theta": float(e.theta),
        },
        "waterline": {
            "point": _pair(wl.line.point),
            "direction": _pair(wl.line.direction),
            "water_normal": _pair(wl.water_normal),
            "rms_px": float(wl.rms_px),
            "n_points": int(wl.n_points),
        },
        "camera": None
        if cam is None
        else {"focal_px": float(cam.focal_px), "cx": float(cam.cx), "cy": float(cam.cy)},
        "arc_covered_deg": float(m.arc_covered_deg),
        "fraction_raw": float(m.fraction_raw),
        "fraction_bias": float(m.fraction_bias),
        "fraction": float(m.fraction),
        "ambiguity_spread": float(m.ambiguity_spread),
        "resample_sigma_cm": float(m.resample_sigma_cm),
        "model_sigma_share": float(m.model_sigma_share),
        "depth_cm": float(m.depth_cm),
        "depth_sigma_cm": float(m.depth_sigma_cm),
        "second_waterline": None
        if line2 is None
        else {"point": _pair(line2.point), "direction": _pair(line2.direction)},
        "agreement_cm": None if agreement is None else float(agreement),
        "estimate_here_cm": None,
    }


def person_record(target: str, pm: PersonMeasurement) -> dict[str, Any]:
    """A person measurement's numbers for the certificate, at full precision."""
    agreement = (
        abs(pm.landmark_depth_cm - pm.depth_cm) if pm.landmark_depth_cm is not None else None
    )
    return {
        "target": target,
        "top_y": float(pm.top_y),
        "shoulder_first_y": float(pm.shoulder_first_y),
        "shoulder_full_y": float(pm.shoulder_full_y),
        "bottom_y": float(pm.waterline_y),
        "height_cm": float(pm.height_cm),
        "height_sigma_cm": float(pm.height_sigma_cm),
        "visible_share": float(pm.visible_share),
        "depth_cm": float(pm.depth_cm),
        "depth_sigma_cm": float(pm.depth_sigma_cm),
        "landmark": pm.landmark,
        "landmark_depth_cm": None if pm.landmark_depth_cm is None else float(pm.landmark_depth_cm),
        "agreement_cm": None if agreement is None else float(agreement),
    }


def _prompt_entry(version: str) -> dict[str, str | None]:
    digest = prompt_digest(version)
    if digest is None and version.startswith("scene-hosted@"):
        digest = hosted_scene_prompt_sha256()
    return {"id": version, "sha256": digest}


class Pipeline:
    def __init__(
        self,
        scene: SceneModel,
        masks: MaskModel,
        config: PipelineConfig | None = None,
        agent: VerifyRefineAgent | None = None,
        signer: Signer | None = None,
    ):
        self.scene = scene
        self.masks = masks
        self.config = config or PipelineConfig()
        self.agent = agent
        # Signs each response's certificate; without one, no certificate is made.
        self.signer = signer

    @property
    def model_names(self) -> ModelsOut:
        return ModelsOut(
            scene=getattr(self.scene, "scene_model_name", self.scene.name),
            masks=getattr(self.masks, "mask_model_name", self.masks.name),
        )

    def model_versions(self) -> list[dict[str, str]]:
        """Each model with the exact weights or endpoint and settings used."""
        names = self.model_names
        return [
            {
                "role": "scene",
                "name": names.scene,
                "version": str(getattr(self.scene, "scene_model_version", names.scene)),
            },
            {
                "role": "masks",
                "name": names.masks,
                "version": str(getattr(self.masks, "mask_model_version", names.masks)),
            },
        ]

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
        faces = cast(Sequence[tuple[int, int, int, int]], kw.pop("faces", ()))
        resp = self._build(key, trace, timings, cost, **kw)
        resp._faces = [(int(a), int(b), int(c), int(d)) for a, b, c, d in faces]
        return resp

    def _build(
        self,
        key: ImageInput,
        trace: list[TraceStep],
        timings: dict[str, int],
        cost: float,
        **kw: object,
    ) -> MeasureResponse:
        rec = _RECORD.get()
        resp = MeasureResponse(
            models=self.model_names,
            timings_ms=timings,
            cost_usd=round(cost, 6),
            agent_trace=[AgentStepOut(**asdict(t)) for t in trace],
            captured_at=key.captured_at,
            input_sha256=None if rec is None else rec.input.get("sha256"),
            **kw,
        )
        if self.signer is not None and rec is not None:
            envelope = self.signer.sign(self.certificate_record(resp, rec))
            resp.certificate = CertificateOut(**envelope)
        return resp

    def certificate_record(self, resp: MeasureResponse, rec: _Record) -> dict[str, Any]:
        """The record a certificate signs (CONTRACTS.md, section 1.2)."""
        versions = sorted({t.prompt_version for t in resp.agent_trace if t.prompt_version})
        return {
            "format": FORMAT,
            "measurement_id": resp.measurement_id,
            "issued_at": utc_now(),
            "input": dict(rec.input),
            "models": self.model_versions(),
            "prompts": [_prompt_entry(v) for v in versions],
            "steps": [
                {
                    "step": t.step,
                    "target": t.target,
                    "model": t.model,
                    "prompt_version": t.prompt_version,
                    "decision": t.decision,
                    "latency_ms": t.latency_ms,
                }
                for t in resp.agent_trace
            ],
            "checks": {
                "rules": list(RULES),
                "failures": [
                    f"{t.target}: {t.output} ({t.decision})"
                    for t in resp.agent_trace
                    if t.step == "validate"
                ],
            },
            "geometry": {"wheel": rec.wheel, "person": rec.person, "estimate": rec.estimate},
            "decision": {
                "usable": resp.usable,
                "method": resp.method,
                "depth_cm": resp.depth_cm,
                "depth_sigma_cm": resp.depth_sigma_cm,
                "confidence": resp.confidence,
                "reasons": list(resp.reasons),
            },
        }

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
        faces: Sequence[tuple[int, int, int, int]] = (),
        lead: str | None = None,
    ) -> MeasureResponse:
        """No wheel could be measured (or ``lead`` says why a measurement was not
        used): the scene model's estimate, clearly labelled."""
        reasons = [
            *reasons,
            lead
            or "no wheel could be measured; depth is the scene model's visual estimate, "
            "not a measurement",
            f"estimate based on: {est.basis}",
        ]
        trace.append(
            TraceStep(
                "estimate",
                "photo",
                self.model_names.scene,
                "no measurable wheel" if lead is None else "measurement not used",
                f"{est.depth_cm:.0f} cm, sigma {est.sigma_cm:.0f} cm",
                "report model estimate",
                0,
            )
        )
        label = f"estimate {est.depth_cm:.0f} cm, sigma {est.sigma_cm:.0f} cm (not measured)"
        overlay = png_b64(
            render_overlay(
                blur_heads(key.rgb, faces),
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
            faces=faces,
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
        on_step: Callable[[TraceStep], None] | None = None,
    ) -> MeasureResponse:
        """Measure one photo (or a video's key frame).

        ``on_step`` is called with every trace step the moment it is recorded,
        in the order the steps happen (the streaming endpoint uses it). With a
        signer, the response carries a signed certificate of the measurement.
        """
        rec = _Record()
        key = image if image is not None else (video.key_frame if video is not None else None)
        if key is not None:
            measured_video = image is None and video is not None
            sha = video.source_sha256 if measured_video and video is not None else key.source_sha256
            rec.input = {
                "kind": "video" if measured_video else "image",
                "sha256": sha,
                "width": key.width,
                "height": key.height,
            }
        token = _RECORD.set(rec)
        try:
            return await self._measure(
                image=image,
                video=video,
                hint_reference=hint_reference,
                hint_diameter_cm=hint_diameter_cm,
                on_step=on_step,
            )
        finally:
            _RECORD.reset(token)

    async def _measure(
        self,
        *,
        image: ImageInput | None,
        video: VideoInput | None,
        hint_reference: str | None,
        hint_diameter_cm: float | None,
        on_step: Callable[[TraceStep], None] | None,
    ) -> MeasureResponse:
        cfg = self.config
        started = time.perf_counter()
        deadline = None if cfg.budget_s is None else started + cfg.budget_s - FINISH_RESERVE_S
        key = image if image is not None else (video.key_frame if video is not None else None)
        if key is None:
            raise ValueError("an image or a video is required")
        timings: dict[str, int] = {}
        trace: list[TraceStep] = LiveTrace(on_step)
        cost = 0.0
        reasons: list[str] = []
        hint_kind = RefKind(hint_reference) if hint_reference else None
        scene_name = self.model_names.scene

        # Model failures (no key, API down, weights not loaded) raise ModelError
        # to the caller: they are service faults, not a verdict on the photo.
        t0 = time.perf_counter()
        try:
            reading = await self.scene.read_scene(key)
        except AnswerRejected as exc:
            # Asked twice, and both answers failed a check the decision rests on.
            timings["scene"] = _ms(t0)
            version = getattr(self.scene, "scene_prompt_version", None)
            trace.append(
                TraceStep(
                    "scene",
                    "photo",
                    scene_name,
                    f"photo {key.width}x{key.height} px",
                    "two answers, both failed their checks",
                    "stop: no checked scene reading",
                    timings["scene"],
                    version,
                )
            )
            first, second = exc.attempts[0], exc.attempts[-1]
            trace.append(
                TraceStep("validate", "photo", "validator", "scene answer", first, "retry", 0)
            )
            trace.append(
                TraceStep(
                    "validate", "photo", "validator", "second scene answer", second, "give up", 0
                )
            )
            return self._unusable(
                key,
                [f"the scene model's answer failed its checks twice ({second})"],
                trace,
                timings,
                exc.cost_usd,
            )
        timings["scene"] = _ms(t0)
        cost += reading.cost_usd
        est = model_estimate(reading)
        rec = _RECORD.get()
        if rec is not None and est is not None:
            rec.estimate = {
                "depth_cm": float(est.depth_cm),
                "low_cm": est.low_cm,
                "high_cm": est.high_cm,
                "sigma_cm": float(est.sigma_cm),
                "basis": est.basis,
            }
        kinds = ", ".join(r.kind.value for r in reading.references) or "none"
        trace.append(
            TraceStep(
                "scene",
                "photo",
                scene_name,
                f"photo {key.width}x{key.height} px"
                + (" (cached reading)" if reading.cached else ""),
                f"water {'visible' if reading.water_visible else 'not visible'}; references: "
                f"{kinds}" + (f"; own estimate {est.depth_cm:.0f} cm" if est is not None else ""),
                _scene_decision(reading, est is not None),
                timings["scene"],
                reading.prompt_version,
            )
        )
        if reading.retried_because is not None:
            trace.append(
                TraceStep(
                    "validate",
                    "photo",
                    "validator",
                    "first scene answer",
                    reading.retried_because,
                    "retry",
                    0,
                )
            )
        if reading.dropped:
            n = len(reading.dropped)
            trace.append(
                TraceStep(
                    "validate",
                    "photo",
                    "validator",
                    "scene answer",
                    "; ".join(reading.dropped),
                    f"drop {n} field{'s' if n > 1 else ''} that failed, use the rest",
                    0,
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
        persons = [r for r in reading.references if r.kind == RefKind.PERSON][: cfg.max_persons]
        # Faces of everyone the scene model boxed, blurred in every image that
        # leaves the service; refined to the head of each person's mask later.
        faces: list[tuple[int, int, int, int]] = [face_box_region(b) for b in reading.faces]
        faces += [
            box_head_region(r.box)
            for r in reading.references
            if r.kind == RefKind.PERSON and r.box is not None  # all, not only those measured
        ]
        if hint_kind == RefKind.KERB or (hint_kind == RefKind.PERSON and not persons):
            if est is not None:
                return self._estimate_only(
                    key, reasons, trace, timings, cost, est, q.score, faces=faces
                )
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
        # Vehicles the scene model boxed: on the agent path their wheels are found
        # inside the box by SAM, whatever its points said.
        vehicle_jobs: list[tuple[VehicleObject, RefKind]] = []
        if self.agent is not None:
            for v in reading.vehicles:
                if not v.wheels_visible or len(vehicle_jobs) >= cfg.max_vehicles:
                    continue
                vkind = VEHICLE_WHEEL.get(v.kind)
                if (
                    vkind is None
                    and v.kind == "truck"
                    and any(w.kind == RefKind.CAR_WHEEL and _inside(w.point, v.box) for w in wheels)
                ):
                    # A pickup or light truck the scene model says stands on car-size
                    # wheels: searched with the car wheel prior (its wider sigma).
                    vkind = RefKind.CAR_WHEEL
                    reasons.append("a light truck on car-size wheels: generic car wheel size used")
                if vkind is None:
                    reasons.append(f"no wheel size is known for a {v.kind.replace('_', ' ')}")
                    continue
                if hint_kind is not None and hint_kind in WHEEL_KINDS and vkind != hint_kind:
                    continue
                vehicle_jobs.append((v, vkind))
            boxes = [v.box for v, _ in vehicle_jobs]
            wheels = [w for w in wheels if not any(_inside(w.point, b) for b in boxes)]
        wheels = wheels[: max(0, cfg.max_wheels - len(vehicle_jobs))]
        if hint_kind == RefKind.PERSON:
            wheels, vehicle_jobs = [], []
        if not wheels and not vehicle_jobs and not persons:
            if est is not None:
                return self._estimate_only(
                    key,
                    [*reasons, "no wheel or person found to measure against"],
                    trace,
                    timings,
                    cost,
                    est,
                    q.score,
                    faces=faces,
                )
            return self._unusable(
                key,
                [*reasons, "no wheel or person found to measure against"],
                trace,
                timings,
                cost,
            )
        if not reading.water_points and reading.water_box is None:
            return self._unusable(
                key, [*reasons, "could not locate the water surface"], trace, timings, cost, est
            )

        cands: list[_Candidate] = []
        for v, vkind in vehicle_jobs:
            i = len(cands)
            centre = ((v.box[0] + v.box[2]) // 2, (v.box[1] + v.box[3]) // 2)
            obj = SceneObject(vkind, centre, v.box, vehicle=v.vehicle)
            cands.append(_Candidate(obj, i + 1, 1 + 2 * i, None, False, vehicle=v))
        for wref in wheels:
            i = len(cands)
            rid = None
            if wref.rim_point is not None and wref.rim_point != wref.point:
                rid = 2 + 2 * i
            cands.append(_Candidate(wref, i + 1, 1 + 2 * i, rid, wref is reading.references[0]))
        water_id = MAX_PROMPTED_OBJECTS

        t0 = time.perf_counter()
        if self.agent is not None:
            # The verifier is shown crops of this photo: faces blurred first.
            agent_key = image_from_rgb(blur_heads(key.rgb, faces)) if faces else key
            water, failures, extra_cost = await self._agent_masks(
                agent_key, reading, cands, water_id, trace, deadline
            )
            timings["agent"] = _ms(t0)
            if any("time budget" in f for f in failures):
                trace.append(
                    TraceStep(
                        "budget",
                        "photo",
                        "pipeline",
                        f"time budget {cfg.budget_s:.0f} s",
                        f"{_ms(started) / 1000:.0f} s used; checks still open were stopped",
                        "answer with what was verified in time",
                        0,
                    )
                )
                reasons.append(
                    f"the time budget of {cfg.budget_s:.0f} s ran out before every check was "
                    "done; this is the best answer reached in time"
                )
        else:
            water, failures, extra_cost = await self._single_request(
                key, reading, cands, water_id, trace
            )
            timings["masks"] = _ms(t0)
        cost += extra_cost
        if (water is None or water.area == 0) and persons:
            # No verified water: people can still be measured against water
            # segmented just below them.
            empty = ObjectMask(
                "water", 0, np.zeros((key.height, key.width), bool), (0, 0, 0, 0), None
            )
            found = await self._measure_persons(key, persons, empty, trace, failures, faces)
            if found is not None:
                return self._person_result(
                    key,
                    [*reasons, "the water surface as a whole could not be verified"],
                    failures,
                    trace,
                    timings,
                    cost,
                    est,
                    q.score,
                    found,
                    None,
                    faces,
                )
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
                    faces=faces,
                )
            return self._unusable(
                key, [*reasons, *failures, "water could not be segmented"], trace, timings, cost
            )

        person_best: _PersonBest | None = None
        if persons:
            person_best = await self._measure_persons(key, persons, water, trace, failures, faces)

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
        if best is None and person_best is not None:
            return self._person_result(
                key,
                reasons,
                failures,
                trace,
                timings,
                cost,
                est,
                q.score,
                person_best,
                water,
                faces,
            )
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
                    faces=faces,
                )
            return self._unusable(key, [*reasons, *failures], trace, timings, cost)

        m, cand, ref = best
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
            reasons.append(f"scene model waterline agrees within {agreement:.1f} cm")
        # The squash correction, then the second waterline's disagreement.
        depth_cm, sigma = wheel_final(
            m.depth_cm, m.depth_sigma_cm, ref.squash_cm, ref.squash_sigma_cm, agreement
        )
        wrec = wheel_record(
            f"{cand.obj.kind.value.replace('_', ' ')} {cand.index}", ref, m, line2, agreement
        )
        if rec is not None:
            rec.wheel = wrec
        confidence = _confidence(q.score, sigma, ref.diameter_cm, agreement)

        if est is not None:
            local = cand.obj.depth_cm if cand.obj.depth_cm is not None else est.depth_cm
            local_sigma = estimate_sigma(local)
            wrec["estimate_here_cm"] = float(local)
            combined = math.hypot(sigma, local_sigma)
            # Shallow-side guard. Calling deep water shallow is the dangerous
            # error, and the estimate's own sigma (20 cm or more) is too wide for a
            # two-combined-sigma test to catch it: a rim outline measured 0.7 cm,
            # sigma 2.5, against an estimate of 20 cm (x1). So a measurement is not
            # used when the scene model's estimate lies above the measurement's own
            # two-sigma upper bound and by more than half the estimate's sigma; the
            # estimate is reported instead, and the reading is in ``reasons``.
            gap = local - depth_cm
            if shallow_conflict(depth_cm, sigma, local, local_sigma):
                trace.append(
                    TraceStep(
                        "estimate",
                        "photo",
                        "geometry",
                        f"measurement {depth_cm:.1f} cm (sigma {sigma:.1f}) and model "
                        f"estimate {local:.0f} cm",
                        f"the estimate is {gap:.0f} cm deeper, beyond the measurement's "
                        f"two-sigma bound of {depth_cm + 2.0 * sigma:.0f} cm",
                        "shallow reading not trusted: report the estimate",
                        0,
                    )
                )
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
                    faces=faces,
                    lead=(
                        f"the {KIND_TEXT[cand.obj.kind]} measured {depth_cm:.1f} cm (sigma "
                        f"{sigma:.1f} cm), but the scene model estimates {local:.0f} cm "
                        + ("at that wheel" if cand.obj.depth_cm is not None else "for the street")
                        + ", above the measurement's two-sigma bound; a shallow reading that "
                        "disagrees is not trusted, so the scene model's estimate is reported"
                    ),
                )
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

        if person_best is not None:
            pm = person_best[0]
            if rec is not None:
                rec.person = person_record(person_best[3], pm)
            combined = math.hypot(sigma, pm.depth_sigma_cm)
            differ = abs(depth_cm - pm.depth_cm) > 2.0 * combined
            if differ:
                confidence = round(confidence * 0.5, 2)
                reasons.append(
                    f"a person in the water gives {pm.depth_cm:.0f} cm (sigma "
                    f"{pm.depth_sigma_cm:.0f} cm), more than two sigma from the wheel; "
                    "confidence lowered"
                )
            else:
                reasons.append(
                    f"a person in the water gives {pm.depth_cm:.0f} cm (sigma "
                    f"{pm.depth_sigma_cm:.0f} cm), consistent with the wheel"
                )
            trace.append(
                TraceStep(
                    "estimate",
                    "photo",
                    "geometry",
                    f"wheel {depth_cm:.1f} cm and person {pm.depth_cm:.1f} cm",
                    f"difference {abs(depth_cm - pm.depth_cm):.0f} cm, two combined sigma "
                    f"{2.0 * combined:.0f} cm",
                    "use the wheel",
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
                blur_heads(key.rgb, faces),
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
            faces=faces,
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
        deadline: float | None = None,
    ) -> tuple[ObjectMask | None, list[str], float]:
        """Masks through the verify-and-refine agent.

        No separate rim mask is used on this path: the accepted wheel is already a
        solid shape (tyre and rim, closed with its convex hull when SAM gave the
        tyre ring), and rim masks from a single scene-model point were unreliable.
        """
        assert self.agent is not None
        refs = [(c.index, c.obj, c.wheel_id) for c in cands if c.vehicle is None]
        vjobs = [
            (c.index, c.vehicle, c.obj.kind, c.wheel_id) for c in cands if c.vehicle is not None
        ]
        water_out, outs = await self.agent.run(
            key,
            refs,
            reading.water_box,
            reading.water_points,
            water_id,
            emit=trace.append,
            vehicles=vjobs,
            deadline=deadline,
        )
        # The agent returns vehicles first, then references; restore the order of cands.
        by_index = {o.index: o for o in outs}
        ref_outs = [by_index[c.index] for c in cands]
        failures: list[str] = []
        for c, out in zip(cands, ref_outs, strict=True):
            if not out.accepted or out.mask is None:
                what = (
                    f"{c.vehicle.kind.replace('_', ' ')} {c.index} wheel"
                    if c.vehicle is not None
                    else f"{KIND_TEXT[c.obj.kind]} {c.index}"
                )
                failures.append(
                    f"{what}: the scene model did not confirm the mask ({out.verdict or 'no mask'})"
                )
                continue
            c.wheel = out.mask
            c.waterline_points = out.waterline_points
        water = water_out.mask if water_out.accepted else None
        if water is not None and "time budget" in water_out.verdict:
            failures.append("water: only the part confirmed before the time budget ran out is used")
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

    # ------------------------------------------------------------------ people

    async def _measure_persons(
        self,
        key: ImageInput,
        persons: Sequence[SceneObject],
        water: ObjectMask,
        trace: list[TraceStep],
        failures: list[str],
        faces: list[tuple[int, int, int, int]],
    ) -> _PersonBest | None:
        """Segment every person the scene model boxed and measure the depth at each.

        SAM is prompted with the person's box, a point on the body and the head
        top, with the water points outside the box as negatives. The mask is cut
        to the box (a little wider, and higher for the hair), water pixels are
        removed and the largest piece kept. Each person's head region from the
        mask joins ``faces`` so that it is blurred in every returned image. The
        measurement with the smallest sigma wins.
        """
        prompts: list[tuple[SceneObject, ObjectPrompt]] = []
        for i, p in enumerate(persons):
            pts = [PointPrompt(*p.point)]
            if p.head_top is not None:
                pts.append(PointPrompt(*p.head_top))
            if p.box is not None:
                for wx, wy in self._water_points_outside(p.box, water.mask)[:2]:
                    pts.append(PointPrompt(wx, wy, positive=False))
            prompts.append((p, ObjectPrompt(PERSON_ID_BASE + i, tuple(pts), p.box)))
        t0 = time.perf_counter()
        seg = await self.masks.segment(key, [op for _, op in prompts])
        trace.append(
            TraceStep(
                "segment",
                "people",
                self.model_names.masks,
                f"{len(prompts)} person box{'es' if len(prompts) > 1 else ''} with head and body "
                "points",
                f"{sum(1 for _, op in prompts if op.object_id in seg.masks)} masks",
                "measure each against the stature prior",
                _ms(t0),
            )
        )
        masks: list[NDArray[np.bool_] | None] = []
        for p, op in prompts:
            om = seg.masks.get(op.object_id)
            mask = None if om is None else _clean_person_mask(om.mask, water.mask, p.box)
            if mask is not None:
                faces.append(head_region(mask, None))
            masks.append(mask)
        local_water = await self._water_below_people(key, water.mask, masks, trace)
        best: _PersonBest | None = None
        for i, (p, _op) in enumerate(prompts):
            target = f"person {i + 1}"
            mask = masks[i]
            lw = local_water[i]
            here = water.mask if lw is None else lw
            tg = time.perf_counter()
            if mask is None:
                res: PersonMeasurement | PersonDecline = PersonDecline(PersonFailure.MASK_EMPTY)
            else:
                res = await asyncio.to_thread(
                    measure_person_depth,
                    mask,
                    here,
                    posture=p.posture or "standing",
                    adult=p.adult,
                    landmark=p.landmark,
                )
            # The box check comes after posture and the rest: those reasons are clearer.
            if (
                isinstance(res, PersonMeasurement)
                and p.box is not None
                and mask is not None
                and not fits_box(mask, p.box)
            ):
                res = PersonDecline(PersonFailure.BOX_MISMATCH)
            ms = _ms(tg)
            if isinstance(res, PersonDecline):
                failures.append(f"{target}: {res.text}")
                trace.append(
                    TraceStep(
                        "geometry", target, "geometry", "person mask", res.text, "declined", ms
                    )
                )
                continue
            assert mask is not None
            if lw is not None:
                failures.append(
                    f"{target}: the water just below was segmented locally, not checked by "
                    "the scene model"
                )
            # The head region from the measured shoulders is tighter and sure.
            faces.append(head_region(mask, res.shoulder_y))
            lm = (
                f"; scene model says the water is at the {res.landmark.replace('_', ' ')} "
                f"({res.landmark_depth_cm:.0f} cm)"
                if res.landmark is not None and res.landmark_depth_cm is not None
                else ""
            )
            trace.append(
                TraceStep(
                    "geometry",
                    target,
                    "geometry",
                    "person and water masks",
                    f"depth {res.depth_cm:.1f} cm, sigma {res.depth_sigma_cm:.1f} cm, "
                    f"{100 * res.visible_share:.0f}% of stature above water{lm}",
                    "candidate",
                    ms,
                )
            )
            if best is None or res.depth_sigma_cm < best[0].depth_sigma_cm:
                best = (res, p, mask, target)
        return best

    async def _water_below_people(
        self,
        key: ImageInput,
        water: NDArray[np.bool_],
        masks: Sequence[NDArray[np.bool_] | None],
        trace: list[TraceStep],
    ) -> list[NDArray[np.bool_] | None]:
        """Water segmented just below each person whose outline does not end on the
        verified water (SAM leaves out churned water at legs, and the verified mask
        may be missing altogether). Like the water at wheels: a band below the
        person, positive points in it, a negative point on the legs, the result
        kept inside the band. Returns, per person, the verified water joined with
        that band, or None when the verified water already reaches them."""
        h, w = water.shape
        jobs: list[tuple[int, tuple[int, int, int, int], ObjectPrompt]] = []
        for i, mask in enumerate(masks):
            if mask is None or water_below(mask, water) >= 0.5:
                continue
            band = below_band(mask)
            if band is None:
                continue
            x0, y0, x1, _ = band
            bw = x1 - x0
            ys = np.nonzero(mask.any(axis=1))[0]
            tall = int(ys.max() - ys.min() + 1)
            box = (
                max(0, x0 - bw // 2),
                max(0, y0 - 2),
                min(w, x1 + bw // 2),
                min(h, y0 + max(8, tall // 6)),
            )
            ym = min(h - 1, (box[1] + box[3]) // 2)
            legs = np.nonzero(mask[max(0, y0 - 6) : y0])
            pts = [PointPrompt(max(0, box[0] + 2), ym), PointPrompt(min(w - 1, box[2] - 3), ym)]
            pts.append(PointPrompt((x0 + x1) // 2, min(h - 1, box[3] - 2)))
            if legs[0].size:
                pts.append(PointPrompt(int(np.median(legs[1])), max(0, y0 - 4), positive=False))
            jobs.append((i, box, ObjectPrompt(WATER_BELOW_ID_BASE + i, tuple(pts), box)))
        out: list[NDArray[np.bool_] | None] = [None] * len(masks)
        if not jobs:
            return out
        t0 = time.perf_counter()
        seg = await self.masks.segment(key, [p for _, _, p in jobs])
        found = 0
        for i, box, p in jobs:
            m = seg.masks.get(p.object_id)
            if m is None:
                continue
            inside = np.zeros_like(water)
            inside[box[1] : box[3], box[0] : box[2]] = True
            pm = masks[i]
            assert pm is not None
            out[i] = water | (m.mask & inside & ~pm)
            found += 1
        trace.append(
            TraceStep(
                "segment",
                "water below people",
                self.model_names.masks,
                f"{len(jobs)} band{'s' if len(jobs) > 1 else ''} just below people the "
                "verified water does not reach",
                f"{found} water mask{'s' if found != 1 else ''}",
                "check that each outline ends at water",
                _ms(t0),
            )
        )
        return out

    @staticmethod
    def _water_points_outside(
        box: tuple[int, int, int, int], water: NDArray[np.bool_]
    ) -> list[tuple[int, int]]:
        """Water pixels just left and right of a person box, as negative prompts."""
        h, w = water.shape
        x0, y0, x1, y1 = box
        y = min(h - 1, max(0, y1 - max(2, (y1 - y0) // 20)))
        gap = max(4, (x1 - x0) // 4)
        out = []
        for x in (x0 - gap, x1 + gap):
            if 0 <= x < w and water[y, x]:
                out.append((int(x), int(y)))
        return out

    def _person_result(
        self,
        key: ImageInput,
        reasons: list[str],
        failures: list[str],
        trace: list[TraceStep],
        timings: dict[str, int],
        cost: float,
        est: ModelEstimate | None,
        quality: float,
        person: _PersonBest,
        water: ObjectMask | None,
        faces: Sequence[tuple[int, int, int, int]],
    ) -> MeasureResponse:
        """No wheel measured, a person was: depth against the adult stature prior."""
        pm, _p, mask, target = person
        depth_cm, sigma = pm.depth_cm, pm.depth_sigma_cm
        reasons = [
            *reasons,
            *failures,
            "a person standing in the water was measured against the adult stature prior "
            f"({pm.height_cm:.0f} cm, sigma {pm.height_sigma_cm:.0f} cm)",
            "a person reference is coarse: expect about 10 to 15 cm of error",
        ]
        agreement = None
        if pm.landmark_depth_cm is not None and pm.landmark is not None:
            agreement = abs(pm.landmark_depth_cm - depth_cm)
            reasons.append(
                f"the scene model puts the water at the {pm.landmark.replace('_', ' ')}, "
                f"about {pm.landmark_depth_cm:.0f} cm ({agreement:.0f} cm apart)"
            )
        sigma = person_final_sigma(sigma, agreement)
        rec = _RECORD.get()
        if rec is not None:
            rec.person = person_record(target, pm)
        confidence = _confidence(quality, sigma, pm.height_cm, agreement)
        if est is not None:
            combined = math.hypot(sigma, est.sigma_cm)
            if abs(depth_cm - est.depth_cm) > 2.0 * combined:
                confidence = round(confidence * 0.5, 2)
                reasons.append(
                    f"the scene model's own estimate ({est.depth_cm:.0f} cm) disagrees with the "
                    "measurement by more than two sigma; confidence lowered"
                )
        trace.append(
            TraceStep(
                "estimate",
                "photo",
                "geometry",
                "no measurable wheel; a measured person",
                f"depth {depth_cm:.1f} cm, sigma {sigma:.1f} cm",
                "use the person",
                0,
            )
        )
        label = f"depth {depth_cm:.0f} cm, sigma {sigma:.0f} cm (person)"
        shown = draw_person_axis(blur_heads(key.rgb, faces), pm)
        overlay = png_b64(
            render_overlay(
                shown,
                None,
                label=label,
                water_mask=None if water is None else water.mask,
                object_masks=[mask],
            )
        )
        return self._response(
            key,
            trace,
            timings,
            cost,
            faces=faces,
            usable=True,
            reasons=list(dict.fromkeys(reasons)),
            depth_cm=round(depth_cm, 1),
            depth_sigma_cm=round(sigma, 1),
            reference=ReferenceOut(
                kind="person",
                source=PERSON_SOURCE,
                height_cm=round(pm.height_cm, 1),
                height_sigma_cm=round(pm.height_sigma_cm, 1),
            ),
            waterline=WaterlineOut(
                method="person_mask_bottom",
                y_px=round(pm.waterline_y, 1),
                agreement_cm=None if agreement is None else round(agreement, 1),
            ),
            confidence=confidence,
            overlay_png_b64=overlay,
            method="person_reference",
            model_estimate=self._estimate_out(est),
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
