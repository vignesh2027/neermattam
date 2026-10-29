"""SAM 2.1 hosted on Modal (``deploy/modal-sam``), behind :class:`MaskModel`.

The service is described in ``docs/MODAL.md``: ``POST /segment`` with a base64
image and a list of prompts (box, positive and negative points), answered with
up to three candidate masks per prompt as COCO run-length encoding, all from
one image embedding. This adapter speaks the same protocol as
``deploy/modal-sam/client.py`` (kept self-contained, so the perception package
does not import from ``deploy/``) and adds what perception needs:

* small objects are sent as a zoomed crop around their box, which gives the
  model more pixels for a wheel and makes the upload smaller; large regions
  (the water) are sent as the whole photo, shrunk to ``max_side``;
* each candidate keeps only its largest connected component, as the local
  adapter does;
* every failure becomes a :class:`ModelError`, so :class:`FallbackMasks` can
  switch to the local model.

``MODAL_SAM_URL`` and ``MODAL_SAM_TOKEN`` come from the environment or the
repository ``.env``; they are never logged.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import cv2
import httpx
import numpy as np
from numpy.typing import NDArray
from PIL import Image

from .adapters import MaskResult, ModelError, TrackResult
from .media import ImageInput, VideoInput
from .muse_open import env_from_dotenv
from .sam import ObjectMask, ObjectPrompt, PointPrompt
from .sam_local import MaskCandidate, choose_mask, largest_component, mask_box

Box = tuple[int, int, int, int]


def rle_decode(rle: dict[str, Any]) -> NDArray[np.bool_]:
    """COCO uncompressed RLE (column-major, zeros first) to a boolean mask."""
    h, w = (int(v) for v in rle["size"])
    counts = np.asarray(rle["counts"], dtype=np.int64)
    if counts.sum() != h * w:
        raise ValueError(f"RLE counts sum to {int(counts.sum())}, expected {h * w}")
    values = np.zeros(counts.size, dtype=bool)
    values[1::2] = True
    return np.repeat(values, counts).reshape((h, w), order="F")


@dataclass(frozen=True, slots=True)
class _Frame:
    rect: Box  # region of the photo sent
    scale: float  # sent pixels per photo pixel
    jpeg: bytes


@dataclass
class ModalSamAdapter:
    """SAM 2.1 (hiera-large by default) on the Modal service."""

    url: str = ""
    token: str = field(default="", repr=False)
    model: str = "large"
    timeout_s: float = 120.0
    zoom_below: float = 0.3
    zoom_context: float = 1.0
    box_pad: float = 0.08
    max_side: int = 1024
    jpeg_quality: int = 90
    transport: httpx.AsyncBaseTransport | None = None
    name: str = "sam2.1-modal"
    last_timings_ms: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_env(cls, **kw: Any) -> ModalSamAdapter:
        names = ("MODAL_SAM_URL", "MODAL_SAM_TOKEN")
        env = {n: os.environ[n] for n in names if os.environ.get(n)}
        if len(env) < len(names):
            env = {**env_from_dotenv(names), **env}
        return cls(url=env.get("MODAL_SAM_URL", ""), token=env.get("MODAL_SAM_TOKEN", ""), **kw)

    @property
    def configured(self) -> bool:
        return bool(self.url and self.token)

    @property
    def mask_model_name(self) -> str:
        return f"sam-2.1-hiera-{self.model} (Modal)"

    def _client(self) -> httpx.AsyncClient:
        if not self.configured:
            raise ModelError("MODAL_SAM_URL and MODAL_SAM_TOKEN are not set")
        transport = self.transport or httpx.AsyncHTTPTransport(local_address="0.0.0.0")
        return httpx.AsyncClient(
            base_url=self.url.rstrip("/"),
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=httpx.Timeout(self.timeout_s, connect=15.0),
            transport=transport,
        )

    async def ready(self) -> tuple[bool, str]:
        if not self.configured:
            return False, "MODAL_SAM_URL and MODAL_SAM_TOKEN are not set"

        async def get() -> httpx.Response:
            async with self._client() as c:
                return await c.get("/health")

        try:
            r = await asyncio.wait_for(get(), self.timeout_s + 15.0)
        except TimeoutError:
            return False, "Modal SAM did not answer in time"
        except httpx.HTTPError as exc:
            return False, f"Modal SAM unreachable: {type(exc).__name__}"
        if r.status_code != 200:
            return False, f"Modal SAM returned HTTP {r.status_code}"
        return True, "ok"

    # ------------------------------------------------------------------ framing

    def _frame(self, image: ImageInput, box: Box | None) -> _Frame:
        w, h = image.width, image.height
        rect: Box = (0, 0, w, h)
        if box is not None and max(box[2] - box[0], box[3] - box[1]) < self.zoom_below * max(w, h):
            side = max(box[2] - box[0], box[3] - box[1])
            m = max(side * self.zoom_context, 64)
            rect = (
                max(0, round(box[0] - m)),
                max(0, round(box[1] - m)),
                min(w, round(box[2] + m)),
                min(h, round(box[3] + m)),
            )
        crop = image.rgb[rect[1] : rect[3], rect[0] : rect[2]]
        s = min(1.0, self.max_side / max(crop.shape[:2]))
        im = Image.fromarray(np.ascontiguousarray(crop))
        if s < 1.0:
            im = im.resize((max(1, round(crop.shape[1] * s)), max(1, round(crop.shape[0] * s))))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=self.jpeg_quality)
        return _Frame(rect, s, buf.getvalue())

    def _prompt_json(
        self,
        pid: str,
        frame: _Frame,
        box: Box | None,
        points: Sequence[PointPrompt],
        pad: bool,
    ) -> dict[str, Any]:
        x0, y0 = frame.rect[0], frame.rect[1]
        s = frame.scale
        d: dict[str, Any] = {"id": pid}
        if box is not None:
            px = self.box_pad * (box[2] - box[0]) if pad else 0.0
            py = self.box_pad * (box[3] - box[1]) if pad else 0.0
            d["box"] = [
                (box[0] - px - x0) * s,
                (box[1] - py - y0) * s,
                (box[2] + px - x0) * s,
                (box[3] + py - y0) * s,
            ]
        pos = [[(p.x - x0) * s, (p.y - y0) * s] for p in points if p.positive]
        neg = [[(p.x - x0) * s, (p.y - y0) * s] for p in points if not p.positive]
        if pos:
            d["positive_points"] = pos
        if neg:
            d["negative_points"] = neg
        return d

    def _place(self, small: NDArray[np.bool_], frame: _Frame, w: int, h: int) -> NDArray[np.bool_]:
        x0, y0, x1, y1 = frame.rect
        if small.shape != (y1 - y0, x1 - x0):
            small = (
                cv2.resize(
                    small.astype(np.uint8), (x1 - x0, y1 - y0), interpolation=cv2.INTER_NEAREST
                )
                > 0
            )
        out = np.zeros((h, w), dtype=bool)
        out[y0:y1, x0:x1] = largest_component(small)
        return out

    async def _segment_frame(
        self, frame: _Frame, prompts: list[dict[str, Any]]
    ) -> dict[str, list[tuple[NDArray[np.bool_], float, int]]]:
        payload = {
            "image_b64": base64.b64encode(frame.jpeg).decode("ascii"),
            "prompts": prompts,
            "model": self.model,
            "max_masks": 3,
        }
        t0 = time.perf_counter()

        async def post() -> httpx.Response:
            async with self._client() as c:
                return await c.post("/segment", content=json.dumps(payload).encode())

        try:
            r = await asyncio.wait_for(post(), self.timeout_s + 15.0)
        except TimeoutError as exc:
            raise ModelError(f"Modal SAM took longer than {self.timeout_s + 15:.0f} s") from exc
        except httpx.HTTPError as exc:
            raise ModelError(f"Modal SAM request failed: {type(exc).__name__}") from exc
        if r.status_code != 200:
            detail = ""
            with contextlib.suppress(json.JSONDecodeError, AttributeError):
                detail = str(r.json().get("detail", ""))[:160]
            raise ModelError(f"Modal SAM failed with HTTP {r.status_code}: {detail}".rstrip(": "))
        try:
            raw = r.json()
            self.last_timings_ms = {
                **{k: float(v) for k, v in (raw.get("timings_ms") or {}).items()},
                "round_trip": round(1000 * (time.perf_counter() - t0), 1),
            }
            out: dict[str, list[tuple[NDArray[np.bool_], float, int]]] = {}
            decoded = await asyncio.to_thread(
                lambda: [
                    (
                        str(res["id"]),
                        [
                            (rle_decode(cand["rle"]), float(cand["score"]), int(cand["index"]))
                            for cand in res["candidates"]
                        ],
                    )
                    for res in raw["results"]
                ]
            )
            for pid, cands in decoded:
                out[pid] = cands
            return out
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelError("Modal SAM returned an unexpected shape") from exc

    # ------------------------------------------------------------------ MaskModel

    async def segment(self, image: ImageInput, objects: Sequence[ObjectPrompt]) -> MaskResult:
        w, h = image.width, image.height
        out: dict[int, ObjectMask] = {}
        # Objects that share a frame go in one request (one embedding).
        groups: dict[Box, tuple[_Frame, list[ObjectPrompt]]] = {}
        for obj in objects:
            if obj.box is None and not any(p.positive for p in obj.points):
                continue
            frame = self._frame(image, obj.box)
            groups.setdefault(frame.rect, (frame, []))[1].append(obj)
        for frame, objs in groups.values():
            zoomed = frame.rect != (0, 0, w, h)
            prompts = [
                self._prompt_json(str(o.object_id), frame, o.box, o.points, zoomed) for o in objs
            ]
            res = await self._segment_frame(frame, prompts)
            for o in objs:
                cands = res.get(str(o.object_id), [])
                if not cands:
                    continue
                masks = [self._place(m, frame, w, h) for m, _, _ in cands]
                best, scored = choose_mask(masks, [sc for _, sc, _ in cands], o.box)
                m = scored[best].mask
                out[o.object_id] = ObjectMask(
                    str(o.object_id),
                    0,
                    m,
                    mask_box(m) or (0, 0, 0, 0),
                    round(scored[best].score, 3),
                )
        return MaskResult(out, 0.0)

    async def candidates(
        self, image: ImageInput, box: Box | None, points: Sequence[PointPrompt]
    ) -> list[MaskCandidate]:
        """Every proposal for the box with the points, and for the points alone.

        The box is always sent with the points when there are points (a box
        alone let a wall leak into the c3 scooters); it is sent alone only when
        there are no points.
        """
        w, h = image.width, image.height
        anchor = box
        if anchor is None:
            pos = [p for p in points if p.positive]
            if not pos:
                return []
            anchor = (
                min(p.x for p in pos),
                min(p.y for p in pos),
                max(p.x for p in pos) + 1,
                max(p.y for p in pos) + 1,
            )
        frame = self._frame(image, anchor)
        zoomed = frame.rect != (0, 0, w, h)
        variants: list[tuple[str, Box | None, Sequence[PointPrompt]]] = []
        if box is not None:
            variants.append(("box+points" if points else "box", box, points))
        if any(p.positive for p in points):
            variants.append(("points", None, points))
        prompts = [self._prompt_json(label, frame, vb, vp, zoomed) for label, vb, vp in variants]
        res = await self._segment_frame(frame, prompts)
        out = []
        for label, _, _ in variants:
            for m, sc, idx in res.get(label, []):
                if m.any():
                    out.append(MaskCandidate(self._place(m, frame, w, h), round(sc, 3), label, idx))
        return out

    async def region_candidates(
        self,
        image: ImageInput,
        region: Box,
        prompts: Sequence[tuple[str, Box | None, Sequence[PointPrompt]]],
    ) -> list[MaskCandidate]:
        """Every proposal for many prompts inside one region, in one request."""
        w, h = image.width, image.height
        x0, y0 = max(0, region[0]), max(0, region[1])
        x1, y1 = min(w, region[2]), min(h, region[3])
        crop = image.rgb[y0:y1, x0:x1]
        s = min(1.0, self.max_side / max(crop.shape[:2]))
        im = Image.fromarray(np.ascontiguousarray(crop))
        if s < 1.0:
            im = im.resize((max(1, round(crop.shape[1] * s)), max(1, round(crop.shape[0] * s))))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=self.jpeg_quality)
        frame = _Frame((x0, y0, x1, y1), s, buf.getvalue())
        payload = [
            self._prompt_json(f"{i}:{label}", frame, box, pts, False)
            for i, (label, box, pts) in enumerate(prompts)
        ]
        res = await self._segment_frame(frame, payload)
        out = []
        for i, (label, _, _) in enumerate(prompts):
            for m, sc, idx in res.get(f"{i}:{label}", []):
                if m.any():
                    out.append(MaskCandidate(self._place(m, frame, w, h), round(sc, 3), label, idx))
        return out

    async def track(self, video: VideoInput, concept: str) -> TrackResult:
        raise ModelError("video tracking by noun phrase needs SAM 3.1")


@dataclass
class FallbackMasks:
    """A primary mask model (Modal) with a local fallback, loaded only when needed.

    After a primary failure the fallback serves for ``cooldown_s`` before the
    primary is tried again. ``mask_model_name`` names whichever served last, so
    the trace stays truthful.
    """

    primary: Any
    fallback: Any
    cooldown_s: float = 120.0
    name: str = "sam-with-fallback"
    _down_until: float = 0.0
    _last: str = ""
    failures: list[str] = field(default_factory=list)

    @property
    def mask_model_name(self) -> str:
        return self._last or str(self.primary.mask_model_name)

    def load(self) -> None:
        """Nothing to load up front: the primary is remote and the fallback is lazy."""

    async def ready(self) -> tuple[bool, str]:
        ok, why = await self.primary.ready()
        if ok:
            return ok, why
        return True, f"primary unavailable ({why}); local fallback in use"

    def _use_primary(self) -> bool:
        return time.monotonic() >= self._down_until

    async def _fallback(self, exc: ModelError) -> Any:
        self.failures.append(str(exc))
        self._down_until = time.monotonic() + self.cooldown_s
        if not getattr(self.fallback, "loaded", True):
            await asyncio.to_thread(self.fallback.load)
        self._last = f"{self.fallback.mask_model_name} (fallback: {exc})"
        return self.fallback

    async def segment(self, image: ImageInput, objects: Sequence[ObjectPrompt]) -> MaskResult:
        if self._use_primary():
            try:
                res = await self.primary.segment(image, objects)
                self._last = str(self.primary.mask_model_name)
                return res  # type: ignore[no-any-return]
            except ModelError as exc:
                fb = await self._fallback(exc)
                return await fb.segment(image, objects)  # type: ignore[no-any-return]
        if not getattr(self.fallback, "loaded", True):
            await asyncio.to_thread(self.fallback.load)
        return await self.fallback.segment(image, objects)  # type: ignore[no-any-return]

    async def candidates(
        self, image: ImageInput, box: Box | None, points: Sequence[PointPrompt]
    ) -> list[MaskCandidate]:
        if self._use_primary():
            try:
                res = await self.primary.candidates(image, box, points)
                self._last = str(self.primary.mask_model_name)
                return res  # type: ignore[no-any-return]
            except ModelError as exc:
                fb = await self._fallback(exc)
                return await fb.candidates(image, box, points)  # type: ignore[no-any-return]
        if not getattr(self.fallback, "loaded", True):
            await asyncio.to_thread(self.fallback.load)
        return await self.fallback.candidates(image, box, points)  # type: ignore[no-any-return]

    async def region_candidates(
        self,
        image: ImageInput,
        region: Box,
        prompts: Sequence[tuple[str, Box | None, Sequence[PointPrompt]]],
    ) -> list[MaskCandidate]:
        if self._use_primary():
            try:
                res = await self.primary.region_candidates(image, region, prompts)
                self._last = str(self.primary.mask_model_name)
                return res  # type: ignore[no-any-return]
            except ModelError as exc:
                fb = await self._fallback(exc)
                return await fb.region_candidates(image, region, prompts)  # type: ignore[no-any-return]
        if not getattr(self.fallback, "loaded", True):
            await asyncio.to_thread(self.fallback.load)
        return await self.fallback.region_candidates(image, region, prompts)  # type: ignore[no-any-return]

    async def track(self, video: VideoInput, concept: str) -> TrackResult:
        return await self.fallback.track(video, concept)  # type: ignore[no-any-return]
