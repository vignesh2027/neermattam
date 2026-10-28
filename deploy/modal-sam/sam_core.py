"""Model-side logic of the Neermattam SAM service, free of Modal and FastAPI.

The same code runs inside the Modal GPU container and on a laptop (CPU or MPS),
which is how the hosted and local results are compared like for like.

* :func:`parse_prompts` validates the request's prompt list.
* :func:`rle_encode` turns a boolean mask into COCO-style uncompressed RLE.
* :class:`SamRunner` loads one checkpoint, computes the image embedding once
  per request and decodes every prompt against it.

``torch`` and ``transformers`` are imported only when a model loads.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

# Request key: (Hugging Face repository, model family, public model name).
MODELS: dict[str, tuple[str, str, str]] = {
    "large": ("facebook/sam2.1-hiera-large", "sam2", "sam-2.1-hiera-large"),
    "base-plus": ("facebook/sam2.1-hiera-base-plus", "sam2", "sam-2.1-hiera-base-plus"),
    "small": ("facebook/sam2.1-hiera-small", "sam2", "sam-2.1-hiera-small"),
    # Gated. Loaded only when HF_TOKEN has been granted access (see docs/MODAL.md).
    "sam3.1": ("facebook/sam3.1", "sam3", "sam-3.1"),
    "sam3": ("facebook/sam3", "sam3", "sam-3"),
}
DEFAULT_MODEL = "large"
GATED = {"sam3.1", "sam3"}
MAX_PROMPTS = 32
MAX_POINTS = 16
MAX_SIDE = 8192


class PromptError(ValueError):
    """The request's prompts are malformed (answered with HTTP 422)."""


class ModelUnavailable(RuntimeError):
    """The requested model cannot be loaded here (answered with HTTP 503)."""


# --------------------------------------------------------------------------- prompts


@dataclass(frozen=True, slots=True)
class Prompt:
    """One object to segment. Coordinates are pixels of the uploaded image.

    ``box`` is ``(x1, y1, x2, y2)``; points are ``(x, y)``.
    """

    id: str | int
    box: tuple[float, float, float, float] | None = None
    positive_points: tuple[tuple[float, float], ...] = ()
    negative_points: tuple[tuple[float, float], ...] = ()


def _point_list(raw: Any, where: str) -> tuple[tuple[float, float], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or len(raw) > MAX_POINTS:
        raise PromptError(f"{where} must be a list of at most {MAX_POINTS} [x, y] pairs")
    out = []
    for p in raw:
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            raise PromptError(f"{where} entries must be [x, y]")
        try:
            out.append((float(p[0]), float(p[1])))
        except (TypeError, ValueError) as exc:
            raise PromptError(f"{where} entries must be numbers") from exc
    return tuple(out)


def parse_prompts(raw: Any, width: int, height: int) -> list[Prompt]:
    """Validate the request's ``prompts`` list and clamp coordinates into the image."""
    if not isinstance(raw, list) or not raw:
        raise PromptError("prompts must be a non-empty list")
    if len(raw) > MAX_PROMPTS:
        raise PromptError(f"at most {MAX_PROMPTS} prompts per request")
    seen: set[str] = set()
    out: list[Prompt] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise PromptError(f"prompt {i} must be an object")
        pid = item.get("id", i)
        if not isinstance(pid, (str, int)) or isinstance(pid, bool):
            raise PromptError(f"prompt {i}: id must be a string or an integer")
        if str(pid) in seen:
            raise PromptError(f"prompt id {pid!r} is repeated")
        seen.add(str(pid))
        box = None
        if item.get("box") is not None:
            b = item["box"]
            if not isinstance(b, (list, tuple)) or len(b) != 4:
                raise PromptError(f"prompt {pid!r}: box must be [x1, y1, x2, y2]")
            try:
                x1, y1, x2, y2 = (float(v) for v in b)
            except (TypeError, ValueError) as exc:
                raise PromptError(f"prompt {pid!r}: box values must be numbers") from exc
            x1, x2 = sorted((min(max(x1, 0.0), width), min(max(x2, 0.0), width)))
            y1, y2 = sorted((min(max(y1, 0.0), height), min(max(y2, 0.0), height)))
            if x2 - x1 < 1 or y2 - y1 < 1:
                raise PromptError(f"prompt {pid!r}: box is empty inside the image")
            box = (x1, y1, x2, y2)

        def clamp(pts: tuple[tuple[float, float], ...]) -> tuple[tuple[float, float], ...]:
            return tuple(
                (min(max(x, 0.0), width - 1.0), min(max(y, 0.0), height - 1.0)) for x, y in pts
            )

        pos = clamp(_point_list(item.get("positive_points"), f"prompt {pid!r}: positive_points"))
        neg = clamp(_point_list(item.get("negative_points"), f"prompt {pid!r}: negative_points"))
        if box is None and not pos:
            raise PromptError(f"prompt {pid!r}: give a box or at least one positive point")
        out.append(Prompt(pid, box, pos, neg))
    return out


# --------------------------------------------------------------------------- masks


def rle_encode(mask: NDArray[np.bool_]) -> dict[str, Any]:
    """COCO-style uncompressed RLE: column-major run lengths, starting with a run of zeros.

    ``pycocotools.mask.frPyObjects`` reads it as is, and :func:`client.rle_decode`
    inverts it with numpy alone.
    """
    h, w = mask.shape
    flat = np.asarray(mask, dtype=np.uint8).ravel(order="F")
    if flat.size == 0:
        return {"size": [h, w], "counts": []}
    change = np.flatnonzero(np.diff(flat)) + 1
    bounds = np.concatenate(([0], change, [flat.size]))
    counts = np.diff(bounds).tolist()
    if flat[0] == 1:
        counts = [0, *counts]
    return {"size": [h, w], "counts": counts}


def mask_bbox(mask: NDArray[np.bool_]) -> list[int] | None:
    """``[x1, y1, x2, y2]`` with exclusive ``x2`` and ``y2``, or None for an empty mask."""
    rows = np.flatnonzero(mask.any(axis=1))
    if rows.size == 0:
        return None
    cols = np.flatnonzero(mask.any(axis=0))
    return [int(cols[0]), int(rows[0]), int(cols[-1]) + 1, int(rows[-1]) + 1]


# --------------------------------------------------------------------------- runner


@dataclass
class SamRunner:
    """One SAM checkpoint on one device.

    ``source`` overrides where the weights load from (a local folder with
    ``config.json`` and ``model.safetensors``); otherwise the Hugging Face
    repository for ``key`` is used, with ``token`` for gated ones.
    """

    key: str = DEFAULT_MODEL
    device: str | None = None
    source: str | None = None
    token: str | None = None
    _model: Any = field(default=None, repr=False)
    _processor: Any = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    load_ms: int | None = None

    def __post_init__(self) -> None:
        if self.key not in MODELS:
            raise ModelUnavailable(f"unknown model {self.key!r}; choose from {sorted(MODELS)}")

    @property
    def repo(self) -> str:
        return MODELS[self.key][0]

    @property
    def family(self) -> str:
        return MODELS[self.key][1]

    @property
    def model_name(self) -> str:
        return MODELS[self.key][2]

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def load(self) -> None:
        with self._lock:
            if self._model is not None:
                return
            t0 = time.perf_counter()
            import torch
            import transformers

            if self.device is None:
                if torch.cuda.is_available():
                    self.device = "cuda"
                elif torch.backends.mps.is_available():
                    self.device = "mps"
                else:
                    self.device = "cpu"
            if self.family == "sam2":
                model_cls, proc_cls = transformers.Sam2Model, transformers.Sam2Processor
            else:
                model_cls = transformers.Sam3TrackerModel
                proc_cls = transformers.Sam3TrackerProcessor
            src = self.source or self.repo
            kw: dict[str, Any] = {}
            if self.key in GATED:
                if not self.token:
                    raise ModelUnavailable(
                        f"{self.repo} is gated: add HF_TOKEN with access to the "
                        "neermattam-hf Modal secret"
                    )
                kw["token"] = self.token
            try:
                proc = proc_cls.from_pretrained(src, **kw)
                model = model_cls.from_pretrained(src, **kw)
            except OSError as exc:
                # facebook/sam3.1 ships only the original-format checkpoint
                # (sam3.1_multiplex.pt), which transformers cannot read yet.
                raise ModelUnavailable(f"cannot load {self.repo}: {exc}") from exc
            self._processor = proc
            self._model = model.to(self.device).eval()
            self.load_ms = round(1000 * (time.perf_counter() - t0))

    def _autocast(self, precision: str) -> Any:
        import contextlib

        import torch

        if precision == "bf16" and self.device == "cuda":
            return torch.autocast(device_type="cuda", dtype=torch.bfloat16)
        return contextlib.nullcontext()

    def segment(
        self,
        rgb: NDArray[np.uint8],
        prompts: list[Prompt],
        max_masks: int = 3,
        precision: str = "fp32",
    ) -> dict[str, Any]:
        """Embed ``rgb`` once and decode every prompt against that embedding.

        Returns ``{"results": [...], "timings_ms": {...}}``. Each result holds
        up to ``max_masks`` candidates sorted by SAM's predicted IoU, each with
        the full-image boolean ``mask``, ``score``, ``bbox`` and ``area``.
        """
        import torch

        self.load()
        h, w = rgb.shape[:2]
        t0 = time.perf_counter()
        with self._lock, torch.inference_mode(), self._autocast(precision):
            img = rgb if rgb.flags.writeable else rgb.copy()
            inputs = self._processor(images=img, return_tensors="pt")
            pixels = inputs["pixel_values"].to(self.device)
            emb = self._model.get_image_embeddings(pixels)
            sizes = inputs["original_sizes"].tolist()
            if self.device == "cuda":
                torch.cuda.synchronize()
            t1 = time.perf_counter()
            results = []
            for p in prompts:
                kw: dict[str, Any] = {"original_sizes": sizes, "return_tensors": "pt"}
                if p.box is not None:
                    kw["input_boxes"] = [[list(p.box)]]
                pts = [*p.positive_points, *p.negative_points]
                if pts:
                    kw["input_points"] = [[[list(xy) for xy in pts]]]
                    labels = [1] * len(p.positive_points) + [0] * len(p.negative_points)
                    kw["input_labels"] = [[labels]]
                enc = self._processor(**kw)
                fwd = {
                    k: enc[k].to(self.device)
                    for k in ("input_points", "input_labels", "input_boxes")
                    if k in enc
                }
                out = self._model(image_embeddings=emb, multimask_output=True, **fwd)
                masks = self._processor.post_process_masks(
                    out.pred_masks.float(), enc["original_sizes"]
                )[0][0]  # (3, H, W) bool for the one object
                masks_np = masks.cpu().numpy()
                scores = out.iou_scores.float().cpu().numpy().reshape(-1)
                order = np.argsort(-scores)[:max_masks]
                cands = []
                for i in order:
                    m = masks_np[i]
                    cands.append(
                        {
                            "index": int(i),
                            "score": round(float(scores[i]), 4),
                            "mask": m,
                            "bbox": mask_bbox(m),
                            "area": int(m.sum()),
                        }
                    )
                results.append({"id": p.id, "candidates": cands})
            if self.device == "cuda":
                torch.cuda.synchronize()
            t2 = time.perf_counter()
        return {
            "results": results,
            "image_size": [w, h],
            "timings_ms": {
                "embed": round(1000 * (t1 - t0), 1),
                "decode": round(1000 * (t2 - t1), 1),
            },
        }


def hf_token() -> str | None:
    """The Hugging Face token from the environment, if one is set and non-empty."""
    tok = os.environ.get("HF_TOKEN", "").strip()
    return tok or None


def baked_dir(root: Path, key: str) -> Path | None:
    """The folder a checkpoint was baked into at image build time, if present."""
    d = root / MODELS[key][0].split("/")[-1]
    return d if (d / "config.json").is_file() else None
