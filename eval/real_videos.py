"""Run the open-weights pipeline on short real flood videos and measure the flow speed.

Usage (from perception/, with the local extra installed):

    uv run --extra local python ../eval/real_videos.py                 # all clips
    uv run --extra local python ../eval/real_videos.py --only gera --no-synthetic  # one clip again
    uv run --extra local python ../eval/real_videos.py --synthetic-only

Clips come from Wikimedia Commons (eval/video_sources.json: URL, author,
licence and the segment used). The originals are downloaded into eval/videos/
(gitignored), checked against the SHA-1 Commons publishes, and the segment is
cut with ffmpeg (which must be on the PATH). Outputs go to eval/out/videos/
(gitignored): the depth overlay of the key frame, the response JSON with its
agent trace, the tracks and per-track verdicts (``<id>.flow.json``) and a GIF
of the tracked debris for the clips given with ``--gif``. Each clip's report
section is stored there too, and the generated section of eval/RESULTS_VIDEO.md
is assembled from every clip's last stored run, between its markers.

Each clip goes through ``Pipeline.measure(video=...)`` exactly as an upload
would: Muse Spark Open reads the key frame, SAM 2.1 and the verifier measure a
reference, then Muse Spark Open boxes the floating debris and SAM 2.1's video
predictor tracks it (perception flow.py). When no reference is measured the
pipeline gives no speed; the debris is then still located and tracked here, so
the motion is reported in pixels per second.

Two checks, neither of them accuracy on real water:

* Known motion: synthetic clips with drifting blobs at a known speed (one with
  a still camera, one panning) go through the same tracker and velocity code,
  with the true boxes standing in for the scene model. This checks the tracker
  and the arithmetic, not the debris locator and not real water.
* Plausible range: each real estimate is compared with 0.05 to 3 m/s, our own
  judgement of street flood flow (slower than about 0.05 m/s, drift is lost in
  wind and wave motion; faster than about 3 m/s is torrent flow). Inside the
  range means only "not absurd".

Live: calls Muse Spark Open on Baseten (BASETEN_* in the repository .env) and
runs SAM 2.1 locally. SAM 2.1's video predictor takes seconds per frame on a
laptop, so a clip takes minutes.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import itertools
import json
import math
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import cv2
import httpx
import numpy as np
from PIL import Image

from neermattam_perception.adapters import ModelError
from neermattam_perception.agent import VerifyRefineAgent
from neermattam_perception.certificate import Signer
from neermattam_perception.flow import FlowResult, FlowScale, measure_flow
from neermattam_perception.media import load_video
from neermattam_perception.muse_open import JsonReply, MuseSparkOpenAdapter, Usage
from neermattam_perception.pipeline import Pipeline, PipelineConfig
from neermattam_perception.sam_local import Sam21LocalAdapter

HERE = Path(__file__).resolve().parent
VIDEOS = HERE / "videos"
OUT = HERE / "out" / "videos"
RESULTS = HERE / "RESULTS_VIDEO.md"
SOURCES = HERE / "video_sources.json"
UA = "neermattam-eval/0.2 (https://github.com/vignesh2027; flood depth research)"
BEGIN, END = "<!-- video-eval:begin -->", "<!-- video-eval:end -->"
# Our judgement of surface speeds that are not absurd for flood water on a street
# (see the module docstring). Not a source, not an accuracy bound.
PLAUSIBLE_MPS = (0.05, 3.0)
# Colours from the project palette: kept tracks, rejected tracks, the anchor.
KEPT, REJECTED, ANCHOR = (37, 99, 235), (159, 18, 57), (76, 29, 149)


# --------------------------------------------------------------------------- clips


def fetch(meta: dict[str, Any]) -> Path:
    """The original from Commons (IPv4, 300 s at most), checked against its SHA-1."""
    VIDEOS.mkdir(exist_ok=True)
    name = str(meta["download"]).rsplit("/", 1)[-1]
    path = VIDEOS / f"{meta['sha1'][:12]}{Path(name).suffix}"
    if not (path.exists() and path.stat().st_size > 0):
        transport = httpx.HTTPTransport(local_address="0.0.0.0", retries=3)
        with httpx.Client(
            transport=transport,
            timeout=httpx.Timeout(300.0, connect=20.0),
            headers={"User-Agent": UA},
        ) as c:
            r = c.get(str(meta["download"]), follow_redirects=True)
            r.raise_for_status()
            path.write_bytes(r.content)
    sha1 = hashlib.sha1(path.read_bytes()).hexdigest()
    if sha1 != meta["sha1"]:
        path.unlink()
        raise RuntimeError(f"{meta['id']}: SHA-1 {sha1} is not the one Commons publishes")
    return path


def cut(meta: dict[str, Any], src: Path) -> Path:
    """The clip's segment as H.264 MP4, no audio and no metadata (frame-accurate)."""
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg is needed to cut the clips")
    dst = VIDEOS / f"{meta['id']}.mp4"
    if dst.exists() and dst.stat().st_size > 0:
        return dst
    cmd = [
        "ffmpeg", "-v", "error", "-y", "-ss", str(meta["start_s"]), "-i", str(src),
        "-t", str(meta["duration_s"]), "-an", "-map_metadata", "-1",
        "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", str(dst),
    ]  # fmt: skip
    subprocess.run(cmd, check=True, timeout=300)
    return dst


# --------------------------------------------------------------------------- outputs


def flow_json(res: FlowResult) -> dict[str, Any]:
    """Everything the flow step found, for eval/out/videos/<id>.flow.json."""
    out: dict[str, Any] = {
        "reason": res.reason,
        "fps": res.fps,
        "scale": None
        if res.scale is None
        else {
            "cm_per_px": res.scale.cm_per_px,
            "rel_sigma": res.scale.rel_sigma,
            "source": res.scale.source,
            "at_xy": res.scale.at_xy,
        },
        "n_located": res.n_located,
        "anchor_used": res.anchor_used,
        "anchor_shift_px": res.anchor_shift_px,
        "cost_usd": res.cost_usd,
        "timings_ms": res.timings_ms,
        "verdicts": [vars(v) for v in res.verdicts],
    }
    if res.reading is not None:
        out["debris"] = [
            {"box": p.box, "point": p.point, "what": p.what} for p in res.reading.debris
        ]
        out["anchor"] = (
            None
            if res.reading.anchor is None
            else {"box": res.reading.anchor.box, "what": res.reading.anchor.what}
        )
        out["flow_words"] = res.reading.flow
        out["dropped"] = list(res.reading.dropped)
    if res.tracks is not None:
        out["frames"] = res.tracks.frames
        out["stride"] = res.tracks.stride
        out["sam_load_ms"] = res.tracks.load_ms
        out["objects"] = {
            k: {
                "prompt_box": o.prompt_box,
                "points": o.points,
                "areas": o.areas,
                "boxes": o.boxes,
                "lost": o.lost,
            }
            for k, o in res.tracks.objects.items()
        }
    pe = res.pixel_estimate
    if pe is not None:
        out["pixel_estimate"] = {
            "surface_px_s": pe.surface_mps,
            "sigma_px_s": pe.surface_sigma_mps,
            "n_used": pe.n_used,
            "rejected": pe.rejected,
        }
    if res.tracks is not None:
        out["region"] = res.tracks.region
    e = res.estimate
    if e is not None:
        out["estimate"] = {
            "surface_mps": e.surface_mps,
            "surface_sigma_mps": e.surface_sigma_mps,
            "mean_mps": e.mean_mps,
            "mean_sigma_mps": e.mean_sigma_mps,
            "n_tracks": e.n_tracks,
            "n_used": e.n_used,
            "rejected": e.rejected,
        }
    return out


def write_gif(clip: Path, res: FlowResult, dst: Path, width: int = 480) -> bool:
    """The tracked frames with each track's box and trail drawn: kept, rejected, anchor."""
    tr = res.tracks
    if tr is None or not tr.frames:
        return False
    kept = {v.track_id for v in res.verdicts if v.kept}
    anchor_id = list(tr.objects)[-1] if res.reading is not None and res.reading.anchor else None
    by_frame = {
        k: {f: (b, (x, y)) for (f, x, y), b in zip(o.points, o.boxes, strict=True)}
        for k, o in tr.objects.items()
    }
    want = set(tr.frames)
    cap = cv2.VideoCapture(str(clip))
    frames: list[Image.Image] = []
    i = -1
    while True:
        ok, bgr = cap.read()
        if not ok:
            break
        i += 1
        if i not in want:
            continue
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        h, w = rgb.shape[:2]
        th = max(1, round(2 * w / 640))
        for k, seen in by_frame.items():
            col = ANCHOR if k == anchor_id else (KEPT if k in kept else REJECTED)
            trail = [np.round(c).astype(int) for f, (_, c) in sorted(seen.items()) if f <= i]
            for a, b in itertools.pairwise(trail):
                cv2.line(rgb, tuple(map(int, a)), tuple(map(int, b)), col, th)
            if i in seen:
                x0, y0, x1, y1 = seen[i][0]
                cv2.rectangle(rgb, (x0 - 3, y0 - 3), (x1 + 3, y1 + 3), col, th)
                cv2.putText(
                    rgb,
                    "A" if k == anchor_id else k,
                    (x0, max(12, y0 - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.5 * w / 640,
                    col,
                    th,
                )
        scale = width / w
        frames.append(Image.fromarray(cv2.resize(rgb, (width, round(h * scale)))))
    cap.release()
    if not frames:
        return False
    ms = round(1000 * tr.stride / res.fps)
    frames[0].save(dst, save_all=True, append_images=frames[1:], duration=ms, loop=0)
    return True


# --------------------------------------------------------------------------- known motion


class _KnownBoxes:
    """Stands in for the debris locator in the synthetic check: the true boxes."""

    def __init__(self, debris: list[Box], anchor: Box, w: int, h: int):
        def grid(b: Box) -> list[float]:
            return [1000 * b[0] / w, 1000 * b[1] / h, 1000 * b[2] / w, 1000 * b[3] / h]

        self.data = {
            "debris": [{"box": grid(b), "what": "blob"} for b in debris],
            "anchor": {"box": grid(anchor), "what": "square"},
            "flow": "known",
        }

    async def locate_debris_json(self, image: Any) -> JsonReply:
        return JsonReply(self.data, Usage(), 0, "")


Box = tuple[int, int, int, int]


def synth_clip(
    path: Path,
    *,
    pan_px_s: float,
    v_px_s: list[tuple[float, float]],
    fps: float = 25.0,
    seconds: float = 3.0,
    seed: int = 0,
) -> Any:
    """A 640x360 clip: a still textured scene (the 'water' and a fixed white square),
    blobs drifting at known pixel speeds, and the whole view panning at ``pan_px_s``.
    Returns a function of the frame number giving the blobs' boxes and the square's box."""
    rng = np.random.default_rng(seed)
    W, H, pad = 640, 360, 200
    noise = rng.normal(0, 1, (H + 2 * pad, W + 2 * pad)).astype(np.float32)
    blurred = np.asarray(cv2.GaussianBlur(noise, (0, 0), 3), dtype=np.float64)
    tex = (blurred - blurred.min()) / (blurred.max() - blurred.min())
    world = np.stack([90 + 60 * tex, 75 + 50 * tex, 55 + 40 * tex], axis=-1).astype(np.uint8)
    sq = (pad + 40, pad + 30, pad + 70, pad + 60)  # the fixed square, in world pixels
    world[sq[1] : sq[3], sq[0] : sq[2]] = 240
    starts = [(pad + 120 + 90 * i, pad + 120 + 40 * (i % 2)) for i in range(len(v_px_s))]
    colours = [(30, 160, 60), (200, 200, 40), (220, 120, 30), (40, 180, 170)]
    n = round(seconds * fps)
    out = cv2.VideoWriter(str(path), cv2.VideoWriter.fourcc(*"mp4v"), fps, (W, H))
    for f in range(n):
        t = f / fps
        frame = world.copy()
        for (x0, y0), (vx, vy), col in zip(starts, v_px_s, colours, strict=False):
            c = (round(x0 + vx * t), round(y0 + vy * t))
            cv2.ellipse(frame, c, (9, 6), 20, 0, 360, col, -1)
        ox = pad - round(pan_px_s * t)  # the view slides right over the world: content moves left
        view = frame[pad : pad + H, ox : ox + W]
        out.write(cv2.cvtColor(np.ascontiguousarray(view), cv2.COLOR_RGB2BGR))
    out.release()

    def boxes_at(f: int) -> tuple[list[Box], Box]:
        t = f / fps
        ox = pad - round(pan_px_s * t)
        blobs = []
        for (x0, y0), (vx, vy) in zip(starts, v_px_s, strict=False):
            x, y = round(x0 + vx * t) - ox, round(y0 + vy * t) - pad
            blobs.append((x - 12, y - 12, x + 13, y + 13))
        square = (sq[0] - ox - 4, sq[1] - pad - 4, sq[2] - ox + 4, sq[3] - pad + 4)
        return blobs, square

    return boxes_at


async def known_motion(sam: Sam21LocalAdapter, out: Path) -> tuple[list[dict[str, Any]], str]:
    """Synthetic clips at a known speed through the tracker and the velocity code."""
    cm_per_px = 0.5  # an assumed scale: 1 px is 0.5 cm, so 100 px/s is 0.5 m/s
    cases = [
        ("still camera", 0.0, [(80.0, 10.0), (80.0, 10.0), (84.0, 6.0), (76.0, 12.0)]),
        ("camera panning 40 px/s", 40.0, [(80.0, 10.0), (80.0, 10.0), (84.0, 6.0), (76.0, 12.0)]),
    ]
    rows = []
    lines = []
    for name, pan, vel in cases:
        path = out / f"synthetic_{'pan' if pan else 'still'}.mp4"
        boxes_at = synth_clip(path, pan_px_s=pan, v_px_s=vel)
        video = load_video(path.read_bytes())
        blobs, square = boxes_at(video.key_index)  # tracking starts at the key frame
        loc = _KnownBoxes(blobs, square, video.width, video.height)
        scale = FlowScale(cm_per_px, 0.0, "assumed 0.5 cm per pixel")
        t0 = time.perf_counter()
        res = await measure_flow(video, loc, sam, scale)
        wall = time.perf_counter() - t0
        true = float(np.median([math.hypot(vx, vy) for vx, vy in vel])) * cm_per_px / 100.0
        e = res.estimate
        row: dict[str, Any] = {
            "case": name,
            "true_surface_mps": round(true, 4),
            "wall_s": round(wall, 1),
        }
        if e is None:
            row["result"] = res.reason
            lines.append(f"| {name} | {true:.3f} | none ({res.reason}) | | | {wall:.0f} |")
        else:
            err = e.surface_mps - true
            row.update(
                surface_mps=round(e.surface_mps, 4),
                sigma_mps=round(e.surface_sigma_mps, 4),
                error_mps=round(err, 4),
                n_used=e.n_used,
                anchor_used=res.anchor_used,
            )
            lines.append(
                f"| {name} | {true:.3f} | {e.surface_mps:.3f} (sigma {e.surface_sigma_mps:.3f}) "
                f"| {err:+.3f} ({100 * err / true:+.1f}%) | {e.n_used} of {len(vel)} "
                f"| {wall:.0f} |"
            )
        rows.append(row)
    table = "\n".join(
        [
            "| Case | True surface speed (m/s) | Measured (m/s) | Error | Tracks used | Wall s |",
            "|---|---|---|---|---|---|",
            *lines,
        ]
    )
    return rows, table


# --------------------------------------------------------------------------- report


def verdict_table(res: FlowResult) -> list[str]:
    if not res.verdicts:
        return []
    rows = [
        "| Track | What | Found on frames | Lost | Kept | Reason | px/s | Speed m/s (sigma) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for v in res.verdicts:
        speed = "" if v.speed_mps is None else f"{v.speed_mps:.2f} ({v.sigma_mps:.2f})"
        pxs = "" if v.px_per_s is None else f"{v.px_per_s:.0f}"
        rows.append(
            f"| {v.track_id} | {v.what} | {v.n_frames} | {v.n_lost} | "
            f"{'yes' if v.kept else 'no'} | {v.reason or ''} | {pxs} | {speed} |"
        )
    return rows


def summarise(
    meta: dict[str, Any],
    body: dict[str, Any],
    res: FlowResult | None,
    wall_s: float,
    extra_cost: float,
    gif: Path | None,
) -> str:
    cid = meta["id"]
    lines = [f"### {cid}: {meta['title']}, {meta['start_s']} s to "
             f"{meta['start_s'] + meta['duration_s']:.2f} s", ""]  # fmt: skip
    lines.append(
        f"Author {meta['author']}, licence {meta['licence']} ({meta['licence_url']}), "
        f"source {meta['page']}. {meta['place']}, {meta['date']}."
    )
    lines.append("")
    ref = body.get("reference")
    if body.get("usable") and body.get("method") in ("wheel_geometry", "person_reference"):
        kind = (ref or {}).get("kind", "?")
        lines.append(
            f"Key frame: **{body['method']}** on a {kind}, depth {body['depth_cm']} cm "
            f"(sigma {body['depth_sigma_cm']} cm)."
        )
    elif body.get("usable"):
        lines.append(f"Key frame: no reference measured; {body['method']} {body['depth_cm']} cm.")
    else:
        lines.append("Key frame: not usable, no reference measured.")
    scale = res.scale if res is not None else None
    lines.append(
        "Scale reference: "
        + (
            f"{scale.source}, {scale.cm_per_px:.3f} cm/px (sigma {100 * scale.rel_sigma:.0f}%)."
            if scale is not None
            else "none, so no speed in metres per second."
        )
    )
    lines.append("")
    if body.get("velocity_mps") is not None and res is not None and res.estimate is not None:
        e = res.estimate
        lo, hi = PLAUSIBLE_MPS
        inside = lo <= e.surface_mps <= hi
        lines.append(
            f"**Speed: surface {e.surface_mps:.2f} m/s (sigma {e.surface_sigma_mps:.2f}), "
            f"depth-averaged {body['velocity_mps']} m/s (sigma {body['velocity_sigma_mps']})**, "
            f"from {e.n_used} of {len(res.verdicts)} tracks."
        )
        lines.append(
            f"Plausible-range check ({lo} to {hi} m/s, not accuracy): "
            + ("inside." if inside else "**outside**.")
        )
    else:
        lines.append(f"**No speed.** {res.reason if res is not None else 'flow not run'}.")
    if res is not None and res.reading is not None:
        anchor_what = res.reading.anchor.what if res.reading.anchor is not None else ""
        cam = (
            f"anchor ({anchor_what}) tracked, camera moved up to "
            f"{res.anchor_shift_px:.0f} px and that motion was removed"
            if res.anchor_used and res.anchor_shift_px is not None
            else "camera motion not removed (no usable anchor)"
        )
        lines.append(
            f"Debris boxed by the scene model: {res.n_located}, of which "
            f"{len(res.reading.debris)} held an object distinct from the water; "
            "scene model's flow: "
            f'"{res.reading.flow or "not stated"}"; {cam}.'
        )
    lines.append("")
    lines += verdict_table(res) if res is not None else []
    lines.append("")
    t = body.get("timings_ms", {})
    flow_t = res.timings_ms if res is not None else {}
    cost = float(body.get("cost_usd") or 0.0) + extra_cost
    lines.append(
        f"Wall time {wall_s:.0f} s (pipeline ms: "
        + ", ".join(f"{k} {v}" for k, v in dict(t).items())
        + "; flow ms: "
        + (", ".join(f"{k} {v}" for k, v in flow_t.items()) or "none")
        + f"). Model cost USD {cost:.4f} (Muse Spark Open; SAM 2.1 runs locally)."
    )
    rel = OUT.relative_to(HERE.parent)
    files = f"`{rel}/{cid}.json`, `{rel}/{cid}.flow.json`, `{rel}/{cid}_overlay.png`"
    if gif is not None:
        files += f", GIF `{rel}/{gif.name}`"
    lines.append(f"Files: {files}.")
    lines.append("")
    return "\n".join(lines)


def write_results(body: str) -> None:
    if RESULTS.exists():
        text = RESULTS.read_text(encoding="utf-8")
        if BEGIN in text and END in text:
            pat = re.compile(re.escape(BEGIN) + ".*?" + re.escape(END), re.DOTALL)
            RESULTS.write_text(pat.sub(lambda _: f"{BEGIN}\n{body}\n{END}", text), "utf-8")
            return
    RESULTS.write_text(f"# Real-video flow speed\n\n{BEGIN}\n{body}\n{END}\n", encoding="utf-8")


# --------------------------------------------------------------------------- run


async def run(args: argparse.Namespace) -> int:
    meta = json.loads(SOURCES.read_text(encoding="utf-8"))["videos"]
    if args.only:
        meta = [m for m in meta if m["id"] in args.only]
    OUT.mkdir(parents=True, exist_ok=True)
    sam = Sam21LocalAdapter(size=args.sam_size)
    t0 = time.perf_counter()
    sam.load()
    load_s = time.perf_counter() - t0

    synth_md = OUT / "synthetic.md"
    if not args.no_synthetic:
        synth_rows, synth_table = await known_motion(sam, OUT)
        (OUT / "synthetic.json").write_text(json.dumps(synth_rows, indent=1), encoding="utf-8")
        synth_md.write_text(f"Run on {time.strftime('%Y-%m-%d')}.\n\n{synth_table}", "utf-8")
        sys.stdout.write(json.dumps(synth_rows) + "\n")
    if args.synthetic_only:
        return 0

    muse = MuseSparkOpenAdapter.from_env(
        scene_effort=None if args.scene_effort == "default" else args.scene_effort
    )
    if not muse.api_key:
        sys.stderr.write("BASETEN_API_KEY is not set (see the repository .env)\n")
        return 2
    signer = Signer.from_env()
    config = PipelineConfig(flow_window_s=args.window, flow_fps=args.fps)
    for m in meta:
        clip = cut(m, fetch(m))
        video = load_video(clip.read_bytes())
        agent = VerifyRefineAgent(muse, sam, debug_dir=OUT / f"{m['id']}_agent")
        pipe = Pipeline(muse, sam, config=config, agent=agent, signer=signer)
        got: list[FlowResult] = []
        for attempt in range(3):  # a network outage should not end a long run
            got.clear()
            t1 = time.perf_counter()
            try:
                resp = await pipe.measure(video=video, on_flow=got.append)
                extra = 0.0
                if not got:
                    # No reference: the pipeline gives no speed. Track anyway, unscaled.
                    got.append(
                        await measure_flow(
                            video, muse, sam, None, window_s=args.window, target_fps=args.fps
                        )
                    )
                    extra = got[0].cost_usd
                break
            except ModelError as exc:
                if attempt == 2:
                    raise
                sys.stderr.write(f"{m['id']}: {exc}; trying again in 30 s\n")
                await asyncio.sleep(30)
        wall = time.perf_counter() - t1
        res = got[0] if got else None
        body = resp.model_dump()
        png = body.pop("overlay_png_b64")
        if png:
            (OUT / f"{m['id']}_overlay.png").write_bytes(base64.b64decode(png))
        (OUT / f"{m['id']}.json").write_text(json.dumps(body, indent=1), encoding="utf-8")
        gif = None
        if res is not None:
            (OUT / f"{m['id']}.flow.json").write_text(
                json.dumps(flow_json(res), indent=1), encoding="utf-8"
            )
            if m["id"] in args.gif and write_gif(clip, res, OUT / f"{m['id']}_tracks.gif"):
                gif = OUT / f"{m['id']}_tracks.gif"
        run_line = (
            f"Run on {time.strftime('%Y-%m-%d')} with {muse.scene_model_name} ({muse.model}, "
            f"reasoning effort {muse.scene_effort or 'default'}) and {sam.mask_model_name} on "
            f"{sam.device}, video predictor in {sam.video_dtype}; SAM ready in {load_s:.1f} s."
        )
        section = summarise(m, body, res, wall, extra, gif)
        head, _, rest = section.partition("\n\n")
        (OUT / f"{m['id']}.section.md").write_text(f"{head}\n\n{run_line}\n\n{rest}", "utf-8")
        e = res.estimate if res is not None else None
        row = {
            "method": body.get("method"),
            "reference": (body.get("reference") or {}).get("kind"),
            "velocity_mps": body.get("velocity_mps"),
            "velocity_sigma_mps": body.get("velocity_sigma_mps"),
            "surface_mps": None if e is None else round(e.surface_mps, 3),
            "surface_sigma_mps": None if e is None else round(e.surface_sigma_mps, 3),
            "px_per_s": None
            if res is None or res.pixel_estimate is None
            else round(res.pixel_estimate.surface_mps, 1),
            "tracks": 0 if res is None else len(res.verdicts),
            # Tracks used for the speed, or that passed every check when unscaled.
            "kept": 0
            if res is None
            else sum(v.kept or (v.reason or "").startswith("no scale (") for v in res.verdicts),
            "reason": None if res is None else res.reason,
            "wall_s": round(wall, 1),
            "cost_usd": round(float(body.get("cost_usd") or 0.0) + extra, 5),
        }
        (OUT / f"{m['id']}.row.json").write_text(json.dumps(row, indent=1), encoding="utf-8")
        sys.stdout.write(f"{m['id']}: {json.dumps(row)}\n")
        sys.stdout.flush()

    # The results file is assembled from every clip's stored section, so one clip
    # can be run again (--only) and merged with the others' last runs.
    all_ids = [m["id"] for m in json.loads(SOURCES.read_text(encoding="utf-8"))["videos"]]
    missing = [
        c
        for c in all_ids
        if not (OUT / f"{c}.section.md").exists() or not (OUT / f"{c}.row.json").exists()
    ]
    if missing or not synth_md.exists():
        sys.stderr.write(f"results file not written: no stored run for {missing or 'synthetic'}\n")
        return 0
    rows = [
        "| Clip | Reference | Surface m/s (sigma) | Depth-averaged m/s (sigma) | Range check | "
        "Tracks passing | Unscaled motion px/s | Wall s | Cost USD |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    summary = {c: json.loads((OUT / f"{c}.row.json").read_text(encoding="utf-8")) for c in all_ids}
    lo, hi = PLAUSIBLE_MPS
    for cid, r in summary.items():
        surf = (
            "none"
            if r["surface_mps"] is None
            else f"{r['surface_mps']:.2f} ({r['surface_sigma_mps']:.2f})"
        )
        mean = (
            "none"
            if r["velocity_mps"] is None
            else f"{r['velocity_mps']} ({r['velocity_sigma_mps']})"
        )
        check = (
            ""
            if r["surface_mps"] is None
            else ("inside" if lo <= r["surface_mps"] <= hi else "outside")
        )
        rows.append(
            f"| {cid} | {r['reference'] or 'none'} | {surf} | {mean} | {check} | {r['kept']} of "
            f"{r['tracks']} | {'' if r['px_per_s'] is None else r['px_per_s']} | "
            f"{r['wall_s']:.0f} | {r['cost_usd']:.4f} |"
        )
    (OUT / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    header = (
        "Generated by `uv run --extra local python ../eval/real_videos.py`, assembled on "
        f"{time.strftime('%Y-%m-%d')} from each clip's last run (each section says when and "
        f"with what it ran). Each clip is tracked for up to {args.window:.0f} s from its key "
        f"frame at about {args.fps:.0f} frames per second. Nobody measured the water speed in "
        "these videos: there is no ground truth, and nothing here is an accuracy figure."
    )
    body_md = "\n\n".join(
        [
            header,
            "### Known-motion check (synthetic, not real water)",
            "Blobs drift at a known pixel speed over a still texture; the true boxes stand in "
            "for the scene model, the scale is fixed at 0.5 cm per pixel, and the clip goes "
            "through the same distinct-object check, SAM 2.1 tracker, track checks and "
            "velocity code as a real one. In the panning case the white square is the anchor "
            "whose motion is removed. The true speed is the median over all four blobs.",
            synth_md.read_text(encoding="utf-8"),
            "### Summary",
            "\n".join(rows),
            f"Range check: the surface speed against {lo} to {hi} m/s. This is our judgement "
            "of what street flood water can do, not a sourced bound; inside says only that a "
            "number is not absurd.",
            *[(OUT / f"{c}.section.md").read_text(encoding="utf-8") for c in all_ids],
        ]
    )
    if args.write_results:
        write_results(body_md)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--only", nargs="*", help="clip ids, such as gera (merged with stored runs)")
    ap.add_argument("--sam-size", default="small", choices=["small", "base-plus", "large"])
    ap.add_argument("--scene-effort", default="low", help="low, medium, high, xhigh or default")
    ap.add_argument("--window", type=float, default=4.0, help="seconds tracked from the key frame")
    ap.add_argument("--fps", type=float, default=10.0, help="frames per second tracked")
    ap.add_argument("--gif", nargs="*", default=["gera"], help="clip ids to draw a GIF for")
    ap.add_argument("--synthetic-only", action="store_true", help="only the known-motion check")
    ap.add_argument("--no-synthetic", action="store_true", help="skip it and use its stored result")
    ap.add_argument(
        "--no-write-results",
        dest="write_results",
        action="store_false",
        help="do not rewrite eval/RESULTS_VIDEO.md",
    )
    return asyncio.run(run(ap.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
