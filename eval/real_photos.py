"""Run the open-weights pipeline on the four Wikimedia Commons flood photos.

Usage (from perception/, with the local extra installed):

    uv run --extra local python ../eval/real_photos.py            # fetch if needed, run, write
    uv run --extra local python ../eval/real_photos.py --only c3  # one photo

Photos are downloaded from Commons into eval/photos/ (gitignored); the URL,
author and licence of each are in eval/commons_photos.json. Outputs go to
eval/out/ (gitignored): the overlay PNG, the response JSON with its agent trace,
and every image the verifier was shown. The generated section of
eval/RESULTS_REAL.md is rewritten between its markers.

There is no ground truth for these photos: nobody measured the water. The
results show qualitative behaviour (what was segmented, what the agent accepted
or rejected, which method answered) and the real latency and cost, not accuracy.

Live: this calls Muse Spark Open on Baseten (BASETEN_* in the repository .env)
and runs SAM 2.1 locally. Each photo costs a few Muse calls of about 3k to 6k
tokens.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import re
import sys
import time
from pathlib import Path

import httpx

from neermattam_perception.agent import VerifyRefineAgent
from neermattam_perception.media import load_image
from neermattam_perception.muse_open import MuseSparkOpenAdapter
from neermattam_perception.pipeline import Pipeline
from neermattam_perception.sam_local import Sam21LocalAdapter

HERE = Path(__file__).resolve().parent
PHOTOS = HERE / "photos"
OUT = HERE / "out"
RESULTS = HERE / "RESULTS_REAL.md"
UA = "neermattam-eval/0.2 (https://github.com/vignesh2027; flood depth research)"


def fetch(meta: dict[str, object]) -> Path:
    PHOTOS.mkdir(exist_ok=True)
    path = PHOTOS / f"{meta['id']}.jpg"
    if path.exists() and path.stat().st_size > 0:
        return path
    # IPv4 only: some networks advertise IPv6 routes that hang.
    transport = httpx.HTTPTransport(local_address="0.0.0.0", retries=2)
    with httpx.Client(transport=transport, timeout=120.0, headers={"User-Agent": UA}) as c:
        r = c.get(str(meta["download"]), follow_redirects=True)
        r.raise_for_status()
        path.write_bytes(r.content)
    return path


def summarise(pid: str, meta: dict[str, object], body: dict[str, object], wall_s: float) -> str:
    kind = "named in the brief" if meta.get("set") == "brief" else "extra, side-on wheels"
    lines = [f"### {pid}: {meta['title']} ({kind})", ""]
    lines.append(
        f"Author {meta['author']}, licence {meta['licence']} ({meta['licence_url']}), "
        f"source {meta['page']}."
    )
    lines.append("")
    if body.get("usable"):
        lines.append(
            f"Result: **{body['method']}**, depth {body['depth_cm']} cm, sigma "
            f"{body['depth_sigma_cm']} cm, confidence {body['confidence']}."
        )
    else:
        lines.append("Result: **not usable**.")
    est = body.get("model_estimate")
    if isinstance(est, dict):
        lines.append(
            f"Scene model's own estimate: {est['depth_cm']} cm (sigma {est['depth_sigma_cm']} cm), "
            f"basis: {est['basis']}."
        )
    lines.append(f"Captured at (EXIF): {body.get('captured_at') or 'not recorded'}.")
    lines.append("")
    lines.append("Reasons: " + "; ".join(str(r) for r in body.get("reasons", [])) + ".")
    lines.append("")
    timings = body.get("timings_ms", {})
    lines.append(
        f"Wall time {wall_s:.1f} s (timings ms: "
        + ", ".join(f"{k} {v}" for k, v in dict(timings).items())  # type: ignore[call-overload]
        + f"); model cost USD {body.get('cost_usd')}."
    )
    lines.append("")
    shown = f" Verifier images: `eval/out/{pid}_agent/`." if (OUT / f"{pid}_agent").exists() else ""
    lines.append(f"Overlay: `eval/out/{pid}_overlay.png`.{shown}")
    lines.append("")
    lines.append("| Step | Target | Model | Input | Output | Decision | ms |")
    lines.append("|---|---|---|---|---|---|---|")
    for t in body.get("agent_trace", []):  # type: ignore[union-attr]
        keys = ("step", "target", "model", "input", "output", "decision", "latency_ms")
        cells = [str(t[k]).replace("|", "/") for k in keys]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    return "\n".join(lines)


def write_results(sections: list[str], header: str) -> None:
    begin, end = "<!-- real-eval:begin -->", "<!-- real-eval:end -->"
    body = header + "\n\n" + "\n".join(sections)
    if RESULTS.exists():
        text = RESULTS.read_text(encoding="utf-8")
        if begin in text and end in text:
            pat = re.compile(re.escape(begin) + ".*?" + re.escape(end), re.DOTALL)
            RESULTS.write_text(pat.sub(f"{begin}\n{body}\n{end}", text), encoding="utf-8")
            return
    RESULTS.write_text(f"# Real-photo evaluation\n\n{begin}\n{body}\n{end}\n", encoding="utf-8")


async def run(only: list[str] | None) -> int:
    meta = json.loads((HERE / "commons_photos.json").read_text(encoding="utf-8"))["photos"]
    if only:
        meta = [m for m in meta if m["id"] in only]
    muse = MuseSparkOpenAdapter.from_env()
    if not muse.api_key:
        sys.stderr.write("BASETEN_API_KEY is not set (see the repository .env)\n")
        return 2
    sam = Sam21LocalAdapter()
    t0 = time.perf_counter()
    sam.load()
    load_s = time.perf_counter() - t0
    OUT.mkdir(exist_ok=True)
    sections = []
    for m in meta:
        path = fetch(m)
        image = load_image(path.read_bytes())
        agent = VerifyRefineAgent(muse, sam, debug_dir=OUT / f"{m['id']}_agent")
        pipe = Pipeline(muse, sam, agent=agent)
        t1 = time.perf_counter()
        resp = await pipe.measure(image=image)
        wall = time.perf_counter() - t1
        body = resp.model_dump()
        png = body.pop("overlay_png_b64")
        if png:
            (OUT / f"{m['id']}_overlay.png").write_bytes(base64.b64decode(png))
        (OUT / f"{m['id']}.json").write_text(json.dumps(body, indent=1), encoding="utf-8")
        sections.append(summarise(m["id"], m, body, wall))
        sys.stdout.write(
            f"{m['id']}: usable={body['usable']} method={body['method']} depth={body['depth_cm']} "
            f"sigma={body['depth_sigma_cm']} wall={wall:.1f}s cost={body['cost_usd']}\n"
        )
    header = (
        f"Generated by `uv run --extra local python ../eval/real_photos.py` on "
        f"{time.strftime('%Y-%m-%d')} with {muse.scene_model_name} ({muse.model} on Baseten) and "
        f"{sam.mask_model_name} on {sam.device} (loaded once in {load_s:.1f} s). "
        "No ground truth exists for these photos; only qualitative behaviour is shown."
    )
    if not only:
        write_results(sections, header)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--only", nargs="*", help="photo ids, such as c3 (results file not rewritten)")
    args = ap.parse_args(argv)
    return asyncio.run(run(args.only))


if __name__ == "__main__":
    raise SystemExit(main())
