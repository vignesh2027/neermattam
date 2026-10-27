"""The verify-and-refine agent: Muse Spark Open checks SAM's masks and corrects the prompts.

A single grounding point from a reasoning model is not trustworthy on a busy
street photo: in the first probe the point for a scooter wheel landed on the
blue water tank next to it, and SAM duly segmented the tank. So no mask is used
until the scene model has looked at it:

1. SAM proposes a mask from the scene model's box (and point).
2. The mask's region is cropped with context, the outline is drawn on the crop
   and the scene model is asked whether this is the object it named, whether
   the waterline is visible, and, if the outline is wrong, for a corrected box.
3. A corrected box goes back to SAM. At most ``max_refinements`` corrections
   per object; then the object is accepted or given up.

The water mask goes through the same loop on the whole photo. Every step is
recorded in a compact, truthful trace (:class:`TraceStep`).
"""

from __future__ import annotations

import asyncio
import io
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import cv2
import numpy as np
from numpy.typing import NDArray
from PIL import Image

from .adapters import MaskModel, ModelError, SceneObject
from .media import ImageInput
from .muse_open import (
    JsonReply,
    grid_box,
    grid_point,
    verify_prompt,
    water_verify_prompt,
)
from .references import RefKind
from .sam import ObjectMask, ObjectPrompt, PointPrompt
from .sam_local import box_iou

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

    async def ask_image(self, jpeg: bytes, prompt: str, *, what: str) -> JsonReply: ...


@dataclass(frozen=True, slots=True)
class TraceStep:
    step: str  # scene, segment, verify, geometry, estimate
    target: str  # "scene", "water", "wheel 1", ...
    model: str
    input: str
    output: str
    decision: str
    latency_ms: int


Box = tuple[int, int, int, int]


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


def _jpeg(rgb: NDArray[np.uint8], quality: int = 90) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


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
    return _jpeg(big)


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
    return _jpeg(np.ascontiguousarray(out))


def from_crop_grid(p: tuple[float, float], rect: Box) -> tuple[int, int]:
    left, top, right, bottom = rect
    return (
        round(left + p[0] / 1000.0 * (right - left)),
        round(top + p[1] / 1000.0 * (bottom - top)),
    )


def crop_box_to_image(b: tuple[float, float, float, float], rect: Box) -> Box:
    x0, y0 = from_crop_grid((b[0], b[1]), rect)
    x1, y1 = from_crop_grid((b[2], b[3]), rect)
    return x0, y0, max(x1, x0 + 2), max(y1, y0 + 2)


def _crop_points(v: Any, rect: Box, limit: int) -> list[tuple[int, int]]:
    if not isinstance(v, list):
        return []
    out = []
    for p in v[:limit]:
        g = grid_point(p)
        if g is not None:
            out.append(from_crop_grid(g, rect))
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


# --------------------------------------------------------------------------- agent


@dataclass
class VerifyRefineAgent:
    verifier: Verifier
    masks: MaskModel
    max_refinements: int = 2
    crop_side: int = 768
    # Confirm every "accept" from the quick check with a full-reasoning check of
    # the same crop (when the verifier offers ``confirm_image``).
    confirm: bool = True
    first_context: float = 2.0
    close_context: float = 0.8
    # When set, every image shown to the verifier is also written here (evaluation only).
    debug_dir: Path | None = None

    def _keep(self, jpeg: bytes, name: str) -> None:
        if self.debug_dir is not None:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            (self.debug_dir / name).write_bytes(jpeg)

    @property
    def mask_name(self) -> str:
        return str(getattr(self.masks, "mask_model_name", self.masks.name))

    @property
    def scene_name(self) -> str:
        return str(getattr(self.verifier, "scene_model_name", "scene model"))

    async def _segment(
        self, image: ImageInput, prompt: ObjectPrompt
    ) -> tuple[ObjectMask | None, int]:
        t0 = time.perf_counter()
        res = await self.masks.segment(image, [prompt])
        ms = round(1000 * (time.perf_counter() - t0))
        m = res.masks.get(prompt.object_id)
        if m is None or m.area == 0:
            return None, ms
        return m, ms

    async def refine_reference(
        self, image: ImageInput, obj: SceneObject, index: int, object_id: int
    ) -> RefOutcome:
        target = f"{obj.kind.value.replace('_', ' ')} {index}"
        kind_text = KIND_TEXT[obj.kind]
        box: Box | None = obj.box
        out = RefOutcome(index, obj, False, None, box, 0)
        pts: tuple[PointPrompt, ...] = ()
        if obj.point is not None and (
            box is None or (box[0] <= obj.point[0] < box[2] and box[1] <= obj.point[1] < box[3])
        ):
            pts = (PointPrompt(*obj.point),)
        for attempt in range(self.max_refinements + 1):
            prompt = ObjectPrompt(object_id, pts, box)
            mask, ms = await self._segment(image, prompt)
            out.trace.append(
                TraceStep(
                    "segment",
                    target,
                    self.mask_name,
                    f"box {_fmt_box(box)}" + _fmt_points(pts),
                    "empty mask"
                    if mask is None
                    else f"mask {mask.area} px, bbox {_fmt_box(mask.box)}, score {mask.confidence}",
                    "verify",
                    ms,
                )
            )
            # The first look is wide, so a box that missed its object still shows
            # it; later looks are close, so the outline can be judged in detail.
            context = self.first_context if attempt == 0 else self.close_context
            rect = crop_rect(
                image.width, image.height, box, mask.box if mask else None, context=context
            )
            jpeg = render_crop(image.rgb, mask.mask if mask else None, box, rect, self.crop_side)
            self._keep(jpeg, f"{target.replace(' ', '_')}_try{attempt}.jpg")
            try:
                reply = await self.verifier.ask_image(
                    jpeg, verify_prompt(kind_text), what=f"verify {target}"
                )
            except ModelError as exc:
                out.trace.append(
                    TraceStep(
                        "verify",
                        target,
                        self.scene_name,
                        f"crop {_fmt_box(rect)}",
                        f"error: {exc}",
                        "give up",
                        0,
                    )
                )
                out.verdict = "verification failed"
                return out
            out.cost_usd += reply.usage.cost_usd
            j = self._judge(reply.data, rect, box, mask, attempt)
            out.trace.append(
                TraceStep(
                    "verify",
                    target,
                    self.scene_name,
                    f"crop {_fmt_box(rect)} with the outline drawn",
                    j.summary,
                    j.decision,
                    reply.latency_ms,
                )
            )
            confirm = getattr(self.verifier, "confirm_image", None)
            if j.decision == "accept" and self.confirm and confirm is not None:
                # A quick check said yes. Before the outline is measured, the same
                # question is asked again with full reasoning; it must agree.
                try:
                    reply2 = await confirm(jpeg, verify_prompt(kind_text), what=f"confirm {target}")
                except ModelError as exc:
                    out.trace.append(
                        TraceStep(
                            "confirm",
                            target,
                            self.scene_name,
                            "same crop, full reasoning",
                            f"error: {exc}",
                            "give up",
                            0,
                        )
                    )
                    out.verdict = "confirmation failed"
                    return out
                out.cost_usd += reply2.usage.cost_usd
                j2 = self._judge(reply2.data, rect, box, mask, attempt)
                if not j2.waterline and j.waterline:
                    j2.waterline = j.waterline
                out.trace.append(
                    TraceStep(
                        "confirm",
                        target,
                        self.scene_name,
                        "same crop, full reasoning",
                        j2.summary,
                        j2.decision,
                        reply2.latency_ms,
                    )
                )
                j = j2
            out.box, out.mask, out.refinements = box, mask, attempt
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
            # point was the suspect part.
            box, pts = j.new_box, j.new_pts
        out.mask = None
        return out

    def _judge(
        self,
        d: dict[str, Any],
        rect: Box,
        box: Box | None,
        mask: ObjectMask | None,
        attempt: int,
    ) -> Judgement:
        """The decision for one verifier answer: accept, refine (with what) or give up."""
        is_target = _truthy(d.get("is_target"))
        quality = str(d.get("outline_quality") or ("good" if is_target else "wrong")).lower()
        reason = str(d.get("reason") or "").strip()[:160]
        wl = [grid_point(p) for p in (d.get("waterline") or [])[:2]]
        wl_px = tuple(from_crop_grid(p, rect) for p in wl if p is not None)
        gb = grid_box(d.get("corrected_box"))
        corrected = crop_box_to_image(gb, rect) if gb is not None else None
        positive = _crop_points(d.get("tyre_points"), rect, 2)
        negative = _crop_points(d.get("exclude_points"), rect, 2)
        new_box = corrected if corrected is not None else box
        new_pts = tuple(PointPrompt(*p) for p in positive) + tuple(
            PointPrompt(*p, positive=False) for p in negative
        )
        changed = bool(new_pts) or (
            corrected is not None and (box is None or box_iou(corrected, box) <= 0.9)
        )
        can_refine = attempt < self.max_refinements and changed
        view = str(d.get("view") or "").lower()
        edge = _fraction(d.get("tyre_edge_visible"))
        # A tyre seen edge-on, a vehicle not standing on its wheel, or a tyre with
        # almost none of its edge in view: there is no outline to measure, so stop
        # rather than spend more calls on it.
        unmeasurable = None
        if is_target and view == "head_on":
            unmeasurable = "tyre seen head-on, its outline cannot be measured"
        elif is_target and d.get("standing") is not None and not _truthy(d.get("standing")):
            unmeasurable = "the vehicle is not standing on this wheel"
        elif is_target and edge is not None and edge < MIN_EDGE_VISIBLE:
            unmeasurable = f"only {100 * edge:.0f}% of the tyre edge is visible"
        verdict = reason or "not the named object"
        if unmeasurable is not None:
            decision = f"give up: {unmeasurable}"
            verdict = unmeasurable
        elif mask is not None and is_target and quality == "good":
            decision = "accept"
        elif can_refine:
            decision = "refine with box " + _fmt_box(new_box) + _fmt_points(new_pts)
        elif is_target and quality == "partial":
            # Only an outline the verifier calls good is measured: a partial one
            # (a mudguard or exhaust taken in) would bias the depth.
            decision = "give up: outline only partly right"
            verdict = f"outline only partly right: {reason}"
        else:
            decision = "give up"
        summary = (
            f"is_target={str(is_target).lower()}, outline {quality}, "
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
    ) -> WaterOutcome:
        out = WaterOutcome(False, None, 0)
        pts = tuple(points)
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
            out.trace.append(
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
                )
            )
            jpeg = render_water(image.rgb, mask.mask if mask else None, box)
            self._keep(jpeg, f"water_try{attempt}.jpg")
            try:
                reply = await self.verifier.ask_image(
                    jpeg, water_verify_prompt(), what="verify water"
                )
            except ModelError as exc:
                out.trace.append(
                    TraceStep(
                        "verify",
                        "water",
                        self.scene_name,
                        "photo with the region tinted",
                        f"error: {exc}",
                        "give up",
                        0,
                    )
                )
                out.verdict = "verification failed"
                return out
            out.cost_usd += reply.usage.cost_usd
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
            out.trace.append(
                TraceStep(
                    "verify",
                    "water",
                    self.scene_name,
                    "photo with the proposed region tinted",
                    f"is_water={str(ok).lower()}, coverage {coverage}"
                    + (f"; {reason}" if reason else ""),
                    decision,
                    reply.latency_ms,
                )
            )
            out.refinements = attempt
            if decision.startswith("accept"):
                out.accepted, out.mask, out.verdict = True, mask, coverage
                return out
            if decision == "give up":
                out.verdict = reason or "not water"
                return out
            box = nbox
            pts = (npt,) if npt is not None else ()
        return out

    async def run(
        self,
        image: ImageInput,
        refs: Sequence[tuple[int, SceneObject, int]],
        water_box: Box | None,
        water_points: Sequence[tuple[int, int]],
        water_id: int,
    ) -> tuple[WaterOutcome, list[RefOutcome]]:
        """Water and every reference, verified concurrently (SAM itself runs one at a time)."""
        water_job = self.refine_water(image, water_box, water_points, water_id)
        ref_jobs = [self.refine_reference(image, obj, idx, oid) for idx, obj, oid in refs]
        water, *outs = await asyncio.gather(water_job, *ref_jobs)
        assert isinstance(water, WaterOutcome)
        return water, [r for r in outs if isinstance(r, RefOutcome)]
