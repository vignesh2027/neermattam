"""Model adapters: the scene model (Muse Spark 1.3) and the mask model (SAM 3.1).

Two interfaces keep the pipeline independent of where the models run:

* :class:`SceneModel` reads the photo: is standing water visible, which
  reference objects are there (each with a pixel point and box), where the
  water surface meets the chosen wheel, what is floating, and how good the
  image is. Its points become SAM prompts, and its waterline is a second,
  independent estimate that we compare with the mask-derived one.
* :class:`MaskModel` turns point and box prompts into masks (all objects in one
  request, up to 16) and tracks a noun phrase through a video.

:class:`MetaApiAdapter` implements both against the hosted Meta Model API.
:class:`OpenWeightsAdapter` is the self-hosted path (SAM 3.1 weights plus Muse
Glimmer on Modal) and is a stub for now.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

import httpx
from meta_sam_parser import ResponsesStreamError

from . import sam
from .media import ImageInput, VideoInput
from .references import RefKind, vehicles
from .sam import ObjectMask, ObjectPrompt

R = TypeVar("R")

SCENE_MODEL = "muse-spark-1.3"
# Standard-mode prices per million tokens (https://dev.meta.ai/docs/pricing-rate-limits).
MUSE_SPARK_INPUT_USD_PER_M = 1.25
MUSE_SPARK_OUTPUT_USD_PER_M = 4.25


class ModelError(RuntimeError):
    """A model call failed; the message is safe to show (it never contains the key)."""


# --------------------------------------------------------------------------- data


@dataclass(frozen=True, slots=True)
class SceneObject:
    kind: RefKind
    point: tuple[int, int]  # a pixel on the object (for a wheel, on the tyre)
    box: tuple[int, int, int, int] | None  # left, top, right, bottom (half-open)
    rim_point: tuple[int, int] | None = None
    vehicle: str | None = None  # catalogue key, or None when unknown
    position: str | None = None  # "front", "rear" or None
    confidence: float = 0.5


@dataclass(frozen=True, slots=True)
class ImageQuality:
    score: float  # 0 (useless) to 1 (clear)
    blurred: bool = False
    too_dark: bool = False
    issues: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SceneReading:
    water_visible: bool
    water_points: tuple[tuple[int, int], ...]
    references: tuple[SceneObject, ...]
    waterline_points: tuple[tuple[int, int], ...]  # where water meets references[0]
    floating_debris: str | None
    quality: ImageQuality
    cost_usd: float = 0.0


@dataclass(frozen=True, slots=True)
class MaskResult:
    masks: dict[int, ObjectMask]
    cost_usd: float = 0.0
    problems: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TrackResult:
    tracks: dict[str, list[tuple[int, float, float]]]  # object id: (frame, x, y) in video px
    frames_billed: int
    cost_usd: float = 0.0
    problems: tuple[str, ...] = ()


class SceneModel(Protocol):
    name: str

    async def read_scene(self, image: ImageInput) -> SceneReading: ...

    async def ready(self) -> tuple[bool, str]: ...


class MaskModel(Protocol):
    name: str

    async def segment(self, image: ImageInput, objects: Sequence[ObjectPrompt]) -> MaskResult: ...

    async def track(self, video: VideoInput, concept: str) -> TrackResult: ...

    async def ready(self) -> tuple[bool, str]: ...


# --------------------------------------------------------------------------- scene prompt

_KINDS = [k.value for k in RefKind]


def scene_schema() -> dict[str, Any]:
    """JSON schema for Muse Spark's structured output.

    Coordinates use Muse Spark's documented grounding convention: a normalised
    0 to 1000 grid over the image sent, (0, 0) top-left.
    """
    point = {
        "type": "object",
        "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
        "required": ["x", "y"],
        "additionalProperties": False,
    }
    box = {
        "type": "object",
        "properties": {k: {"type": "integer"} for k in ("x_min", "y_min", "x_max", "y_max")},
        "required": ["x_min", "y_min", "x_max", "y_max"],
        "additionalProperties": False,
    }
    reference = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": _KINDS},
            "point": point,
            "rim_point": point,
            "box": box,
            "vehicle": {"type": "string", "enum": [*sorted(vehicles()), "unknown"]},
            "position": {"type": "string", "enum": ["front", "rear", "unknown"]},
            "confidence": {"type": "number"},
        },
        "required": ["kind", "point", "rim_point", "box", "vehicle", "position", "confidence"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "water_visible": {"type": "boolean"},
            "water_points": {"type": "array", "items": point, "maxItems": 3},
            "references": {"type": "array", "items": reference, "maxItems": 6},
            "waterline_points": {"type": "array", "items": point, "maxItems": 2},
            "floating_debris": {"type": "string"},
            "image_quality": {
                "type": "object",
                "properties": {
                    "score": {"type": "number"},
                    "blurred": {"type": "boolean"},
                    "too_dark": {"type": "boolean"},
                    "issues": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["score", "blurred", "too_dark", "issues"],
                "additionalProperties": False,
            },
        },
        "required": [
            "water_visible",
            "water_points",
            "references",
            "waterline_points",
            "floating_debris",
            "image_quality",
        ],
        "additionalProperties": False,
    }


SCENE_INSTRUCTIONS = """You are reading a street photo taken during urban flooding in India.
Use coordinates on a normalized 0-1000 grid over this image, (0,0) top-left, (1000,1000) bottom-right.

Report:
1. water_visible: true only if standing or flowing floodwater covers part of the road or ground.
2. water_points: up to 3 points that lie on the water surface, away from objects.
3. references: up to 6 objects of known size that stand in the water, best first. A wheel is the best reference when part of it is above the water. For each:
   - kind: two_wheeler_wheel, car_wheel, auto_wheel (auto-rickshaw), bus_wheel, kerb or person.
   - point: a point on the dark tyre rubber above the water, not on the rim.
   - rim_point: a point on the metal rim or hub above the water (for non-wheels, repeat point).
   - box: the bounding box of the whole visible wheel (tyre and rim) above the water.
   - vehicle: the exact model if you can tell from the bodywork, otherwise unknown.
   - position: front or rear wheel, or unknown.
   - confidence: 0 to 1.
4. waterline_points: for the first reference, the two points where the water surface meets its left and right edges. Empty if unsure.
5. floating_debris: a short noun phrase for objects floating on the water (for example "plastic bottle"), or an empty string.
6. image_quality: score 0 to 1, blurred, too_dark, and short issue phrases.
Omit anything you cannot see. Do not guess hidden objects."""


def _px(p: dict[str, Any], w: int, h: int) -> tuple[int, int]:
    x = round(float(p["x"]) * w / 1000.0)
    y = round(float(p["y"]) * h / 1000.0)
    return min(max(x, 0), w - 1), min(max(y, 0), h - 1)


def parse_scene_json(
    data: dict[str, Any], width: int, height: int, cost: float = 0.0
) -> SceneReading:
    """Convert Muse Spark's structured answer (0 to 1000 grid) to pixel coordinates."""
    refs = []
    for r in data.get("references", [])[:6]:
        try:
            kind = RefKind(r["kind"])
        except (KeyError, ValueError):
            continue
        b = r.get("box")
        box = None
        if isinstance(b, dict):
            x0, y0 = _px({"x": b["x_min"], "y": b["y_min"]}, width, height)
            x1, y1 = _px({"x": b["x_max"], "y": b["y_max"]}, width, height)
            if x1 > x0 and y1 > y0:
                box = (x0, y0, x1 + 1, y1 + 1)
        vehicle = r.get("vehicle")
        pos = r.get("position")
        refs.append(
            SceneObject(
                kind=kind,
                point=_px(r["point"], width, height),
                box=box,
                rim_point=_px(r["rim_point"], width, height) if r.get("rim_point") else None,
                vehicle=vehicle if vehicle and vehicle != "unknown" else None,
                position=pos if pos in ("front", "rear") else None,
                confidence=float(r.get("confidence", 0.5)),
            )
        )
    q = data.get("image_quality") or {}
    debris = (data.get("floating_debris") or "").strip()
    return SceneReading(
        water_visible=bool(data.get("water_visible", False)),
        water_points=tuple(_px(p, width, height) for p in data.get("water_points", [])[:3]),
        references=tuple(refs),
        waterline_points=tuple(_px(p, width, height) for p in data.get("waterline_points", [])[:2]),
        floating_debris=debris or None,
        quality=ImageQuality(
            score=min(1.0, max(0.0, float(q.get("score", 0.5)))),
            blurred=bool(q.get("blurred", False)),
            too_dark=bool(q.get("too_dark", False)),
            issues=tuple(str(i) for i in q.get("issues", [])[:5]),
        ),
        cost_usd=cost,
    )


# --------------------------------------------------------------------------- Meta Model API


class MetaApiAdapter:
    """Muse Spark 1.3 (Chat Completions, JSON schema output) and SAM 3.1 (Responses, SSE).

    ``MODEL_API_KEY`` comes from the environment. ``transport`` lets tests plug
    in an ``httpx.MockTransport`` so no test touches the network.
    """

    name = "meta-model-api"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = sam.BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 90.0,
    ) -> None:
        self._key = api_key if api_key is not None else os.environ.get("MODEL_API_KEY", "")
        self._base = base_url.rstrip("/")
        self._transport = transport
        self._timeout = httpx.Timeout(timeout_s, connect=15.0)

    @property
    def scene_model_name(self) -> str:
        return SCENE_MODEL

    @property
    def mask_model_name(self) -> str:
        return sam.MODEL

    def _client(self) -> httpx.AsyncClient:
        if not self._key:
            raise ModelError("MODEL_API_KEY is not set")
        return httpx.AsyncClient(
            base_url=self._base,
            headers={"Authorization": f"Bearer {self._key}"},
            timeout=self._timeout,
            transport=self._transport,
        )

    @staticmethod
    def _raise_for(resp: httpx.Response, what: str) -> None:
        if resp.status_code >= 400:
            try:
                msg = resp.json().get("error", {}).get("message", "")
            except (json.JSONDecodeError, AttributeError):
                msg = ""
            raise ModelError(f"{what} failed with HTTP {resp.status_code}: {msg}".strip(": "))

    async def ready(self) -> tuple[bool, str]:
        if not self._key:
            return False, "MODEL_API_KEY is not set"
        try:
            async with self._client() as c:
                r = await c.get("/models")
        except httpx.HTTPError as exc:
            return False, f"Meta Model API unreachable: {type(exc).__name__}"
        if r.status_code != 200:
            return False, f"Meta Model API returned HTTP {r.status_code}"
        return True, "ok"

    async def read_scene(self, image: ImageInput) -> SceneReading:
        body = {
            "model": SCENE_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": SCENE_INSTRUCTIONS},
                        {"type": "image_url", "image_url": {"url": sam.image_data_url(image.jpeg)}},
                    ],
                }
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "flood_scene", "schema": scene_schema(), "strict": True},
            },
        }
        async with self._client() as c:
            try:
                r = await c.post("/chat/completions", json=body)
            except httpx.HTTPError as exc:
                raise ModelError(f"scene model request failed: {type(exc).__name__}") from exc
        self._raise_for(r, "scene model")
        payload = r.json()
        try:
            content = payload["choices"][0]["message"]["content"]
            data = json.loads(content)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise ModelError("scene model returned an unexpected shape") from exc
        usage = payload.get("usage") or {}
        cost = (
            float(usage.get("prompt_tokens", 0)) * MUSE_SPARK_INPUT_USD_PER_M
            + float(usage.get("completion_tokens", 0)) * MUSE_SPARK_OUTPUT_USD_PER_M
        ) / 1e6
        try:
            return parse_scene_json(data, image.width, image.height, cost)
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
            # Strict schemas should prevent this; a malformed answer is a model
            # fault (503), not an unhandled 500.
            raise ModelError("scene model returned an unexpected shape") from exc

    async def _stream_responses(self, body: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        async with (
            self._client() as c,
            c.stream("POST", "/responses", json=body, headers={"Accept": "text/event-stream"}) as r,
        ):
            if r.status_code >= 400:
                await r.aread()
                self._raise_for(r, "SAM request")
            async for ev in sam.sse_events(r.aiter_lines()):
                yield ev

    async def _parse(self, parse: Callable[[Any], Awaitable[R]], body: dict[str, Any]) -> R:
        """Run the official parser over the SSE stream; surface failures as ModelError."""
        try:
            return await parse(self._stream_responses(body))
        except ResponsesStreamError as exc:
            cause = exc.__cause__
            while cause is not None and not isinstance(cause, ModelError | httpx.HTTPError):
                cause = cause.__cause__
            if isinstance(cause, ModelError):
                raise cause from exc
            if isinstance(cause, httpx.HTTPError):
                raise ModelError(f"SAM request failed: {type(cause).__name__}") from exc
            raise ModelError(f"SAM stream error: {exc.code}") from exc
        except httpx.HTTPError as exc:
            raise ModelError(f"SAM request failed: {type(exc).__name__}") from exc

    async def segment(self, image: ImageInput, objects: Sequence[ObjectPrompt]) -> MaskResult:
        prompt = sam.build_prompt((image.width, image.height), objects)
        body = sam.request_body(
            prompt, image_url=sam.image_data_url(image.jpeg), mask_encoding="one_bit"
        )
        result = await self._parse(sam.parse_image_stream, body)
        # Decoding full-frame masks takes seconds of pure Python; keep it off the loop.
        masks = await asyncio.to_thread(sam.collect_masks, result, image.width, image.height)
        return MaskResult(
            {int(m.object_id): m for m in masks if m.frame == 0},
            sam.segmentation_cost_usd("image"),
            tuple(sam.result_errors(result)),
        )

    async def upload_video(self, video: VideoInput) -> str:
        async with self._client() as c:
            try:
                r = await c.post(
                    "/files",
                    data={"purpose": "user_data"},
                    files={"file": ("clip.mp4", video.data, video.media_type)},
                )
            except httpx.HTTPError as exc:
                raise ModelError(f"video upload failed: {type(exc).__name__}") from exc
        self._raise_for(r, "video upload")
        fid = r.json().get("id")
        if not isinstance(fid, str) or not fid.startswith("file-"):
            raise ModelError("video upload returned no file id")
        return fid

    async def track(self, video: VideoInput, concept: str) -> TrackResult:
        fid = await self.upload_video(video)
        body = sam.request_body(concept, video_file_id=fid, mask_encoding="one_bit")
        result = await self._parse(sam.parse_video_stream, body)
        masks = await asyncio.to_thread(sam.collect_masks, result, video.width, video.height)
        return TrackResult(
            sam.tracks_from_masks(masks),
            max(video.n_frames, sam.VIDEO_MIN_FRAMES),
            sam.segmentation_cost_usd("video", video.n_frames),
            tuple(sam.result_errors(result)),
        )


# --------------------------------------------------------------------------- open weights


@dataclass
class OpenWeightsAdapter:
    """Self-hosted path: SAM 3.1 open weights plus Muse Glimmer, served on Modal.

    TODO(open-weights): implement against the Modal deployment once it exists.
      * read_scene: send the same SCENE_INSTRUCTIONS and scene_schema() to Muse
        Glimmer behind an OpenAI-compatible server (vLLM or SGLang on Modal),
        with guided JSON decoding, then reuse parse_scene_json().
      * segment and track: run SAM 3.1 weights in a Modal function that emits
        the same special-token text as the hosted API, so sam.parse_text() and
        sam.collect_masks() can be reused unchanged.
      * ready: ping the Modal endpoints.
    Until then every call raises NotImplementedError and ready() says why.
    """

    endpoint: str | None = None
    name: str = "open-weights"
    _notes: list[str] = field(default_factory=list)

    @property
    def scene_model_name(self) -> str:
        return "muse-glimmer"

    @property
    def mask_model_name(self) -> str:
        return "sam-3.1-open-weights"

    async def ready(self) -> tuple[bool, str]:
        return False, "open-weights adapter is not implemented yet"

    async def read_scene(self, image: ImageInput) -> SceneReading:
        raise NotImplementedError("TODO(open-weights): Muse Glimmer on Modal")

    async def segment(self, image: ImageInput, objects: Sequence[ObjectPrompt]) -> MaskResult:
        raise NotImplementedError("TODO(open-weights): SAM 3.1 weights on Modal")

    async def track(self, video: VideoInput, concept: str) -> TrackResult:
        raise NotImplementedError("TODO(open-weights): SAM 3.1 weights on Modal")
