"""Neermattam SAM service on Modal: SAM 2.1 hiera-large on one GPU, behind a bearer token.

Two functions:

* ``web``: a small CPU container running FastAPI. It checks the bearer token
  and passes the request to the GPU class. Requests without the token are
  refused here, so they never start a GPU.
* ``Segmenter``: the GPU class. It loads SAM 2.1 hiera-large at start (the
  weights are baked into the image), loads base-plus, small or the gated
  SAM 3 checkpoints on first use, and computes one image embedding per request
  for all prompts.

Deploy with ``modal deploy deploy/modal-sam/app.py``. See docs/MODAL.md.
"""

# No "from __future__ import annotations": FastAPI must see the real Request
# type on the handlers defined inside web().
import os
import time
from typing import Any

import modal

APP_NAME = "neermattam-sam"
# Read when the app is deployed, so a redeploy can switch GPU or idle time.
# "cpu" runs the same code without a GPU (slow; for checking the plumbing only).
GPU = os.environ.get("NEERMATTAM_SAM_GPU", "L4")
GPU_ARG = None if GPU.lower() in ("", "none", "cpu") else GPU
SCALEDOWN_S = int(os.environ.get("NEERMATTAM_SAM_SCALEDOWN_S", "120"))
MAX_GPU_CONTAINERS = int(os.environ.get("NEERMATTAM_SAM_MAX_CONTAINERS", "2"))
# fp32 by default (with TF32 matmuls on the GPU). bf16 is opt-in per request
# until it has been measured on the GPU; on the CPU it matched fp32 closely.
DEFAULT_PRECISION = os.environ.get("NEERMATTAM_SAM_PRECISION", "fp32")
MODELS_DIR = "/models"
MAX_IMAGE_BYTES = 20 * 1024 * 1024
BAKED = (
    "facebook/sam2.1-hiera-large",
    "facebook/sam2.1-hiera-base-plus",
    "facebook/sam2.1-hiera-small",
)


def bake_weights() -> None:
    """Runs once at image build time: the three SAM 2.1 checkpoints go into the image."""
    from huggingface_hub import snapshot_download

    for repo in BAKED:
        snapshot_download(
            repo,
            local_dir=f"{MODELS_DIR}/{repo.split('/')[-1]}",
            allow_patterns=["*.json", "*.safetensors"],
        )


gpu_image = (
    modal.Image.debian_slim(python_version="3.12")
    .uv_pip_install(
        "torch==2.14.1",
        "torchvision==0.29.1",  # the SAM 2 image processor needs it
        "transformers==5.21.0",
        "huggingface_hub",
        "safetensors",
        "numpy",
        "pillow",
    )
    .env({"HF_HUB_DISABLE_TELEMETRY": "1"})
    .run_function(bake_weights)
    # Set after the bake, so the build leaves /hf-cache empty for the volume mount.
    .env({"HF_HOME": "/hf-cache"})
    .add_local_python_source("sam_core")
)
web_image = (
    modal.Image.debian_slim(python_version="3.12")
    .uv_pip_install("fastapi[standard]")
)

app = modal.App(APP_NAME)
# Cache for gated checkpoints downloaded at run time (SAM 3.x), so they download once.
hf_cache = modal.Volume.from_name("neermattam-hf-cache", create_if_missing=True)


@app.cls(
    image=gpu_image,
    gpu=GPU_ARG,
    cpu=4.0 if GPU_ARG is None else None,
    memory=8192 if GPU_ARG is None else None,
    min_containers=0,
    max_containers=MAX_GPU_CONTAINERS,
    scaledown_window=SCALEDOWN_S,
    timeout=180,
    secrets=[modal.Secret.from_name("neermattam-hf")],
    volumes={"/hf-cache": hf_cache},
)
class Segmenter:
    @modal.enter()
    def start(self) -> None:
        import torch

        import sam_core

        torch.backends.cuda.matmul.allow_tf32 = True
        torch.backends.cudnn.allow_tf32 = True

        self.runners: dict[str, sam_core.SamRunner] = {}
        t0 = time.perf_counter()
        self._runner(sam_core.DEFAULT_MODEL)
        self.started_ms = round(1000 * (time.perf_counter() - t0))

    def _runner(self, key: str) -> Any:
        from pathlib import Path

        import sam_core

        r = self.runners.get(key)
        if r is None:
            if key not in sam_core.MODELS:
                raise sam_core.ModelUnavailable(
                    f"unknown model {key!r}; choose from {sorted(sam_core.MODELS)}"
                )
            baked = sam_core.baked_dir(Path(MODELS_DIR), key)
            r = sam_core.SamRunner(
                key=key,
                source=str(baked) if baked else None,
                token=sam_core.hf_token(),
            )
            r.load()
            if key in sam_core.GATED:
                hf_cache.commit()
            self.runners[key] = r
        return r

    @modal.method()
    def health(self) -> dict[str, Any]:
        import torch

        import sam_core

        return {
            "status": "ok",
            "default_model": sam_core.MODELS[sam_core.DEFAULT_MODEL][2],
            "loaded_models": [r.model_name for r in self.runners.values()],
            "available_models": sorted(sam_core.MODELS),
            "gated_access_token": sam_core.hf_token() is not None,
            "device": "cuda" if torch.cuda.is_available() else "cpu",
            "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
            "default_precision": DEFAULT_PRECISION,
            "container_start_ms": self.started_ms,
        }

    @modal.method()
    def segment(
        self,
        image: bytes,
        prompts: Any,
        model: str = "large",
        max_masks: int = 3,
        precision: str | None = None,
    ) -> dict[str, Any]:
        import io

        import numpy as np
        import torch
        from PIL import Image, ImageOps, UnidentifiedImageError

        import sam_core

        t0 = time.perf_counter()
        precision = precision or DEFAULT_PRECISION
        if precision not in ("fp32", "bf16"):
            return {"error": {"status": 422, "detail": "precision must be fp32 or bf16"}}
        if not 1 <= int(max_masks) <= 3:
            return {"error": {"status": 422, "detail": "max_masks must be 1, 2 or 3"}}
        try:
            with Image.open(io.BytesIO(image)) as im:
                rgb = np.asarray(ImageOps.exif_transpose(im).convert("RGB"))
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            return {"error": {"status": 415, "detail": f"cannot decode image: {exc}"}}
        h, w = rgb.shape[:2]
        if max(w, h) > sam_core.MAX_SIDE:
            return {"error": {"status": 413, "detail": f"image side above {sam_core.MAX_SIDE} px"}}
        try:
            parsed = sam_core.parse_prompts(prompts, w, h)
            runner = self._runner(model)
        except sam_core.PromptError as exc:
            return {"error": {"status": 422, "detail": str(exc)}}
        except sam_core.ModelUnavailable as exc:
            return {"error": {"status": 503, "detail": str(exc)}}
        t1 = time.perf_counter()
        out = runner.segment(rgb, parsed, max_masks=int(max_masks), precision=precision)
        t2 = time.perf_counter()
        for res in out["results"]:
            for c in res["candidates"]:
                c["rle"] = sam_core.rle_encode(c.pop("mask"))
        t3 = time.perf_counter()
        timings = out["timings_ms"]
        timings["decode_image"] = round(1000 * (t1 - t0), 1)
        timings["encode_rle"] = round(1000 * (t3 - t2), 1)
        timings["gpu_call"] = round(1000 * (t3 - t0), 1)
        return {
            "model": runner.model_name,
            "device": runner.device,
            "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
            "precision": precision,
            "image_size": out["image_size"],
            "results": out["results"],
            "timings_ms": timings,
        }


@app.function(
    image=web_image,
    cpu=0.25,
    memory=512,
    min_containers=0,
    scaledown_window=300,
    timeout=200,
    secrets=[modal.Secret.from_name("neermattam-sam")],
)
@modal.concurrent(max_inputs=8)
@modal.asgi_app(label="neermattam-sam")
def web() -> Any:
    import base64
    import binascii
    import hmac
    import json

    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import JSONResponse

    token = os.environ["NEERMATTAM_SAM_TOKEN"]
    api = FastAPI(title="Neermattam SAM", docs_url=None, redoc_url=None, openapi_url=None)

    def authorise(request: Request) -> None:
        got = request.headers.get("authorization", "")
        scheme, _, value = got.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(value.strip(), token):
            raise HTTPException(401, "missing or wrong bearer token")

    def answer(result: dict[str, Any], t0: float) -> JSONResponse:
        if "error" in result:
            return JSONResponse({"detail": result["error"]["detail"]}, result["error"]["status"])
        result["timings_ms"]["server_total"] = round(1000 * (time.perf_counter() - t0), 1)
        return JSONResponse(result)

    @api.get("/health")
    async def health(request: Request) -> JSONResponse:
        authorise(request)
        return JSONResponse(await Segmenter().health.remote.aio())

    @api.post("/segment")
    async def segment(request: Request) -> JSONResponse:
        authorise(request)
        t0 = time.perf_counter()
        ctype = request.headers.get("content-type", "")
        try:
            if ctype.startswith("multipart/form-data"):
                form = await request.form()
                upload = form.get("image")
                if upload is None or isinstance(upload, str):
                    raise HTTPException(422, "multipart field 'image' must be a file")
                image = await upload.read()
                prompts = json.loads(str(form.get("prompts", "null")))
                opts = {k: form.get(k) for k in ("model", "max_masks", "precision")}
            else:
                body = await request.json()
                if not isinstance(body, dict):
                    raise HTTPException(422, "body must be a JSON object")
                image = base64.b64decode(str(body.get("image_b64", "")), validate=True)
                prompts = body.get("prompts")
                opts = {k: body.get(k) for k in ("model", "max_masks", "precision")}
        except (json.JSONDecodeError, binascii.Error, ValueError) as exc:
            raise HTTPException(422, f"malformed request: {exc}") from exc
        if not image:
            raise HTTPException(422, "image is missing")
        if len(image) > MAX_IMAGE_BYTES:
            raise HTTPException(413, "image larger than 20 MB")
        try:
            max_masks = int(opts["max_masks"] or 3)
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, "max_masks must be an integer") from exc
        result = await Segmenter().segment.remote.aio(
            image,
            prompts,
            model=str(opts["model"] or "large"),
            max_masks=max_masks,
            precision=str(opts["precision"]) if opts["precision"] else None,
        )
        return answer(result, t0)

    return api
