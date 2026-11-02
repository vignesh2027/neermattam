"""Images and data for the landing page showcase and the sample photos.

    cd gateway/brand-src && ../../perception/.venv/bin/python make_landing_media.py

Set EVAL_DIR when eval/photos and eval/out live in another checkout.

Reads the evaluation photos and one recorded run from eval/ (neither is in
git: eval/real_photos.py downloads the photos and writes eval/out/). Writes:

* internal/samples/photos/<id>.jpg: the sample photos visitors can send
  through the live pipeline, at most 1600 px on the long side, with faces
  and number plates pixelated and no metadata;
* internal/web/static/samples/<id>.webp: their thumbnails;
* internal/web/static/showcase/: the showcase photo (faces pixelated as the
  service's own overlay has them), the measured overlay without its text
  label, a close-up of the measured person, and the verifier's images;
* internal/web/static/showcase/<run>-stage*.webp: the whole photo at full
  size and at 960 px for the landing hero, with the shapes of the run as
  vectors in showcase.json's "stage" (see stage() below);
* internal/web/showcase.json: the run itself (its agent steps, result and
  credits), which the landing page renders. Swap the run by changing RUN.

Faces are pixelated where the service's overlay pixelated them (found by
comparing the overlay with the photo) plus the boxes listed below; check
every output by eye before committing it.
"""
import json
import os
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageFilter

HERE = Path(__file__).resolve().parent
GW = HERE.parent
REPO = GW.parent
EVAL = Path(os.environ.get("EVAL_DIR", REPO / "eval"))
SAMPLES_OUT = GW / "internal" / "samples" / "photos"
THUMBS_OUT = GW / "internal" / "web" / "static" / "samples"
SHOW_OUT = GW / "internal" / "web" / "static" / "showcase"
SHOW_JSON = GW / "internal" / "web" / "showcase.json"

# The run shown on the landing page: c1 from the run recorded at the end of
# eval/RESULTS_REAL.md (30 October 2026), measured from a person.
RUN = "c1"
RUN_DATE = "2026-10-30"
# Verifier images for each verify step, in trace order.
# The part of the photo the hero shows, as fractions (x0, y0, x1, y1), 4:3.
CROP = (0.0, 0.22, 0.42, 0.78)
RUN_THUMBS = ["water_try0", "water_try1", "two_wheeler_1_wheel_try0", "two_wheeler_1_wheel_try1", "two_wheeler_1_wheel_try2"]

# Extra regions to pixelate, as fractions of the photo (x0, y0, x1, y1):
# number plates the overlay does not cover, and tiny faces it missed.
EXTRA = {
    "x2": [(0.452, 0.112, 0.496, 0.155), (0.588, 0.562, 0.630, 0.622)],
    "c3": [(0.465, 0.445, 0.505, 0.485)],
    "u_aby-GGLtD-A": [(0.31, 0.415, 0.35, 0.49)],
    "c1": [(0.012, 0.44, 0.056, 0.52)],
}
# Detected regions to leave out, by their top-left corner: on c1 the
# measured man's outline joins his face box into one region that covers his
# body, so his face is listed above instead; on c3 three are JPEG noise in
# the foliage and a shop sign, with no person in them.
DROP = {"c1": [(0.0, 0.417)], "c3": [(0.057, 0.301), (0.072, 0.31), (0.057, 0.651)]}


def credits():
    out = {}
    for f in ("commons_photos.json", "unsplash_photos.json"):
        for p in json.loads((EVAL / f).read_text())["photos"]:
            out[p["id"]] = p
    return out


def blur_boxes(pid):
    """Regions the service's overlay pixelated, found by comparing it with the
    photo: changed pixels that are not the blue water tint or the label."""
    ov = Image.open(EVAL / "out" / f"{pid}_overlay.png").convert("RGB")
    w, h = ov.size
    org = Image.open(EVAL / "photos" / f"{pid}.jpg").convert("RGB")
    o = np.asarray(org.resize((w, h), Image.LANCZOS)).astype(int)
    v = np.asarray(ov).astype(int)
    d = v - o
    tint = d[..., 2] - d[..., 0] > 40
    other = ((np.abs(d).sum(2) > 60) & ~tint).astype(np.uint8)
    other[:72, :] = 0  # the text label
    other = cv2.morphologyEx(other, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    n, _, st, _ = cv2.connectedComponentsWithStats(other)
    boxes = []
    for i in range(1, n):
        x, y, bw, bh, area = st[i]
        # Pixelated faces are solid blocks; JPEG noise in foliage is thin.
        solid = area / float(bw * bh)
        if bw < 12 or bh < 12 or not (area >= 1500 or (area >= 250 and solid > 0.5)):
            continue
        b = (x / w, y / h, (x + bw) / w, (y + bh) / h)
        if any(abs(b[0] - dx) < 0.01 and abs(b[1] - dy) < 0.01 for dx, dy in DROP.get(pid, [])):
            continue
        boxes.append(b)
    return boxes


def pixelate(im, boxes, grow=0.12):
    """Blur each region heavily, with a soft edge, so a face cannot be
    recognised and the photo still reads naturally."""
    im = im.copy()
    w, h = im.size
    for x0, y0, x1, y1 in boxes:
        bw, bh = x1 - x0, y1 - y0
        x0, x1 = max(0, x0 - bw * grow), min(1, x1 + bw * grow)
        y0, y1 = max(0, y0 - bh * grow), min(1, y1 + bh * grow)
        box = (int(x0 * w), int(y0 * h), int(x1 * w) + 1, int(y1 * h) + 1)
        reg = im.crop(box)
        r = max(4, min(reg.size) // 4)
        # Blur twice as wide as the region needs, then a soft-edged mask.
        blurred = reg.resize((max(1, reg.width // 4), max(1, reg.height // 4)), Image.BILINEAR)
        blurred = blurred.filter(ImageFilter.GaussianBlur(max(1, r // 4))).resize(reg.size, Image.BICUBIC)
        blurred = blurred.filter(ImageFilter.GaussianBlur(max(2, r // 3)))
        mask = Image.new("L", reg.size, 0)
        edge = max(2, min(reg.size) // 10)
        mask.paste(255, (edge, edge, reg.width - edge, reg.height - edge))
        mask = mask.filter(ImageFilter.GaussianBlur(edge / 2))
        im.paste(Image.composite(blurred, reg, mask), box[:2])
    return im


def fit(im, side):
    s = side / max(im.size)
    if s >= 1:
        return im
    return im.resize((round(im.width * s), round(im.height * s)), Image.LANCZOS)


# Evaluation photo id: the sample's public id.
SAMPLES = {"c1": "nandanam", "x2": "chennai-bike", "u_aby-GGLtD-A": "kolkata-car", "c3": "chennai-street"}


def sample(pid):
    im = Image.open(EVAL / "photos" / f"{pid}.jpg").convert("RGB")
    im = pixelate(im, blur_boxes(pid) + EXTRA.get(pid, []))
    im = fit(im, 1600)
    SAMPLES_OUT.mkdir(parents=True, exist_ok=True)
    name = SAMPLES[pid]
    im.save(SAMPLES_OUT / f"{name}.jpg", "JPEG", quality=86, optimize=True, progressive=True)
    THUMBS_OUT.mkdir(parents=True, exist_ok=True)
    fit(im, 560).save(THUMBS_OUT / f"{name}.webp", "WEBP", quality=74, method=6)
    return im.size


def showcase():
    run = json.loads((EVAL / "out" / f"{RUN}.json").read_text())
    ov = Image.open(EVAL / "out" / f"{RUN}_overlay.png").convert("RGB")
    w, h = ov.size
    org = Image.open(EVAL / "photos" / f"{RUN}.jpg").convert("RGB").resize((w, h), Image.LANCZOS)
    boxes = blur_boxes(RUN) + EXTRA.get(RUN, [])
    # The photo as it arrived, with the faces pixelated where the service's
    # overlay pixelates them.
    v = np.asarray(ov)
    o = np.asarray(pixelate(org, boxes))
    photo = Image.fromarray(o)
    # The overlay without its burned-in text label (the page writes the
    # depth itself, in the reader's language).
    m = v.copy()
    label = (np.abs(v.astype(int) - np.asarray(org).astype(int)).sum(2) > 60)
    ys, xs = np.nonzero(label[:80, :])
    if len(xs):
        x1 = min(w, xs.max() + 4)
        m[:80, :x1] = o[:80, :x1]
    measured = Image.fromarray(m)
    # Crop to the street around the measured man, so he is large enough to
    # see on the page, and upscale the crop for sharp display.
    cx0, cy0, cx1, cy1 = (round(CROP[0] * w), round(CROP[1] * h), round(CROP[2] * w), round(CROP[3] * h))
    photo = photo.crop((cx0, cy0, cx1, cy1)).resize(((cx1 - cx0) * 2, (cy1 - cy0) * 2), Image.LANCZOS)
    measured = measured.crop((cx0, cy0, cx1, cy1)).resize(((cx1 - cx0) * 2, (cy1 - cy0) * 2), Image.LANCZOS)
    SHOW_OUT.mkdir(parents=True, exist_ok=True)
    photo.save(SHOW_OUT / f"{RUN}-photo.webp", "WEBP", quality=80, method=6)
    measured.save(SHOW_OUT / f"{RUN}-measured.webp", "WEBP", quality=80, method=6)

    # The measured person: the first person geometry step names it; its
    # outline is the violet line the overlay draws. Crop around it.
    outline = (v[..., 0] > 80) & (v[..., 2] > 120) & (v[..., 1] < 90)
    ys, xs = np.nonzero(outline[:, : w // 3])
    px0, px1, py0, py1 = xs.min(), xs.max(), ys.min(), ys.max()
    pad = 0.6 * (py1 - py0)
    cx = (px0 + px1) / 2
    box = (int(max(0, cx - pad)), int(max(0, py0 - pad * 0.35)), int(min(w, cx + pad)), int(min(h, py1 + pad * 0.35)))
    loupe = Image.fromarray(m).crop(box)
    loupe = loupe.resize((loupe.width * 3, loupe.height * 3), Image.LANCZOS)
    loupe.save(SHOW_OUT / f"{RUN}-person.webp", "WEBP", quality=82, method=6)

    thumbs = []
    for name in RUN_THUMBS:
        t = Image.open(EVAL / "out" / f"{RUN}_agent" / f"{name}.jpg").convert("RGB")
        fit(t, 720).save(SHOW_OUT / f"{RUN}-{name}.webp", "WEBP", quality=76, method=6)
        thumbs.append(f"showcase/{RUN}-{name}.webp")

    steps, k = [], 0
    for s in run["agent_trace"]:
        st = {key: s[key] for key in ("step", "target", "model", "input", "output", "decision", "latency_ms") if key in s}
        if s.get("step") == "verify" and k < len(thumbs):
            st["thumb"] = thumbs[k]
            k += 1
        steps.append(st)

    c = credits()[RUN]
    est = run.get("model_estimate") or {}
    ref = run.get("reference") or {}
    data = {
        "id": RUN,
        "city": "chennai",
        "place": "Nandanam",
        "photo": {
            "title": c["title"], "author": c["author"], "licence": c["licence"],
            "licence_url": c["licence_url"], "source_url": c["page"], "taken": c.get("date", ""),
        },
        "run": {
            "date": RUN_DATE, "doc": "eval/RESULTS_REAL.md", "measurement_id": run["measurement_id"],
            "wall_s": 48.5, "cost_usd": run.get("cost_usd"),
            "models": run.get("models", {}),
        },
        "result": {
            "method": run["method"], "depth_cm": run["depth_cm"], "sigma_cm": run["depth_sigma_cm"],
            "height_cm": ref.get("height_cm"), "height_sigma_cm": ref.get("height_sigma_cm"),
            "estimate_cm": est.get("depth_cm"), "estimate_sigma_cm": est.get("depth_sigma_cm"),
            "estimate_basis": est.get("basis", ""),
        },
        "images": {
            "photo": f"showcase/{RUN}-photo.webp", "measured": f"showcase/{RUN}-measured.webp",
            "person": f"showcase/{RUN}-person.webp", "width": photo.width, "height": photo.height,
            "person_box": [round((px0 - cx0) / (cx1 - cx0), 4), round((py0 - cy0) / (cy1 - cy0), 4),
                           round((px1 - cx0) / (cx1 - cx0), 4), round((py1 - cy0) / (cy1 - cy0), 4)],
        },
        "steps": steps,
    }
    SHOW_JSON.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")


def svg_path(contour, k):
    """An OpenCV contour as an SVG path in full-photo pixels."""
    pts = contour.reshape(-1, 2) * k
    return "M" + " ".join(f"{x:.0f} {y:.0f}" for x, y in pts) + "Z"


def stage():
    """The landing page's hero stage: the whole photo at full size with faces
    pixelated, and the run's shapes as vectors in the photo's own pixels, so
    the page can draw them one by one over a sharp photo:

    * water: the verified water mask, traced from the service's overlay;
    * person: the measured man's outline, traced the same way;
    * waterline and height: the cyan and yellow lines the overlay draws.

    The boxes the agent asked SAM 2.1 for are in the trace itself, in the
    same pixels; the page reads them from there.
    """
    org = Image.open(EVAL / "photos" / f"{RUN}.jpg").convert("RGB")
    W, H = org.size
    ov = Image.open(EVAL / "out" / f"{RUN}_overlay.png").convert("RGB")
    w, h = ov.size
    k = W / w
    # Faces: the overlay's blur boxes are fractions, so they apply to the
    # full-size photo too.
    photo = pixelate(org, blur_boxes(RUN) + EXTRA.get(RUN, []))
    SHOW_OUT.mkdir(parents=True, exist_ok=True)
    photo.save(SHOW_OUT / f"{RUN}-stage.webp", "WEBP", quality=80, method=6)
    fit(photo, 960).save(SHOW_OUT / f"{RUN}-stage-960.webp", "WEBP", quality=78, method=6)

    v = np.asarray(ov).astype(int)
    o = np.asarray(org.resize((w, h), Image.LANCZOS)).astype(int)
    d = v - o
    tint = ((d[..., 2] - d[..., 0] > 40) & (np.abs(d).sum(2) > 60)).astype(np.uint8)
    cyan = (v[..., 1] > 150) & (v[..., 2] > 150) & (v[..., 0] < 100) & (np.abs(d).sum(2) > 60)
    yellow = (v[..., 0] > 200) & (v[..., 1] > 180) & (v[..., 2] < 80) & (np.abs(d).sum(2) > 60)

    # The water: the biggest tinted region, smoothed a little.
    water = cv2.morphologyEx(tint, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    water[:, : w // 8] = 0  # the measured man's outline sits at the far left
    cs, _ = cv2.findContours(water, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    big = max(cs, key=cv2.contourArea)
    water_d = svg_path(cv2.approxPolyDP(big, 1.2, True), k)

    # The man: the violet outline at the far left, filled, then traced.
    man = tint.copy()
    ys, xs = np.nonzero(yellow[:, : w // 8])
    col = int(np.median(xs))  # the height line runs down his middle
    man[:, col + 40:] = 0
    man[cyan] = 0
    cy = np.nonzero(cyan[:, : w // 4])[0]
    man[cy.min() - 1:, :] = 0  # the waterline is drawn on its own
    man = cv2.morphologyEx(man, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    # The overlay draws the outline as a thin line: fill each row between
    # its outermost pixels for the silhouette, then trace that once.
    solid = np.zeros_like(man)
    for y in np.unique(np.nonzero(man)[0]):
        xs = np.nonzero(man[y])[0]
        solid[y, xs.min():xs.max() + 1] = 1
    man = cv2.morphologyEx(solid, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    cs, _ = cv2.findContours(man, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    body = max(cs, key=cv2.contourArea)
    person_d = svg_path(cv2.approxPolyDP(body, 0.8, True), k)
    bx, by, bw, bh = cv2.boundingRect(body)

    ys, xs = np.nonzero(cyan[:, : w // 4])
    waterline = [round(xs.min() * k), round(xs.max() * k), round(np.median(ys) * k)]
    # The yellow cross: its long stroke is the height line, its short one
    # the shoulder line.
    ys, xs = np.nonzero(yellow[:, : w // 8])
    height = [round(col * k), round(ys.min() * k), round(ys.max() * k)]

    return {
        "photo": f"showcase/{RUN}-stage.webp", "photo_small": f"showcase/{RUN}-stage-960.webp",
        "width": W, "height": H,
        "water": water_d, "person": person_d,
        "person_box": [round(bx * k), round(by * k), round((bx + bw) * k), round((by + bh) * k)],
        "waterline": waterline, "height_line": height,
    }


def main():
    for pid in SAMPLES:
        print(pid, sample(pid))
    showcase()
    data = json.loads(SHOW_JSON.read_text())
    data["stage"] = stage()
    SHOW_JSON.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    print("showcase written")


if __name__ == "__main__":
    main()
