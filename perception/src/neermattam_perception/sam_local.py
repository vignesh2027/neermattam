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
import threading
import time
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from numpy.typing import NDArray

from .adapters import MaskModel, MaskResult, MetaApiAdapter, ModelError, TrackResult
from .media import ImageInput, VideoInput
from .sam import ObjectMask, ObjectPrompt, PointPrompt

SAM21_MODEL_ID = "facebook/sam2.1-hiera-small"
SAM21_LABEL = "sam-2.1-hiera-small"


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


@dataclass
class Sam21LocalAdapter:
    """SAM 2.1 open weights, local, behind :class:`~neermattam_perception.adapters.MaskModel`."""

    model_id: str = SAM21_MODEL_ID
    device: str | None = None
    cache_images: int = 4
    # Objects whose box is smaller than this share of the image's longer side are
    # segmented in a zoomed crop with ``zoom_context`` times their size around them.
    zoom_below: float = 0.3
    zoom_context: float = 1.0
    box_pad: float = 0.08
    name: str = "sam2.1-local"
    _model: Any = field(default=None, repr=False)
    _processor: Any = field(default=None, repr=False)
    _cache: OrderedDict[str, Any] = field(default_factory=OrderedDict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    load_ms: int | None = None
    last_embed_ms: int | None = None
    last_embed_cached: bool = False

    @property
    def mask_model_name(self) -> str:
        return SAM21_LABEL

    @property
    def loaded(self) -> bool:
        return self._model is not None

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
            # Prefer the local Hugging Face cache: on a network whose IPv6 does
            # not route, the Hub's freshness check alone can stall for minutes.
            try:
                proc = Sam2Processor.from_pretrained(self.model_id, local_files_only=True)
                model = Sam2Model.from_pretrained(self.model_id, local_files_only=True)
            except OSError:  # not cached yet: download once
                proc = Sam2Processor.from_pretrained(self.model_id)
                model = Sam2Model.from_pretrained(self.model_id)
            self._processor = proc
            self._model = model.to(dev).eval()  # type: ignore[arg-type]
            self.load_ms = round(1000 * (time.perf_counter() - t0))
        if warm_up:
            rgb = np.full((256, 256, 3), 128, dtype=np.uint8)
            self._segment_sync(rgb, [ObjectPrompt(1, (), (64, 64, 192, 192))], key="warm-up")
            with self._lock:
                self._cache.clear()

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
        with torch.no_grad():
            emb = self._model.get_image_embeddings(inputs["pixel_values"].to(self.device))
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

    def _predict(
        self,
        emb: Any,
        sizes: list[list[int]],
        box: tuple[int, int, int, int] | None,
        points: Sequence[PointPrompt],
    ) -> tuple[NDArray[np.bool_], float]:
        """SAM's three proposals for one prompt, and the chosen one (frame of ``sizes``)."""
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
        fwd = {
            k: prompt[k].to(self.device)
            for k in ("input_points", "input_labels", "input_boxes")
            if k in prompt
        }
        with torch.no_grad():
            res = self._model(image_embeddings=emb, multimask_output=True, **fwd)
        masks = self._processor.post_process_masks(res.pred_masks.cpu(), prompt["original_sizes"])[
            0
        ][0]  # (3, H, W) for the one object
        scores = res.iou_scores.cpu().float().numpy().reshape(-1)
        ms = [np.asarray(m.numpy() > 0) for m in masks]
        best, cands = choose_mask(ms, scores.tolist(), box)
        return largest_component(cands[best].mask), round(cands[best].score, 3)

    def _segment_sync(
        self, rgb: NDArray[np.uint8], objects: Sequence[ObjectPrompt], key: str
    ) -> dict[int, ObjectMask]:
        if self._model is None:
            raise ModelError("SAM 2.1 is not loaded")
        h, w = rgb.shape[:2]
        out: dict[int, ObjectMask] = {}
        with self._lock:
            for obj in objects:
                box = None
                if obj.box is not None:
                    left, top, right, bottom = obj.box
                    left, top = max(0, min(left, w - 1)), max(0, min(top, h - 1))
                    right, bottom = max(left + 1, min(right, w)), max(top + 1, min(bottom, h))
                    box = (left, top, right, bottom)
                if box is None and not obj.points:
                    continue
                small = box is not None and max(
                    box[2] - box[0], box[3] - box[1]
                ) < self.zoom_below * max(w, h)
                if not small or box is None:
                    emb, sizes = self._embedding(rgb, key)
                    mask, score = self._predict(emb, sizes, box, obj.points)
                else:
                    # A small object gets most of SAM's 1024 px input to itself: it
                    # is segmented in a crop around its box, then placed back.
                    x0, y0, x1, y1 = self._zoom_rect(box, w, h)
                    crop = np.ascontiguousarray(rgb[y0:y1, x0:x1])
                    emb, sizes = self._embedding(crop, f"{key}:{x0},{y0},{x1},{y1}")
                    # Boxes drawn by a language model are often a little tight, and
                    # SAM will not cross its box: give it some room.
                    pad_x = self.box_pad * (box[2] - box[0])
                    pad_y = self.box_pad * (box[3] - box[1])
                    local_box = (
                        max(0, round(box[0] - pad_x) - x0),
                        max(0, round(box[1] - pad_y) - y0),
                        min(x1 - x0, round(box[2] + pad_x) - x0),
                        min(y1 - y0, round(box[3] + pad_y) - y0),
                    )
                    local_pts = [PointPrompt(p.x - x0, p.y - y0, p.positive) for p in obj.points]
                    local, score = self._predict(emb, sizes, local_box, local_pts)
                    mask = np.zeros((h, w), dtype=bool)
                    mask[y0:y1, x0:x1] = local
                mb = mask_box(mask) or (0, 0, 0, 0)
                out[obj.object_id] = ObjectMask(str(obj.object_id), 0, mask, mb, score)
        return out

    async def segment(self, image: ImageInput, objects: Sequence[ObjectPrompt]) -> MaskResult:
        key = hashlib.sha1(image.jpeg).hexdigest()
        masks = await asyncio.to_thread(self._segment_sync, image.rgb, objects, key)
        return MaskResult(masks, 0.0)

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
    """``sam2.1-local`` (default), ``sam3.1-api`` or ``sam3.1-local``."""
    if name == "sam2.1-local":
        return Sam21LocalAdapter()
    if name == "sam3.1-api":
        return Sam31ApiAdapter(api or MetaApiAdapter())
    if name == "sam3.1-local":
        return Sam31LocalAdapter()
    raise ValueError(f"unknown mask model {name!r}")
