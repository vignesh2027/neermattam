"""Run the open-weights pipeline on the four Wikimedia Commons flood photos.

Usage (from perception/, with the local extra installed):

    uv run --extra local python ../eval/real_photos.py            # fetch if needed, run, write
    uv run --extra local python ../eval/real_photos.py --only c3  # one photo
    uv run --extra local python ../eval/sam_ab.py                 # the SAM size comparison

Photos are downloaded from Commons into eval/photos/ (gitignored); the URL,
author and licence of each are in eval/commons_photos.json and (the Unsplash
close-ups) eval/unsplash_photos.json. Outputs go to
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
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from neermattam_perception.adapters import ModelError
from neermattam_perception.agent import VerifyRefineAgent
from neermattam_perception.media import load_image
from neermattam_perception.modal_sam import ModalSamAdapter
from neermattam_perception.muse_open import MuseSparkOpenAdapter
from neermattam_perception.pipeline import Pipeline
from neermattam_perception.sam_local import Sam21LocalAdapter

HERE = Path(__file__).resolve().parent
PHOTOS = HERE / "photos"
OUT = HERE / "out"
RESULTS = HERE / "RESULTS_REAL.md"
CLOSE_DIR = Path.home() / "neermattam-data" / "probe" / "close"
SETS = {"commons": "commons_photos.json", "close": "unsplash_photos.json"}
UA = "neermattam-eval/0.2 (https://github.com/vignesh2027; flood depth research)"


def fetch(meta: dict[str, object]) -> Path:
    PHOTOS.mkdir(exist_ok=True)
    path = PHOTOS / f"{meta['id']}.jpg"
    if path.exists() and path.stat().st_size > 0:
        return path
    local = Path(os.environ.get("NEERMATTAM_CLOSE_DIR", str(CLOSE_DIR))) / f"{meta['id']}.jpg"
    if meta.get("set") == "close" and local.is_file():
        shutil.copyfile(local, path)
        return path
    # IPv4 only: some networks advertise IPv6 routes that hang. 120 s at most.
    transport = httpx.HTTPTransport(local_address="0.0.0.0", retries=2)
    with httpx.Client(transport=transport, timeout=120.0, headers={"User-Agent": UA}) as c:
        r = c.get(str(meta["download"]), follow_redirects=True)
        r.raise_for_status()
        path.write_bytes(r.content)
    return path


def summarise(
    pid: str, meta: dict[str, Any], body: dict[str, Any], wall_s: float, out: Path = OUT
) -> str:
    kind = {
        "brief": "Commons, named in the brief",
        "extra": "Commons, side-on wheels",
        "close": "Unsplash close-up",
    }.get(str(meta.get("set")), str(meta.get("set")))
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
        + ", ".join(f"{k} {v}" for k, v in dict(timings).items())
        + f"); model cost USD {body.get('cost_usd')}."
    )
    lines.append("")
    rel = out.relative_to(HERE.parent) if out.is_relative_to(HERE.parent) else out
    shown = f" Verifier images: `{rel}/{pid}_agent/`." if (out / f"{pid}_agent").exists() else ""
    lines.append(f"Overlay: `{rel}/{pid}_overlay.png`.{shown}")
    lines.append("")
    lines.append("| Step | Target | Model | Input | Output | Decision | ms |")
    lines.append("|---|---|---|---|---|---|---|")
    for t in body.get("agent_trace", []):
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


def process_memory() -> dict[str, float]:
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return {"peak_rss_mb": round(peak / 1e6 if sys.platform == "darwin" else peak / 1e3, 1)}


def is_person(step: dict[str, Any]) -> bool:
    return str(step["target"]).startswith("person")


def photo_stats(body: dict[str, Any], wall_s: float) -> dict[str, Any]:
    """Counts from one response: wheels checked and accepted, depths measured, calls."""
    trace = body.get("agent_trace", [])
    wheels = {t["target"] for t in trace if t["step"] == "verify" and t["target"] != "water"}
    accepted = {
        t["target"]
        for t in trace
        if t["step"] in ("verify", "confirm") and t["decision"] == "accept"
    } - {"water"}
    # A confirm can overrule a quick accept.
    overruled = {t["target"] for t in trace if t["step"] == "confirm" and t["decision"] != "accept"}
    accepted -= overruled
    geo = [t for t in trace if t["step"] == "geometry"]
    depths = [t for t in geo if t["decision"] == "candidate" and not is_person(t)]
    people = [t for t in geo if is_person(t)]
    checks = [t for t in trace if t["step"] in ("verify", "confirm")]
    return {
        "wheels_checked": len(wheels),
        "wheels_accepted": len(accepted),
        "wheel_depths": len(depths),
        "geometry_outputs": [t["output"] for t in depths],
        "verifier_calls": len(checks),
        "persons_seen": len(people),
        "person_depths": sum(1 for t in people if t["decision"] == "candidate"),
        "person_outputs": [f"{t['target']}: {t['output']}" for t in people],
        "method": body.get("method"),
        "depth_cm": body.get("depth_cm"),
        "depth_sigma_cm": body.get("depth_sigma_cm"),
        "wall_s": round(wall_s, 1),
        "timings_ms": body.get("timings_ms"),
        "cost_usd": body.get("cost_usd"),
    }


async def run(args: argparse.Namespace) -> int:
    meta = []
    for name in args.sets:
        meta += json.loads((HERE / SETS[name]).read_text(encoding="utf-8"))["photos"]
    if args.only:
        meta = [m for m in meta if m["id"] in args.only]
    muse = MuseSparkOpenAdapter.from_env(
        scene_cache_dir=Path(args.scene_cache) if args.scene_cache else None,
        scene_effort=None if args.scene_effort == "default" else args.scene_effort,
    )
    if not muse.api_key:
        sys.stderr.write("BASETEN_API_KEY is not set (see the repository .env)\n")
        return 2
    sam: Any
    if args.modal:
        sam = ModalSamAdapter.from_env(model=args.sam_size)
        t0 = time.perf_counter()
        ok, why = await sam.ready()  # wakes the container; the first request is the cold one
        if not ok:
            sys.stderr.write(f"Modal SAM not ready: {why}\n")
            return 2
    else:
        sam = Sam21LocalAdapter(size=args.sam_size, dtype="float16" if args.fp16 else "float32")
        t0 = time.perf_counter()
        sam.load()
    load_s = time.perf_counter() - t0
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    sections = []
    stats: dict[str, Any] = {}
    for m in meta:
        path = fetch(m)
        image = load_image(path.read_bytes())
        agent = VerifyRefineAgent(
            muse, sam, debug_dir=out / f"{m['id']}_agent", confirm=args.confirm
        )
        pipe = Pipeline(muse, sam, agent=agent)
        for attempt in range(3):  # a network outage should not end a long run
            t1 = time.perf_counter()
            try:
                resp = await pipe.measure(image=image)
                break
            except ModelError as exc:
                if attempt == 2:
                    raise
                sys.stderr.write(f"{m['id']}: {exc}; trying again in 30 s\n")
                await asyncio.sleep(30)
        wall = time.perf_counter() - t1
        body = resp.model_dump()
        png = body.pop("overlay_png_b64")
        if png:
            (out / f"{m['id']}_overlay.png").write_bytes(base64.b64decode(png))
        (out / f"{m['id']}.json").write_text(json.dumps(body, indent=1), encoding="utf-8")
        sections.append(summarise(m["id"], m, body, wall, out))
        stats[m["id"]] = photo_stats(body, wall)
        sys.stdout.write(
            f"{m['id']}: method={body['method']} depth={body['depth_cm']} "
            f"sigma={body['depth_sigma_cm']} wall={wall:.1f}s cost={body['cost_usd']} "
            f"wheels {stats[m['id']]['wheels_accepted']}/{stats[m['id']]['wheels_checked']} "
            f"depths {stats[m['id']]['wheel_depths']} "
            f"persons {stats[m['id']]['person_depths']}/{stats[m['id']]['persons_seen']}\n"
        )
    summary = {
        "sam": sam.mask_model_name,
        "device": "Modal CPU" if args.modal else sam.device,
        "load_s": round(load_s, 1),
        "memory": process_memory() if args.modal else sam.memory(),
        "scene_effort": muse.scene_effort or "default",
        "confirm": args.confirm,
        "photos": stats,
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    header = (
        f"Generated by `uv run --extra local python ../eval/real_photos.py` on "
        f"{time.strftime('%Y-%m-%d')} with {muse.scene_model_name} ({muse.model} on Baseten, "
        f"scene reasoning effort {muse.scene_effort or 'default'}) and "
        f"{sam.mask_model_name} on {summary['device']} (ready in {load_s:.1f} s). "
        "No ground truth exists for these photos; only qualitative behaviour is shown."
    )
    if not args.only and args.write_results:
        write_results(sections, header)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--only", nargs="*", help="photo ids, such as c3 (results file not rewritten)")
    ap.add_argument("--sam-size", default="large", choices=["small", "base-plus", "large"])
    ap.add_argument("--fp16", action="store_true", help="SAM weights in float16")
    ap.add_argument("--modal", action="store_true", help="SAM on the Modal service (MODAL_SAM_*)")
    ap.add_argument("--scene-effort", default="low", help="low, medium, high, xhigh or default")
    ap.add_argument("--confirm", action="store_true", help="full-reasoning re-check of accepts")
    ap.add_argument("--scene-cache", default=None, help="folder to keep scene readings in")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--sets", nargs="*", default=["close", "commons"], choices=list(SETS))
    ap.add_argument(
        "--no-write-results",
        dest="write_results",
        action="store_false",
        help="do not rewrite eval/RESULTS_REAL.md",
    )
    args = ap.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
