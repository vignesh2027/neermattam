"""FastAPI service for perception (docs/CONTRACTS.md section 1), port 8301.

Endpoints: ``POST /v1/measure``, ``POST /v1/measure/stream`` (the same, as
server-sent events: each agent step as it happens, then the result),
``POST /v1/transcribe``, ``GET /healthz``, ``GET /readyz``.

Models are chosen by environment (see :func:`default_app`): the open-weights
path (Muse Spark Open on Baseten, SAM 2.1 locally, with the verify-and-refine
agent) when ``BASETEN_API_KEY`` is set, otherwise the hosted Meta Model API.
A local mask model is loaded once, at start-up, before the first request.

Uploads are size-limited while being read, images are dimension-limited before
decoding, and only metadata-free re-encodes are kept or sent to models, so GPS
tags never leave the process. ``DateTimeOriginal`` is read before the metadata
is dropped and returned in the ``X-Captured-At`` response header for freshness
checks (RFC 3339, in UTC when the camera recorded a zone offset). The response
body itself matches the contract exactly.

Set ``NEERMATTAM_STORE_DIR`` to keep each sanitised upload and its response.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from .adapters import MetaApiAdapter, ModelError
from .agent import TraceStep, VerifyRefineAgent
from .media import (
    MAX_IMAGE_BYTES,
    MAX_VIDEO_BYTES,
    ImageInput,
    MediaRejected,
    VideoInput,
    image_from_rgb,
    load_image,
    load_video,
)
from .models import MeasureResponse, TranscribeResponse
from .muse_open import MODEL_LABEL, MuseSparkOpenAdapter
from .person import blur_heads
from .pipeline import Pipeline
from .references import RefKind
from .sam_local import mask_model_from_config

PORT = 8301
_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
_VIDEO_TYPES = {"video/mp4", "video/quicktime"}
# Formats Muse Spark Open accepts (https://docs.baseten.co/inference/model-apis/audio).
_AUDIO_TYPES = {
    "audio/wav": "audio/wav",
    "audio/x-wav": "audio/wav",
    "audio/wave": "audio/wav",
    "audio/mpeg": "audio/mpeg",
    "audio/mp3": "audio/mpeg",
    "audio/flac": "audio/flac",
    "audio/x-flac": "audio/flac",
    "audio/ogg": "audio/ogg",
    "audio/opus": "audio/ogg",
    "audio/mp4": "audio/mp4",
    "audio/m4a": "audio/mp4",
    "audio/x-m4a": "audio/mp4",
    "audio/aac": "audio/aac",
}
MAX_AUDIO_BYTES = 25 * 1024 * 1024
KEEPALIVE_S = 15.0


async def _read_limited(upload: UploadFile, limit: int, what: str) -> bytes:
    chunks, total = [], 0
    while True:
        chunk = await upload.read(1 << 20)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise HTTPException(413, f"{what} is larger than {limit // (1024 * 1024)} MB")
        chunks.append(chunk)
    return b"".join(chunks)


def _store(store: Path, resp: MeasureResponse, image: ImageInput) -> None:
    store.mkdir(parents=True, exist_ok=True)
    # Metadata-free copy, with the head of every person found blurred: no face is stored.
    jpeg = image_from_rgb(blur_heads(image.rgb, resp._faces)).jpeg if resp._faces else image.jpeg
    (store / f"{resp.measurement_id}.jpg").write_bytes(jpeg)
    body = resp.model_dump(exclude={"overlay_png_b64"})
    (store / f"{resp.measurement_id}.json").write_text(json.dumps(body), encoding="utf-8")


# Largest request body accepted: one image, one video and the form fields.
MAX_BODY_BYTES = MAX_IMAGE_BYTES + MAX_VIDEO_BYTES + (1 << 20)


class _BodyTooLarge(Exception):
    pass


class BodyLimit:
    """ASGI middleware that refuses request bodies over ``limit`` bytes with 413.

    Starlette parses (and spools to disk) the whole multipart body before the
    endpoint runs, so the per-file limits in the endpoint alone cannot stop a
    huge upload. This checks ``Content-Length`` up front and counts the bytes of
    chunked bodies as they arrive.
    """

    def __init__(self, app: Any, limit: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        length = dict(scope.get("headers") or []).get(b"content-length", b"")
        if length.isdigit() and int(length) > self.limit:
            await self._reject(send)
            return
        seen = 0
        started = False

        async def limited_receive() -> Any:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.limit:
                    raise _BodyTooLarge
            return message

        async def tracked_send(message: Any) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _BodyTooLarge:
            if not started:
                await self._reject(send)

    async def _reject(self, send: Any) -> None:
        body = json.dumps({"detail": "request body is too large"}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def create_app(
    pipeline: Pipeline,
    store_dir: str | None = None,
    transcriber: MuseSparkOpenAdapter | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Load local weights once, before serving (the first load downloads them).
        load = getattr(pipeline.masks, "load", None)
        if callable(load):
            await asyncio.to_thread(load)
        yield

    app = FastAPI(title="Neermattam perception", version="0.2.0", lifespan=lifespan)
    app.add_middleware(BodyLimit)
    store = Path(store_dir) if store_dir else None

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        ok, why = await pipeline.ready()
        return JSONResponse(
            {"status": "ok" if ok else "unavailable", "reason": why}, status_code=200 if ok else 503
        )

    async def inputs(
        image: UploadFile | None,
        video: UploadFile | None,
        hint_reference: str | None,
        hint_diameter_cm: float | None,
    ) -> tuple[ImageInput | None, VideoInput | None]:
        """Validated, sanitised media; raises the 413, 415 and 422 errors."""
        if image is None and video is None:
            raise HTTPException(422, "send an image, or a video")
        if hint_reference is not None and hint_reference not in {k.value for k in RefKind}:
            raise HTTPException(422, f"unknown hint_reference {hint_reference!r}")
        if hint_diameter_cm is not None and not 10.0 <= hint_diameter_cm <= 200.0:
            raise HTTPException(422, "hint_diameter_cm must be between 10 and 200")
        img: ImageInput | None = None
        vid: VideoInput | None = None
        try:
            if image is not None:
                if image.content_type and image.content_type not in _IMAGE_TYPES:
                    raise HTTPException(415, "image must be JPEG or PNG")
                raw = await _read_limited(image, MAX_IMAGE_BYTES, "image")
                img = await asyncio.to_thread(load_image, raw)
            if video is not None:
                ctype = video.content_type or "video/mp4"
                if ctype not in _VIDEO_TYPES:
                    raise HTTPException(415, "video must be MP4 or MOV")
                raw = await _read_limited(video, MAX_VIDEO_BYTES, "video")
                vid = await asyncio.to_thread(load_video, raw, ctype)
        except MediaRejected as exc:
            raise HTTPException(422, str(exc)) from exc
        return img, vid

    async def run(
        img: ImageInput | None,
        vid: VideoInput | None,
        hint_reference: str | None,
        hint_diameter_cm: float | None,
        on_step: Callable[[TraceStep], None] | None = None,
    ) -> MeasureResponse:
        try:
            resp = await pipeline.measure(
                image=img,
                video=vid,
                hint_reference=hint_reference,
                hint_diameter_cm=hint_diameter_cm,
                on_step=on_step,
            )
        except ModelError as exc:
            raise HTTPException(503, f"model unavailable: {exc}") from exc
        except NotImplementedError as exc:
            raise HTTPException(503, f"model adapter not implemented: {exc}") from exc
        key = img if img is not None else (vid.key_frame if vid is not None else None)
        if store is not None and key is not None:
            _store(store, resp, key)
        return resp

    @app.post("/v1/measure", response_model=MeasureResponse)
    async def measure(
        image: Annotated[UploadFile | None, File()] = None,
        video: Annotated[UploadFile | None, File()] = None,
        hint_reference: Annotated[str | None, Form()] = None,
        hint_diameter_cm: Annotated[float | None, Form()] = None,
    ) -> JSONResponse:
        img, vid = await inputs(image, video, hint_reference, hint_diameter_cm)
        resp = await run(img, vid, hint_reference, hint_diameter_cm)
        headers = {}
        if resp.captured_at:
            headers["X-Captured-At"] = resp.captured_at
        return JSONResponse(resp.model_dump(), headers=headers)

    @app.post("/v1/measure/stream")
    async def measure_stream(
        image: Annotated[UploadFile | None, File()] = None,
        video: Annotated[UploadFile | None, File()] = None,
        hint_reference: Annotated[str | None, Form()] = None,
        hint_diameter_cm: Annotated[float | None, Form()] = None,
    ) -> StreamingResponse:
        img, vid = await inputs(image, video, hint_reference, hint_diameter_cm)
        queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()

        def on_step(step: TraceStep) -> None:
            queue.put_nowait(("step", json.dumps(asdict(step))))

        async def work() -> None:
            try:
                resp = await run(img, vid, hint_reference, hint_diameter_cm, on_step)
                queue.put_nowait(("result", json.dumps(resp.model_dump())))
            except HTTPException as exc:
                body = {"status": exc.status_code, "detail": exc.detail}
                queue.put_nowait(("error", json.dumps(body)))
            except Exception:  # the stream must end with an event, never hang
                body = {"status": 500, "detail": "internal error"}
                queue.put_nowait(("error", json.dumps(body)))
                logging.getLogger(__name__).exception("measure stream failed")

        async def events() -> AsyncIterator[str]:
            task = asyncio.create_task(work())
            try:
                while True:
                    try:
                        kind, data = await asyncio.wait_for(queue.get(), KEEPALIVE_S)
                    except TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    yield f"event: {kind}\ndata: {data}\n\n"
                    if kind in ("result", "error"):
                        break
            finally:
                if not task.done():  # the client went away: stop the work
                    task.cancel()

        headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        if img is not None and img.captured_at:
            headers["X-Captured-At"] = img.captured_at
        return StreamingResponse(events(), media_type="text/event-stream", headers=headers)

    @app.post("/v1/transcribe", response_model=TranscribeResponse)
    async def transcribe(audio: Annotated[UploadFile, File()]) -> TranscribeResponse:
        ctype = (audio.content_type or "").split(";")[0].strip().lower()
        if ctype not in _AUDIO_TYPES:
            raise HTTPException(415, "audio must be WAV, MP3, FLAC, OGG or M4A")
        raw = await _read_limited(audio, MAX_AUDIO_BYTES, "audio")
        if not raw:
            raise HTTPException(422, "audio is empty")
        if transcriber is None:
            raise HTTPException(503, "no audio model is configured (set BASETEN_API_KEY)")
        try:
            t = await transcriber.transcribe(raw, _AUDIO_TYPES[ctype])
        except ModelError as exc:
            raise HTTPException(503, f"audio model unavailable: {exc}") from exc
        return TranscribeResponse(
            text=t.text,
            language=t.language,
            place_text=t.place_text,
            depth_words=t.depth_words,
            depth_cm=t.depth_cm,
            depth_sigma_cm=t.depth_sigma_cm,
            languages=list(t.languages),
            place_spoken=t.place_spoken,
            depth_words_en=t.depth_words_en,
            depth_low_cm=t.depth_low_cm,
            depth_high_cm=t.depth_high_cm,
            depth_basis=t.depth_basis,
            model=MODEL_LABEL,
            cost_usd=round(t.usage.cost_usd, 6),
        )

    return app


def key_from_dotenv(start: Path | None = None) -> str | None:
    """``MODEL_API_KEY`` from the nearest ``.env`` at or above ``start``.

    Secrets live only in the gitignored ``.env`` at the repository root. Only
    this one variable is read, and it is never logged.
    """
    here = (start or Path.cwd()).resolve()
    for folder in (here, *here.parents):
        env = folder / ".env"
        if not env.is_file():
            continue
        for raw in env.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("export "):
                line = line[7:].strip()
            key, sep, value = line.partition("=")
            if sep and key.strip() == "MODEL_API_KEY":
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                    value = value[1:-1]
                return value or None
        return None
    return None


def default_app() -> FastAPI:
    """The app with models chosen by environment.

    ``NEERMATTAM_SCENE``: ``muse-spark-open`` (Baseten) or ``muse-spark-1.3``
    (Meta Model API); by default the open model when ``BASETEN_API_KEY`` is set.
    ``NEERMATTAM_MASKS``: ``sam2.1-local`` (the default: hiera-small on this
    machine; optionally ``:large`` and ``:fp16``), ``sam2.1-modal`` (SAM 2.1
    large on Modal with local small as the fallback), ``sam3.1-api`` or
    ``sam3.1-local``. With the open model the
    verify-and-refine agent checks every mask.
    """
    store = os.environ.get("NEERMATTAM_STORE_DIR")
    muse = MuseSparkOpenAdapter.from_env()
    scene_choice = os.environ.get("NEERMATTAM_SCENE") or (
        "muse-spark-open" if muse.api_key else "muse-spark-1.3"
    )
    key = os.environ.get("MODEL_API_KEY") or key_from_dotenv()
    meta = MetaApiAdapter(key or "")
    if scene_choice == "muse-spark-1.3":
        masks_name = os.environ.get("NEERMATTAM_MASKS", "sam3.1-api")
        masks = meta if masks_name == "sam3.1-api" else mask_model_from_config(masks_name)
        return create_app(Pipeline(meta, masks), store, muse if muse.api_key else None)
    # Local hiera-small is the default: on the real photos it reached the same
    # outcomes as large and as large on Modal, and it is the fastest
    # (eval/RESULTS_SAM_AB.md, eval/RESULTS_REAL.md). Modal stays selectable.
    masks = mask_model_from_config(os.environ.get("NEERMATTAM_MASKS", "sam2.1-local"), api=meta)
    agent = VerifyRefineAgent(muse, masks)
    return create_app(Pipeline(muse, masks, agent=agent), store, muse)


def main() -> None:
    uvicorn.run(
        default_app(),
        host=os.environ.get("PERCEPTION_HOST", "127.0.0.1"),
        port=int(os.environ.get("PERCEPTION_PORT", PORT)),
    )


if __name__ == "__main__":
    main()
