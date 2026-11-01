"""Measure the India-wide archival flood photos with the running perception service.

Usage, from perception/ with the service already running on PERCEPTION_URL
(default http://127.0.0.1:8301) and NEERMATTAM_STORE_DIR set for it, so the
face-blurred copy of each photo can be read back:

    uv run python ../eval/india_photos.py --store "$NEERMATTAM_STORE_DIR"
    uv run python ../eval/india_photos.py --store ... --only chn1 kol2

The photo list, with author, licence, source page, capture date and place, is
eval/india_photos.json. Each photo is downloaded into eval/photos/india/
(gitignored) and sent to POST /v1/measure, the same endpoint the gateway
uses for a report, so the depth, sigma, method and signed certificate are
the service's own. Outputs:

- eval/out/india/<id>.json, <id>.certificate.json and <id>_overlay.png
  (gitignored), the raw answers;
- gateway/internal/photos/records/<id>.json, the record the gateway serves: the
  photo's metadata plus perception's whole answer (without the overlay), the
  certificate included;
- gateway/internal/web/static/photos/<id>.jpg, a 480 pixel wide thumbnail of
  the copy perception stored with the faces it found blurred;
- eval/RESULTS_INDIA.md, a table of what each photo returned, with latency and
  model cost.

These are archival photos. Nothing here posts to the engine: they never enter
live fusion. There is no ground truth: nobody measured the water in them.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, ImageFilter

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
META = HERE / "india_photos.json"
PHOTOS = HERE / "photos" / "india"
OUT = HERE / "out" / "india"
DATA = ROOT / "gateway" / "internal" / "photos" / "records"
THUMBS = ROOT / "gateway" / "internal" / "web" / "static" / "photos"
RESULTS = HERE / "RESULTS_INDIA.md"
UA = "neermattam-eval/0.3 (https://github.com/vignesh2027; flood depth research)"
THUMB_WIDTH = 480


def fetch(meta: dict[str, Any]) -> Path:
    """Download one photo with curl over IPv4, retrying when Commons rate limits."""
    PHOTOS.mkdir(parents=True, exist_ok=True)
    path = PHOTOS / f"{meta['id']}.jpg"
    if path.exists() and path.read_bytes()[:2] == b"\xff\xd8":
        return path
    for attempt in range(6):
        out = subprocess.run(
            ["curl", "-4", "-s", "-L", "--max-time", "120", "-A", UA, "-o", str(path),
             "-w", "%{http_code}", str(meta["download"])],
            capture_output=True, text=True, check=False,
        )
        if out.stdout.strip() == "200" and path.read_bytes()[:2] == b"\xff\xd8":
            return path
        time.sleep(5 * (attempt + 1))
    raise SystemExit(f"{meta['id']}: download failed ({out.stdout.strip()})")


def thumbnail(src: Path, dst: Path) -> None:
    """A small JPEG of perception's face-blurred copy, with no metadata."""
    im = Image.open(src).convert("RGB")
    h = round(im.height * THUMB_WIDTH / im.width)
    im = im.resize((THUMB_WIDTH, h), Image.Resampling.LANCZOS).filter(ImageFilter.SMOOTH)
    dst.parent.mkdir(parents=True, exist_ok=True)
    im.save(dst, "JPEG", quality=78, optimize=True, progressive=True)


def measure(client: httpx.Client, base: str, path: Path) -> tuple[dict[str, Any], float]:
    t0 = time.perf_counter()
    for attempt in range(3):
        try:
            r = client.post(
                f"{base}/v1/measure",
                files={"image": (path.name, path.read_bytes(), "image/jpeg")},
            )
        except httpx.HTTPError as exc:
            if attempt == 2:
                raise
            sys.stderr.write(f"{path.stem}: {exc}; trying again in 20 s\n")
            time.sleep(20)
            continue
        if r.status_code == 503 and attempt < 2:
            sys.stderr.write(f"{path.stem}: 503 {r.text[:200]}; trying again in 20 s\n")
            time.sleep(20)
            continue
        r.raise_for_status()
        return r.json(), time.perf_counter() - t0
    raise RuntimeError("unreachable")


def method_label(body: dict[str, Any]) -> str:
    if not body.get("usable"):
        return "not usable"
    return {
        "wheel_geometry": "wheel",
        "person_reference": "person",
        "model_estimate": "model estimate (labelled)",
    }.get(str(body.get("method")), str(body.get("method")))


def run(args: argparse.Namespace) -> int:
    doc = json.loads(META.read_text(encoding="utf-8"))
    photos = [p for p in doc["photos"] if not args.only or p["id"] in args.only]
    store = Path(args.store)
    OUT.mkdir(parents=True, exist_ok=True)
    DATA.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    rows: list[str] = []
    total_cost = 0.0
    with httpx.Client(timeout=300.0, transport=httpx.HTTPTransport(local_address="0.0.0.0")) as c:
        for p in photos:
            path = fetch(p)
            body, wall = measure(c, args.perception, path)
            png = body.pop("overlay_png_b64", None)
            if png:
                (OUT / f"{p['id']}_overlay.png").write_bytes(base64.b64decode(png))
            (OUT / f"{p['id']}.json").write_text(json.dumps(body, indent=1), encoding="utf-8")
            if body.get("certificate"):
                (OUT / f"{p['id']}.certificate.json").write_text(
                    json.dumps(body["certificate"], indent=1), encoding="utf-8"
                )
            mid = body["measurement_id"]
            blurred = store / f"{mid}.jpg"
            if not blurred.exists():
                raise SystemExit(f"{p['id']}: perception stored no copy at {blurred}; is NEERMATTAM_STORE_DIR set for it?")
            thumbnail(blurred, THUMBS / f"{p['id']}.jpg")
            record = {
                "id": p["id"],
                "city": p["city"],
                "place": p["place"],
                "lat": p["lat"],
                "lon": p["lon"],
                "coord_source": p["coord_source"],
                "photo_date": p["date"],
                "measured_on": today,
                "title": p["title"],
                "source_url": p["page"],
                "author": p["author"],
                "licence": p["licence"],
                "licence_url": p["licence_url"],
                "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "wall_s": round(wall, 1),
                "measurement": body,
            }
            (DATA / f"{p['id']}.json").write_text(
                json.dumps(record, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            cost = float(body.get("cost_usd") or 0.0)
            total_cost += cost
            depth = "" if body.get("depth_cm") is None else f"{body['depth_cm']:.1f}"
            sigma = "" if body.get("depth_sigma_cm") is None else f"{body['depth_sigma_cm']:.1f}"
            rows.append(
                f"| {p['id']} | {p['city']} | {p['place']} | {p['date'][:4]} | {depth} | {sigma} | "
                f"{method_label(body)} | {p['licence']} | {wall:.1f} | {cost:.4f} | `{mid}` |"
            )
            sys.stdout.write(
                f"{p['id']}: {method_label(body)} depth={depth} sigma={sigma} wall={wall:.1f}s cost={cost}\n"
            )
            sys.stdout.flush()
    if not args.only:
        RESULTS.write_text(
            "# Measured photos across India\n\n"
            f"Generated by `uv run python ../eval/india_photos.py` on {today} against the perception service "
            "(Muse Spark Open on Baseten and SAM 2.1 hiera-small, the deployed configuration), one POST to "
            "`/v1/measure` per photo. The photos and their credits are listed in `eval/india_photos.json`.\n\n"
            "There is no ground truth: nobody measured the water in these photos, so nothing here is accuracy. "
            "They are archival photos from the years shown. The gateway shows them as a separate, labelled layer "
            "and never fuses them into the live map.\n\n"
            "| Photo | City | Place | Year | Depth cm | Sigma cm | Method | Licence | Wall s | Model cost USD | Measurement |\n"
            "|---|---|---|---|---|---|---|---|---|---|---|\n"
            + "\n".join(rows)
            + f"\n\nTotal model cost for this run: USD {total_cost:.4f}.\n",
            encoding="utf-8",
        )
    sys.stdout.write(f"total cost USD {total_cost:.4f}\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--perception", default="http://127.0.0.1:8301")
    ap.add_argument("--store", required=True, help="the NEERMATTAM_STORE_DIR the service writes to")
    ap.add_argument("--only", nargs="*", help="photo ids; RESULTS_INDIA.md is not rewritten")
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
