"""Mask models for the open-weights path.

* :class:`Sam21LocalAdapter` runs SAM 2.1 (``facebook/sam2.1-hiera-small``,
  Apache-2.0, not gated) locally through ``transformers`` on Apple MPS, CUDA or
  the CPU. The model loads once (``load()``, called at service start), the
  image embedding is computed once per image and reused for every prompt, and
  box prompts (with an optional point) are preferred over points alone. Of the
  three masks SAM proposes per prompt, the one with the best mix of predicted
  IoU and agreement with the prompt box is kept.
* :class:`Sam31ApiAdapter` is SAM 3.1 on the Meta Model API (the existing
  hosted path) behind the same interface, and :class:`Sam31LocalAdapter` is the
  slot for SAM 3.1 weights once access is granted. :func:`mask_model_from_config`
  picks one by name, so SAM 3.1 drops in by configuration.

``torch``, ``torchvision`` and ``transformers`` are an optional extra
(``uv sync --extra local``); nothing here imports them until a model loads.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import resource
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from .adapters import MaskModel, MaskResult, MetaApiAdapter, ModelError, TrackResult
from .media import ImageInput, VideoInput
from .sam import ObjectMask, ObjectPrompt, PointPrompt

# SAM 2.1 checkpoints, all Apache-2.0 and ungated on Hugging Face.
SAM21_SIZES = {
    "small": "facebook/sam2.1-hiera-small",
    "base-plus": "facebook/sam2.1-hiera-base-plus",
    "large": "facebook/sam2.1-hiera-large",
}
SAM21_MODEL_ID = SAM21_SIZES["small"]
# Where checkpoints fetched outside the Hugging Face cache are looked for, one
# folder per repository name (config.json, model.safetensors and the processor
# configs). ``NEERMATTAM_MODELS_DIR`` overrides it.
DEFAULT_MODELS_DIR = Path.home() / "neermattam-data" / "models"


def box_iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    """IoU of two half-open pixel boxes ``(left, top, right, bottom)``."""
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def mask_box(mask: NDArray[np.bool_]) -> tuple[int, int, int, int] | None:
    ys, xs = np.nonzero(mask)
    if xs.size == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def largest_component(mask: NDArray[np.bool_]) -> NDArray[np.bool_]:
    import cv2

    n, lab, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    if n <= 2:
        return mask
    k = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    return np.asarray(lab == k)


@dataclass(frozen=True, slots=True)
class Candidate:
    mask: NDArray[np.bool_] = field(repr=False)
    iou_pred: float
    box_agreement: float | None

    @property
    def score(self) -> float:
        if self.box_agreement is None:
            return self.iou_pred
        return 0.5 * self.iou_pred + 0.5 * self.box_agreement


def choose_mask(
    masks: Sequence[NDArray[np.bool_]],
    iou_scores: Sequence[float],
    box: tuple[int, int, int, int] | None,
) -> tuple[int, list[Candidate]]:
    """Index of the best of SAM's proposals, and every proposal's scores.

    With a box prompt the score averages SAM's predicted IoU and the IoU of the
    mask's bounding box with the prompt box, so a mask that leaks into a
    neighbouring object (or covers only a sliver of the box) loses to one that
    fills the box it was asked for. Without a box it is SAM's IoU alone.
    """
    cands = []
    for m, s in zip(masks, iou_scores, strict=True):
        agree = None
        if box is not None:
            mb = mask_box(m)
            agree = 0.0 if mb is None else box_iou(mb, box)
        cands.append(Candidate(m, float(s), agree))
    best = max(range(len(cands)), key=lambda i: (cands[i].score, cands[i].iou_pred))
    return best, cands


@dataclass(frozen=True, slots=True)
class MaskCandidate:
    """One of SAM's proposals for a prompt variant, in full-frame coordinates."""

    mask: NDArray[np.bool_] = field(repr=False)
    iou_pred: float
    variant: str  # "box+points", "box" or "points"
    index: int  # which of SAM's three proposals


@dataclass
class Sam21LocalAdapter:
    """SAM 2.1 open weights, local, behind :class:`~neermattam_perception.adapters.MaskModel`.

    ``size`` picks the checkpoint (``small``, ``base-plus`` or ``large``) and
    ``dtype`` the weights' precision (``float16`` halves the memory on MPS).
    """

    size: str = "small"
    device: str | None = None
    dtype: str = "float32"
    cache_images: int = 4
    # Objects whose box is smaller than this share of the image's longer side are
    # segmented in a zoomed crop with ``zoom_context`` times their size around them.
    zoom_below: float = 0.3
    zoom_context: float = 1.0
    box_pad: float = 0.08
    models_dir: Path | None = None
    name: str = "sam2.1-local"
    _model: Any = field(default=None, repr=False)
    _processor: Any = field(default=None, repr=False)
    _cache: OrderedDict[str, Any] = field(default_factory=OrderedDict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    load_ms: int | None = None
    last_embed_ms: int | None = None
    last_embed_cached: bool = False

    def __post_init__(self) -> None:
        if self.size not in SAM21_SIZES:
            raise ValueError(f"unknown SAM 2.1 size {self.size!r}")
        if self.dtype not in ("float32", "float16"):
            raise ValueError("dtype must be float32 or float16")

    @property
    def model_id(self) -> str:
        return SAM21_SIZES[self.size]

    @property
    def mask_model_name(self) -> str:
        return f"sam-2.1-hiera-{self.size}" + ("-fp16" if self.dtype == "float16" else "")

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def _local_dir(self) -> Path | None:
        base = self.models_dir or Path(
            os.environ.get("NEERMATTAM_MODELS_DIR", str(DEFAULT_MODELS_DIR))
        )
        d = base / self.model_id.split("/")[-1]
        return d if (d / "model.safetensors").is_file() and (d / "config.json").is_file() else None

    def load(self, warm_up: bool = True) -> None:
        """Load the weights once (downloads them on first use) and run one warm-up pass."""
        with self._lock:
            if self._model is not None:
                return
            t0 = time.perf_counter()
            try:
                import torch
                from transformers import Sam2Model, Sam2Processor
            except ImportError as exc:  # the optional extra is not installed
                raise ModelError(
                    "SAM 2.1 needs torch, torchvision and transformers (uv sync --extra local)"
                ) from exc
            dev = self.device
            if dev is None:
                if torch.backends.mps.is_available():
                    dev = "mps"
                elif torch.cuda.is_available():
                    dev = "cuda"
                else:
                    dev = "cpu"
            self.device = dev
            # Prefer local copies: on a network whose IPv6 does not route, the
            # Hub's freshness check alone can stall for minutes.
            local = self._local_dir()
            source = str(local) if local is not None else self.model_id
            try:
                proc = Sam2Processor.from_pretrained(source, local_files_only=True)
                model = Sam2Model.from_pretrained(source, local_files_only=True)
            except OSError:  # not cached yet: download once
                proc = Sam2Processor.from_pretrained(self.model_id)
                model = Sam2Model.from_pretrained(self.model_id)
            if self.dtype == "float16":
                model = model.half()  # type: ignore[no-untyped-call]
            self._processor = proc
            self._model = model.to(dev).eval()  # type: ignore[arg-type]
            self.load_ms = round(1000 * (time.perf_counter() - t0))
        if warm_up:
            rgb = np.full((256, 256, 3), 128, dtype=np.uint8)
            self._segment_sync(rgb, [ObjectPrompt(1, (), (64, 64, 192, 192))], key="warm-up")
            with self._lock:
                self._cache.clear()

    def memory(self) -> dict[str, float]:
        """Peak resident memory of this process and the GPU memory torch holds, in MB."""
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # ru_maxrss is in bytes on macOS and in kilobytes on Linux.
        peak_mb = peak / 1e6 if sys.platform == "darwin" else peak / 1e3
        out = {"peak_rss_mb": round(peak_mb, 1)}
        if self.device == "mps":
            import torch

            out["mps_driver_mb"] = round(torch.mps.driver_allocated_memory() / 1e6, 1)
        return out

    async def ready(self) -> tuple[bool, str]:
        if self._model is None:
            return False, "SAM 2.1 is not loaded"
        return True, "ok"

    def _embedding(self, rgb: NDArray[np.uint8], key: str) -> tuple[Any, list[list[int]]]:
        import torch

        hit = self._cache.get(key)
        if hit is not None:
            self._cache.move_to_end(key)
            self.last_embed_cached = True
            self.last_embed_ms = 0
            return hit  # type: ignore[no-any-return]
        t0 = time.perf_counter()
        inputs = self._processor(images=np.array(rgb), return_tensors="pt")
        pixels = inputs["pixel_values"].to(self.device)
        if self.dtype == "float16":
            pixels = pixels.half()
        with torch.no_grad():
            emb = self._model.get_image_embeddings(pixels)
        if self.device == "mps":
            # The MPS allocator keeps the encoder's working memory cached; on an
            # 8 GB machine that pushed the larger checkpoints into swap.
            torch.mps.empty_cache()
        sizes = inputs["original_sizes"].tolist()
        self._cache[key] = (emb, sizes)
        while len(self._cache) > self.cache_images:
            self._cache.popitem(last=False)
        self.last_embed_cached = False
        self.last_embed_ms = round(1000 * (time.perf_counter() - t0))
        return emb, sizes

    def _zoom_rect(
        self, box: tuple[int, int, int, int], w: int, h: int
    ) -> tuple[int, int, int, int]:
        """The region a small object is segmented in: its box plus its size on every side."""
        side = max(box[2] - box[0], box[3] - box[1])
        m = max(side * self.zoom_context, 64)
        return (
            max(0, round(box[0] - m)),
            max(0, round(box[1] - m)),
            min(w, round(box[2] + m)),
            min(h, round(box[3] + m)),
        )

    def _predict_all(
        self,
        emb: Any,
        sizes: list[list[int]],
        box: tuple[int, int, int, int] | None,
        points: Sequence[PointPrompt],
    ) -> tuple[list[NDArray[np.bool_]], list[float]]:
        """SAM's three proposals for one prompt and their predicted IoU (frame of ``sizes``)."""
        import torch

        h, w = int(sizes[0][0]), int(sizes[0][1])
        kw: dict[str, Any] = {"original_sizes": sizes, "return_tensors": "pt"}
        if box is not None:
            kw["input_boxes"] = [[[float(v) for v in box]]]
        if points:
            pts = [[float(min(max(p.x, 0), w - 1)), float(min(max(p.y, 0), h - 1))] for p in points]
            kw["input_points"] = [[pts]]
            kw["input_labels"] = [[[1 if p.positive else 0 for p in points]]]
        prompt = self._processor(**kw)
        fwd = {}
        for k in ("input_points", "input_labels", "input_boxes"):
            if k in prompt:
                t = prompt[k].to(self.device)
                if self.dtype == "float16" and t.is_floating_point():
                    t = t.half()
                fwd[k] = t
        with torch.no_grad():
            res = self._model(image_embeddings=emb, multimask_output=True, **fwd)
        masks = self._processor.post_process_masks(
            res.pred_masks.float().cpu(), prompt["original_sizes"]
        )[0][0]  # (3, H, W) for the one object
        scores = res.iou_scores.cpu().float().numpy().reshape(-1)
        return [np.asarray(m.numpy() > 0) for m in masks], [float(v) for v in scores]

    def _frame(
        self, rgb: NDArray[np.uint8], box: tuple[int, int, int, int] | None, key: str
    ) -> tuple[Any, list[list[int]], tuple[int, int, int, int]]:
        """Embedding and region to segment in: the whole photo, or a zoomed crop."""
        h, w = rgb.shape[:2]
        small = box is not None and max(box[2] - box[0], box[3] - box[1]) < self.zoom_below * max(
            w, h
        )
        if not small or box is None:
            emb, sizes = self._embedding(rgb, key)
            return emb, sizes, (0, 0, w, h)
        # A small object gets most of SAM's 1024 px input to itself: it is
        # segmented in a crop around its box, then placed back.
        x0, y0, x1, y1 = self._zoom_rect(box, w, h)
        crop = np.ascontiguousarray(rgb[y0:y1, x0:x1])
        emb, sizes = self._embedding(crop, f"{key}:{x0},{y0},{x1},{y1}")
        return emb, sizes, (x0, y0, x1, y1)

    def _local_prompt(
        self,
        box: tuple[int, int, int, int] | None,
        points: Sequence[PointPrompt],
        rect: tuple[int, int, int, int],
        full: bool,
    ) -> tuple[tuple[int, int, int, int] | None, list[PointPrompt]]:
        x0, y0, x1, y1 = rect
        local_box = None
        if box is not None:
            pad_x = 0.0 if full else self.box_pad * (box[2] - box[0])
            pad_y = 0.0 if full else self.box_pad * (box[3] - box[1])
            # Boxes drawn by a language model are often a little tight, and SAM
            # will not cross its box: a zoomed object's box gets some room.
            local_box = (
                max(0, round(box[0] - pad_x) - x0),
                max(0, round(box[1] - pad_y) - y0),
                min(x1 - x0, round(box[2] + pad_x) - x0),
                min(y1 - y0, round(box[3] + pad_y) - y0),
            )
        return local_box, [PointPrompt(p.x - x0, p.y - y0, p.positive) for p in points]

    @staticmethod
    def _clip_box(
        box: tuple[int, int, int, int] | None, w: int, h: int
    ) -> tuple[int, int, int, int] | None:
        if box is None:
            return None
        left, top, right, bottom = box
        left, top = max(0, min(left, w - 1)), max(0, min(top, h - 1))
        right, bottom = max(left + 1, min(right, w)), max(top + 1, min(bottom, h))
        return left, top, right, bottom

    def _segment_sync(
        self, rgb: NDArray[np.uint8], objects: Sequence[ObjectPrompt], key: str
    ) -> dict[int, ObjectMask]:
        if self._model is None:
            raise ModelError("SAM 2.1 is not loaded")
        h, w = rgb.shape[:2]
        out: dict[int, ObjectMask] = {}
        with self._lock:
            for obj in objects:
                box = self._clip_box(obj.box, w, h)
                if box is None and not obj.points:
                    continue
                emb, sizes, rect = self._frame(rgb, box, key)
                full = rect == (0, 0, w, h)
                lbox, lpts = self._local_prompt(box, obj.points, rect, full)
                masks, scores = self._predict_all(emb, sizes, lbox, lpts)
                best, cands = choose_mask(masks, scores, lbox)
                local = largest_component(cands[best].mask)
                mask = np.zeros((h, w), dtype=bool)
                mask[rect[1] : rect[3], rect[0] : rect[2]] = local
                mb = mask_box(mask) or (0, 0, 0, 0)
                out[obj.object_id] = ObjectMask(
                    str(obj.object_id), 0, mask, mb, round(cands[best].score, 3)
                )
        return out

    def _candidates_sync(
        self,
        rgb: NDArray[np.uint8],
        box: tuple[int, int, int, int] | None,
        points: Sequence[PointPrompt],
        key: str,
    ) -> list[MaskCandidate]:
        if self._model is None:
            raise ModelError("SAM 2.1 is not loaded")
        h, w = rgb.shape[:2]
        box = self._clip_box(box, w, h)
        variants: list[tuple[str, tuple[int, int, int, int] | None, Sequence[PointPrompt]]] = []
        # The box always goes with the points when there are points: a box alone
        # let a wall leak into the c3 scooters (docs/MODAL.md); alone only when
        # there are no points.
        if box is not None:
            variants.append(("box+points" if points else "box", box, points))
        if any(p.positive for p in points):
            variants.append(("points", None, points))
        out: list[MaskCandidate] = []
        with self._lock:
            if not variants:
                return out
            # One region for every variant, so the zoomed crop's embedding is reused.
            anchor = box
            if anchor is None:
                xs = [p.x for p in points if p.positive]
                ys = [p.y for p in points if p.positive]
                anchor = self._clip_box((min(xs), min(ys), max(xs) + 1, max(ys) + 1), w, h)
            emb, sizes, rect = self._frame(rgb, anchor, key)
            full = rect == (0, 0, w, h)
            for label, vbox, vpts in variants:
                lbox, lpts = self._local_prompt(vbox, vpts, rect, full)
                masks, scores = self._predict_all(emb, sizes, lbox, lpts)
                for i, (m, sc) in enumerate(zip(masks, scores, strict=True)):
                    if not m.any():
                        continue
                    full_mask = np.zeros((h, w), dtype=bool)
                    full_mask[rect[1] : rect[3], rect[0] : rect[2]] = largest_component(m)
                    out.append(MaskCandidate(full_mask, round(sc, 3), label, i))
        return out

    async def segment(self, image: ImageInput, objects: Sequence[ObjectPrompt]) -> MaskResult:
        key = hashlib.sha1(image.jpeg).hexdigest()
        masks = await asyncio.to_thread(self._segment_sync, image.rgb, objects, key)
        return MaskResult(masks, 0.0)

    async def candidates(
        self,
        image: ImageInput,
        box: tuple[int, int, int, int] | None,
        points: Sequence[PointPrompt],
    ) -> list[MaskCandidate]:
        """Every proposal for the box with the points, the box alone and the points alone."""
        key = hashlib.sha1(image.jpeg).hexdigest()
        return await asyncio.to_thread(self._candidates_sync, image.rgb, box, points, key)

    async def track(self, video: VideoInput, concept: str) -> TrackResult:
        # SAM 2.1 tracks objects from prompts on a frame, not from a noun phrase.
        raise ModelError("video tracking by noun phrase needs SAM 3.1")


@dataclass
class Sam31ApiAdapter:
    """SAM 3.1 on the hosted Meta Model API, as a stand-alone mask model."""

    api: MetaApiAdapter
    name: str = "sam3.1-api"

    @property
    def mask_model_name(self) -> str:
        return self.api.mask_model_name

    async def ready(self) -> tuple[bool, str]:
        return await self.api.ready()

    async def segment(self, image: ImageInput, objects: Sequence[ObjectPrompt]) -> MaskResult:
        return await self.api.segment(image, objects)

    async def track(self, video: VideoInput, concept: str) -> TrackResult:
        return await self.api.track(video, concept)


@dataclass
class Sam31LocalAdapter:
    """Slot for SAM 3.1 open weights (gated; access pending).

    When access arrives, implement :meth:`load` and :meth:`segment` exactly as
    :class:`Sam21LocalAdapter` does (the transformers SAM 3 classes take the
    same box and point prompts), and :meth:`track` with SAM 3.1's noun-phrase
    video tracking. Until then it says so instead of failing obscurely.
    """

    model_id: str = "facebook/sam3.1"
    name: str = "sam3.1-local"

    @property
    def mask_model_name(self) -> str:
        return "sam-3.1-open-weights"

    def load(self, warm_up: bool = True) -> None:
        raise ModelError("SAM 3.1 weights are gated and not available yet")

    async def ready(self) -> tuple[bool, str]:
        return False, "SAM 3.1 weights are gated and not available yet"

    async def segment(self, image: ImageInput, objects: Sequence[ObjectPrompt]) -> MaskResult:
        raise ModelError("SAM 3.1 weights are gated and not available yet")

    async def track(self, video: VideoInput, concept: str) -> TrackResult:
        raise ModelError("SAM 3.1 weights are gated and not available yet")


def mask_model_from_config(name: str, *, api: MetaApiAdapter | None = None) -> MaskModel:
    """``sam2.1-modal`` (hosted large, local small as fallback), ``sam2.1-local[:size[:fp16]]``,
    ``sam3.1-api`` or ``sam3.1-local``."""
    if name.startswith("sam2.1-local"):
        # "sam2.1-local", or with a size: "sam2.1-local:large", "sam2.1-local:base-plus:fp16"
        parts = name.split(":")
        size = parts[1] if len(parts) > 1 else os.environ.get("NEERMATTAM_SAM_SIZE", "small")
        fp16 = (len(parts) > 2 and parts[2] == "fp16") or os.environ.get(
            "NEERMATTAM_SAM_FP16"
        ) == "1"
        return Sam21LocalAdapter(size=size, dtype="float16" if fp16 else "float32")
    if name == "sam2.1-modal":
        from .modal_sam import FallbackMasks, ModalSamAdapter

        return FallbackMasks(ModalSamAdapter.from_env(), Sam21LocalAdapter(size="small"))
    if name == "sam3.1-api":
        return Sam31ApiAdapter(api or MetaApiAdapter())
    if name == "sam3.1-local":
        return Sam31LocalAdapter()
    raise ValueError(f"unknown mask model {name!r}")
