"""FastAPI service for perception (docs/CONTRACTS.md section 1), port 8301.

Endpoints: ``POST /v1/measure``, ``GET /healthz``, ``GET /readyz``.

Uploads are size-limited while being read, images are dimension-limited before
decoding, and only metadata-free re-encodes are kept or sent to models, so GPS
tags never leave the process. ``DateTimeOriginal`` is read before the metadata
is dropped and returned in the ``X-Captured-At`` response header for freshness
checks (RFC 3339, in UTC when the camera recorded a zone offset). The response
body itself matches the contract exactly.

Set ``NEERMATTAM_STORE_DIR`` to keep each sanitised upload and its response.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from .adapters import MetaApiAdapter, ModelError
from .media import (
    MAX_IMAGE_BYTES,
    MAX_VIDEO_BYTES,
    ImageInput,
    MediaRejected,
    VideoInput,
    load_image,
    load_video,
)
from .models import MeasureResponse
from .pipeline import Pipeline
from .references import RefKind

PORT = 8301
_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
_VIDEO_TYPES = {"video/mp4", "video/quicktime"}


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
    (store / f"{resp.measurement_id}.jpg").write_bytes(image.jpeg)  # metadata-free copy
    body = resp.model_dump(exclude={"overlay_png_b64"})
    (store / f"{resp.measurement_id}.json").write_text(json.dumps(body), encoding="utf-8")


def create_app(pipeline: Pipeline, store_dir: str | None = None) -> FastAPI:
    app = FastAPI(title="Neermattam perception", version="0.1.0")
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

    @app.post("/v1/measure", response_model=MeasureResponse)
    async def measure(
        image: Annotated[UploadFile | None, File()] = None,
        video: Annotated[UploadFile | None, File()] = None,
        hint_reference: Annotated[str | None, Form()] = None,
        hint_diameter_cm: Annotated[float | None, Form()] = None,
    ) -> JSONResponse:
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
                img = load_image(await _read_limited(image, MAX_IMAGE_BYTES, "image"))
            if video is not None:
                ctype = video.content_type or "video/mp4"
                if ctype not in _VIDEO_TYPES:
                    raise HTTPException(415, "video must be MP4 or MOV")
                vid = load_video(await _read_limited(video, MAX_VIDEO_BYTES, "video"), ctype)
        except MediaRejected as exc:
            raise HTTPException(422, str(exc)) from exc
        try:
            resp = await pipeline.measure(
                image=img,
                video=vid,
                hint_reference=hint_reference,
                hint_diameter_cm=hint_diameter_cm,
            )
        except ModelError as exc:
            raise HTTPException(503, f"model unavailable: {exc}") from exc
        except NotImplementedError as exc:
            raise HTTPException(503, f"model adapter not implemented: {exc}") from exc
        key = img if img is not None else (vid.key_frame if vid is not None else None)
        if store is not None and key is not None:
            _store(store, resp, key)
        headers = {}
        if key is not None and key.captured_at:
            headers["X-Captured-At"] = key.captured_at
        return JSONResponse(resp.model_dump(), headers=headers)

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
    key = os.environ.get("MODEL_API_KEY") or key_from_dotenv()
    adapter = MetaApiAdapter(key or "")
    return create_app(Pipeline(adapter, adapter), os.environ.get("NEERMATTAM_STORE_DIR"))


def main() -> None:
    uvicorn.run(
        default_app(),
        host=os.environ.get("PERCEPTION_HOST", "127.0.0.1"),
        port=int(os.environ.get("PERCEPTION_PORT", PORT)),
    )


if __name__ == "__main__":
    main()
