"""Measure the Modal SAM service on the probe photos and draw mask overlays.

    python deploy/modal-sam/measure.py                 # hosted service
    python deploy/modal-sam/measure.py --local small   # same prompts, local SamRunner

Reads MODAL_SAM_URL and MODAL_SAM_TOKEN from the repository's .env. Prints a
JSON summary (cold start, warm latency with 1 and 6 prompts, per-photo
timings) and writes overlays next to the photos: ``modal_<photo>.png`` for the
hosted run, ``local_<size>_<photo>.png`` for a local run, plus a crop of the
three scooter candidates on c3.

Prompts are written for the 900 px wide probe copies and scaled to the real
image size.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import client

REPO = HERE.parents[1]
PROBE = Path(os.environ.get("NEERMATTAM_PROBE_DIR", Path.home() / "neermattam-data" / "probe"))
REF_WIDTH = 900.0

# id: box [x1, y1, x2, y2] or ("point", x, y), in the 900 px wide copies.
PROBES: dict[str, dict[str, Any]] = {
    "c3": {
        "scooters": [560, 580, 700, 660],
        "wader": [252, 553, 310, 625],
        "person_b": [212, 553, 256, 602],
        "person_c": [158, 545, 198, 597],
        "bike_by_tree": [428, 555, 505, 605],
        "water": ("point", 330, 650),
    },
    "c1": {
        "tree": [440, 0, 775, 485],
        "person_left": [10, 228, 56, 335],
        "person_2": [106, 224, 154, 326],
        "motorbike": [432, 230, 477, 282],
        "water": ("point", 300, 420),
    },
    "c2": {
        "rider": [455, 278, 500, 356],
        "covered_scooter": [254, 286, 336, 420],
        "parked_bike": [780, 283, 823, 360],
        "car": [847, 272, 900, 365],
        "person_left": [8, 255, 66, 466],
        "water": ("point", 600, 560),
    },
    "c4": {
        "rooftop_people": [558, 160, 792, 224],
        "pushcart": [210, 393, 322, 450],
        "tree": [288, 215, 447, 390],
        "shop_front": [495, 340, 660, 440],
        "sign": [640, 292, 780, 345],
        "water": ("point", 750, 520),
    },
}
COLOURS = [
    (37, 99, 235),
    (159, 18, 57),
    (76, 29, 149),
    (14, 165, 233),
    (234, 179, 8),
    (22, 163, 74),
]


def load_env() -> None:
    env = REPO / ".env"
    if not env.is_file():
        return
    for line in env.read_text().splitlines():
        k, sep, v = line.partition("=")
        if sep and k.strip() in ("MODAL_SAM_URL", "MODAL_SAM_TOKEN"):
            os.environ.setdefault(k.strip(), v.strip().strip('"'))


def prompts_for(name: str, width: int) -> list[client.SamPrompt]:
    s = width / REF_WIDTH
    out = []
    for pid, spec in PROBES[name].items():
        if isinstance(spec, tuple):
            out.append(client.SamPrompt(pid, positive_points=((spec[1] * s, spec[2] * s),)))
        else:
            out.append(client.SamPrompt(pid, box=tuple(v * s for v in spec)))  # type: ignore[arg-type]
    return out


def overlay(
    rgb: np.ndarray, prompts: list[client.SamPrompt], masks: dict[Any, list[Any]], path: Path
) -> None:
    out = rgb.astype(np.float32).copy()
    for i, p in enumerate(prompts):
        cands = masks.get(p.id) or []
        if not cands:
            continue
        m = cands[0].mask
        col = np.array(COLOURS[i % len(COLOURS)], dtype=np.float32)
        out[m] = 0.45 * out[m] + 0.55 * col
    im = Image.fromarray(out.clip(0, 255).astype(np.uint8))
    d = ImageDraw.Draw(im)
    for i, p in enumerate(prompts):
        col = COLOURS[i % len(COLOURS)]
        cands = masks.get(p.id) or []
        score = f" {cands[0].score:.2f}" if cands else ""
        if p.box is not None:
            d.rectangle(p.box, outline=(255, 255, 255), width=1)
            anchor = (p.box[0], max(0, p.box[1] - 12))
        else:
            x, y = p.positive_points[0]
            d.ellipse((x - 4, y - 4, x + 4, y + 4), fill=(255, 255, 255), outline=col)
            anchor = (x + 6, y - 6)
        d.text(anchor, f"{p.id}{score}", fill=(255, 255, 255), stroke_width=2, stroke_fill=col)
    im.save(path)


def candidates_strip(rgb: np.ndarray, cands: list[Any], box: tuple[float, ...], path: Path) -> None:
    """The scooter box's candidates side by side, zoomed 3x, with score and area."""
    pad = 30
    x1, y1 = max(0, int(box[0]) - pad), max(0, int(box[1]) - pad)
    x2, y2 = min(rgb.shape[1], int(box[2]) + pad), min(rgb.shape[0], int(box[3]) + pad)
    tiles = []
    for c in cands:
        crop = rgb[y1:y2, x1:x2].astype(np.float32).copy()
        m = c.mask[y1:y2, x1:x2]
        crop[m] = 0.45 * crop[m] + 0.55 * np.array(COLOURS[0], dtype=np.float32)
        t = Image.fromarray(crop.astype(np.uint8))
        t = t.resize((t.width * 3, t.height * 3), Image.NEAREST)
        ImageDraw.Draw(t).text(
            (6, 6),
            f"#{c.index} score {c.score:.3f} area {c.area}",
            fill=(255, 255, 255),
            stroke_width=2,
            stroke_fill=(0, 0, 0),
        )
        tiles.append(t)
    strip = Image.new("RGB", (sum(t.width for t in tiles) + 4 * (len(tiles) - 1), tiles[0].height))
    x = 0
    for t in tiles:
        strip.paste(t, (x, 0))
        x += t.width + 4
    strip.save(path)


class LocalBackend:
    """The same prompts through sam_core.SamRunner on this machine."""

    def __init__(self, size: str) -> None:
        import sam_core

        self.core = sam_core
        self.runner = sam_core.SamRunner(key=size)
        t0 = time.perf_counter()
        self.runner.load()
        self.load_ms = round(1000 * (time.perf_counter() - t0))

    def segment(self, path: Path, prompts: list[client.SamPrompt]) -> client.SamResponse:
        rgb = np.asarray(Image.open(path).convert("RGB"))
        h, w = rgb.shape[:2]
        parsed = self.core.parse_prompts([p.to_json() for p in prompts], w, h)
        t0 = time.perf_counter()
        out = self.runner.segment(rgb, parsed)
        rtt = round(1000 * (time.perf_counter() - t0), 1)
        masks = {
            r["id"]: [
                client.SamMask(c["mask"], c["score"], c["bbox"], c["area"], c["index"])
                for c in r["candidates"]
            ]
            for r in out["results"]
        }
        return client.SamResponse(
            masks, self.runner.model_name, str(self.runner.device), (w, h), out["timings_ms"], rtt, {}
        )


class RemoteBackend:
    def __init__(self, model: str, precision: str | None) -> None:
        self.model = model
        self.precision = precision

    def segment(self, path: Path, prompts: list[client.SamPrompt]) -> client.SamResponse:
        return client.segment(
            path.read_bytes(), prompts, model=self.model, precision=self.precision, timeout=600
        )


def summarise(res: client.SamResponse) -> dict[str, Any]:
    return {
        "round_trip_ms": res.round_trip_ms,
        "server_ms": res.timings_ms,
        "best": {
            str(pid): {"score": c[0].score, "area": c[0].area, "bbox": c[0].bbox}
            for pid, c in res.masks.items()
            if c
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", metavar="SIZE", help="run SamRunner locally instead (small, ...)")
    ap.add_argument("--model", default="large")
    ap.add_argument("--precision", choices=["fp32", "bf16"])
    ap.add_argument("--warm-runs", type=int, default=5)
    ap.add_argument("--tag", default="", help="suffix for overlay names")
    ap.add_argument("--skip-cold", action="store_true")
    args = ap.parse_args()
    load_env()

    report: dict[str, Any] = {}
    if args.local:
        backend: Any = LocalBackend(args.local)
        prefix = f"local_{args.local}"
        report["load_ms"] = backend.load_ms
    else:
        backend = RemoteBackend(args.model, args.precision)
        prefix = "modal" + (f"_{args.model}" if args.model != "large" else "")
    prefix += args.tag
    c3 = PROBE / "c3.jpg"
    w3 = Image.open(c3).width
    one = [p for p in prompts_for("c3", w3) if p.id == "scooters"]
    six = prompts_for("c3", w3)

    if not args.local and not args.skip_cold:
        t0 = time.perf_counter()
        res = backend.segment(c3, one)
        report["cold_first_request_ms"] = round(1000 * (time.perf_counter() - t0), 1)
        report["cold_server_ms"] = res.timings_ms
    else:
        backend.segment(c3, one)  # warm-up

    for label, prompts in (("warm_1_prompt", one), ("warm_6_prompts", six)):
        runs = [backend.segment(c3, prompts) for _ in range(args.warm_runs)]
        report[label] = {
            "round_trip_ms_median": statistics.median(r.round_trip_ms for r in runs),
            "round_trip_ms_all": [r.round_trip_ms for r in runs],
            "server_ms_last": runs[-1].timings_ms,
        }

    rgb3 = np.asarray(Image.open(c3).convert("RGB"))
    res = backend.segment(c3, one)
    candidates_strip(rgb3, res.masks["scooters"], one[0].box, PROBE / f"{prefix}_c3_scooters.png")

    report["photos"] = {}
    for name in PROBES:
        path = PROBE / f"{name}.jpg"
        rgb = np.asarray(Image.open(path).convert("RGB"))
        prompts = prompts_for(name, rgb.shape[1])
        res = backend.segment(path, prompts)
        overlay(rgb, prompts, res.masks, PROBE / f"{prefix}_{name}.png")
        report["photos"][name] = summarise(res)
        report["model"], report["device"] = res.model, res.device
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
