"""Per-photo latency of the open-weights pipeline, with the service conditions next to it.

From perception/ (live):

    uv run --extra local python ../eval/latency.py --masks local-small
    uv run --extra local python ../eval/latency.py --masks modal

For every photo in eval/commons_photos.json it first times two probe calls to
Muse Spark Open (a one-line text answer, and the same answer about a 1024 px
JPEG of the photo), so that a slow network or a slow Baseten queue shows up
beside the pipeline time instead of being mistaken for the pipeline. Then it
runs the full pipeline with a fresh scene reading (no cache) and records the
wall time and the time spent in each kind of step (scene, mask, check,
geometry), summed from the agent trace. Parallel steps overlap, so the sums can
exceed the wall time. Results go to eval/out/latency-<masks>.json and stdout,
and each response and overlay to eval/out/latency-<masks>/.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import sys
import time
from pathlib import Path
from typing import Any

from PIL import Image

from neermattam_perception.agent import VerifyRefineAgent
from neermattam_perception.media import load_image
from neermattam_perception.modal_sam import ModalSamAdapter
from neermattam_perception.muse_open import MuseSparkOpenAdapter
from neermattam_perception.pipeline import Pipeline
from neermattam_perception.sam_local import Sam21LocalAdapter

HERE = Path(__file__).resolve().parent


async def probe(muse: MuseSparkOpenAdapter, path: Path) -> dict[str, float]:
    t0 = time.perf_counter()
    await muse.chat(
        [{"role": "user", "content": [{"type": "text", "text": 'Reply {"ok": 1}'}]}],
        max_tokens=200,
        what="probe",
        effort="low",
    )
    text_s = time.perf_counter() - t0
    im = Image.open(path).convert("RGB")
    im.thumbnail((1024, 1024))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=75)
    t0 = time.perf_counter()
    await muse.chat(
        [
            {
                "role": "user",
                "content": [
                    muse._image_part(buf.getvalue()),
                    {"type": "text", "text": 'Reply {"ok": 1}'},
                ],
            }
        ],
        max_tokens=200,
        what="probe",
        effort="low",
    )
    return {
        "text_call_s": round(text_s, 1),
        "image_call_s": round(time.perf_counter() - t0, 1),
        "image_kb": round(len(buf.getvalue()) / 1024),
    }


def step_sums(trace: list[dict[str, Any]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for t in trace:
        out[t["step"]] = out.get(t["step"], 0.0) + t["latency_ms"] / 1000.0
    return {k: round(v, 1) for k, v in out.items()}


async def run(args: argparse.Namespace) -> int:
    meta = json.loads((HERE / "commons_photos.json").read_text(encoding="utf-8"))["photos"]
    if args.only:
        meta = [m for m in meta if m["id"] in args.only]
    muse = MuseSparkOpenAdapter.from_env()
    sam: Any
    if args.masks == "modal":
        sam = ModalSamAdapter.from_env()
        await sam.ready()
    else:
        sam = Sam21LocalAdapter(size=args.masks.split("-", 1)[1])
        sam.load()
    rows = {}
    for m in meta:
        path = HERE / "photos" / f"{m['id']}.jpg"
        image = load_image(path.read_bytes())
        cond = await probe(muse, path)
        pipe = Pipeline(muse, sam, agent=VerifyRefineAgent(muse, sam))
        muse._scene_cache.clear()
        t0 = time.perf_counter()
        resp = await pipe.measure(image=image)
        wall = time.perf_counter() - t0
        body = resp.model_dump()
        keep = HERE / "out" / f"latency-{args.masks}"
        keep.mkdir(parents=True, exist_ok=True)
        png = body.pop("overlay_png_b64")
        if png:
            (keep / f"{m['id']}_overlay.png").write_bytes(base64.b64decode(png))
        (keep / f"{m['id']}.json").write_text(json.dumps(body, indent=1), encoding="utf-8")
        rows[m["id"]] = {
            "wall_s": round(wall, 1),
            "steps_s": step_sums(body["agent_trace"]),
            "wheels_checked": len(
                {t["target"] for t in body["agent_trace"] if t["step"] == "verify"} - {"water"}
            ),
            "method": body["method"],
            "depth_cm": body["depth_cm"],
            "depth_sigma_cm": body["depth_sigma_cm"],
            "service": cond,
        }
        sys.stdout.write(f"{m['id']}: {json.dumps(rows[m['id']])}\n")
        sys.stdout.flush()
    out = HERE / "out" / f"latency-{args.masks}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(
        json.dumps(
            {"masks": sam.mask_model_name, "date": time.strftime("%Y-%m-%d %H:%M"), "photos": rows},
            indent=1,
        ),
        encoding="utf-8",
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--masks",
        default="local-small",
        choices=["local-small", "local-base-plus", "local-large", "modal"],
    )
    ap.add_argument("--only", nargs="*")
    return asyncio.run(run(ap.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
