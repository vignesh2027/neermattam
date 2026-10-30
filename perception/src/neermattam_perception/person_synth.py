"""Synthetic people standing in water, for checking the person method (person.py).

A front-on figure is drawn with the Drillis and Contini segment proportions the
method itself assumes, so this checks the algorithm (shoulder finding, the
waterline row, the arithmetic and the sigma), not how real people differ from
the model. What varies per figure:

* stature, drawn from the same NCD-RisC mixture the method uses as its prior;
* the shoulder share of stature, by its assumed spread;
* the body turned away from the camera (widths shrink by up to 40 percent);
* the slope from neck to shoulder (it starts up to 0.03 H above the shoulder line);
* a slight stoop or lean (the upper body's vertical extent by up to 5 percent);
* the water depth, 15 to 120 cm;
* ragged mask edges (1 to 3 px).

Run: ``uv run python -m neermattam_perception.person_synth [--n 400 --seed 11]``
(also ``eval/person_synth.py``).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

import cv2
import numpy as np
from numpy.typing import NDArray

from .person import (
    HEAD_TO_SHOULDER,
    HEAD_TO_SHOULDER_SD,
    STATURE_MEN_CM,
    STATURE_SD_WITHIN_CM,
    STATURE_WOMEN_CM,
    PersonMeasurement,
    measure_person_depth,
)


@dataclass(frozen=True, slots=True)
class Figure:
    mask: NDArray[np.bool_]
    water: NDArray[np.bool_]
    depth_cm: float
    stature_cm: float


def draw_figure(rng: np.random.Generator, depth_cm: float, px_per_cm: float = 3.0) -> Figure:
    men = rng.random() < 0.5
    H_cm = rng.normal(STATURE_MEN_CM if men else STATURE_WOMEN_CM, STATURE_SD_WITHIN_CM)
    H = H_cm * px_per_cm
    k = HEAD_TO_SHOULDER + rng.normal(0, HEAD_TO_SHOULDER_SD)
    turn = rng.uniform(0.6, 1.0)  # widths shrink as the body turns
    stoop = rng.uniform(0.95, 1.0)  # upper body slightly shortened
    slope = rng.uniform(0.0, 0.03)  # neck to shoulder slope, as a share of stature
    w_img, h_img = int(0.8 * H) + 40, int(H) + 80
    cx, floor = w_img // 2, int(H) + 40
    m = np.zeros((h_img, w_img), np.uint8)

    def y(share: float) -> int:  # image row of a height share, upper body stooped
        above_hip = max(0.0, share - 0.53)
        return round(floor - (min(share, 0.53) + above_hip * stoop) * H)

    def w(share: float) -> int:
        return max(1, round(share * H * turn / 2))

    top = y(1.0)
    shoulder_share = 1.0 - k
    chin = shoulder_share + 0.05
    # head
    cv2.ellipse(
        m, (cx, round((top + y(chin)) / 2)), (w(0.09), round((y(chin) - top) / 2)), 0, 0, 360, 1, -1
    )
    # neck
    cv2.rectangle(m, (cx - w(0.05), y(chin) - 2), (cx + w(0.05), y(shoulder_share) + 2), 1, -1)
    # torso: shoulders 0.259 H wide tapering to hips 0.191 H, the shoulder line
    # reached by a sloping trapezius that starts up to 0.03 H above it
    top_of_slope = shoulder_share + slope
    pts = np.array(
        [
            (cx - w(0.06), y(top_of_slope)),
            (cx + w(0.06), y(top_of_slope)),
            (cx + w(0.259), y(shoulder_share)),
            (cx + w(0.191), y(0.53)),
            (cx - w(0.191), y(0.53)),
            (cx - w(0.259), y(shoulder_share)),
        ],
        np.int32,
    )
    cv2.fillPoly(m, [pts], 1)
    # arms hanging at the sides to the wrist (0.485 H)
    for side in (-1, 1):
        x_outer = cx + side * w(0.259)
        x_inner = cx + side * (w(0.259) - w(0.06))
        cv2.rectangle(
            m, (min(x_inner, x_outer), y(shoulder_share)), (max(x_inner, x_outer), y(0.485)), 1, -1
        )
    # legs to the floor
    for side in (-1, 1):
        x0 = cx + side * w(0.01)
        x1 = cx + side * w(0.17)
        cv2.rectangle(m, (min(x0, x1), y(0.53)), (max(x0, x1), floor), 1, -1)
    mask = m.astype(bool)
    water_row = round(floor - depth_cm * px_per_cm)
    water = np.zeros_like(mask)
    water[water_row:, :] = True
    visible = mask & ~water
    # ragged edges: perturb the boundary by eroding or dilating locally
    noise = rng.uniform(1.0, 3.0)
    blur = cv2.GaussianBlur(visible.astype(np.float32), (0, 0), noise)
    jitter = np.asarray(rng.normal(0, 0.12, blur.shape), dtype=np.float32)
    visible = (cv2.GaussianBlur(blur + jitter, (0, 0), 1.0) > 0.5) & ~water
    return Figure(visible, water & ~visible, depth_cm, H_cm)


def evaluate(n: int, seed: int) -> dict[str, float]:
    rng = np.random.default_rng(seed)
    errs, sigmas, declined = [], [], 0
    for _ in range(n):
        depth = rng.uniform(15.0, 120.0)
        f = draw_figure(rng, depth)
        m = measure_person_depth(f.mask, f.water)
        if not isinstance(m, PersonMeasurement):
            declined += 1
            continue
        errs.append(m.depth_cm - f.depth_cm)
        sigmas.append(m.depth_sigma_cm)
    e = np.array(errs)
    s = np.array(sigmas)
    return {
        "cases": float(n),
        "measured": float(e.size),
        "declined": float(declined),
        "mae_cm": float(np.mean(np.abs(e))),
        "bias_cm": float(np.mean(e)),
        "p90_abs_cm": float(np.percentile(np.abs(e), 90)),
        "median_sigma_cm": float(np.median(s)),
        "within_1_sigma": float(np.mean(np.abs(e) <= s)),
        "within_2_sigma": float(np.mean(np.abs(e) <= 2 * s)),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="synthetic check of the person method")
    ap.add_argument("--n", type=int, default=400)
    ap.add_argument("--seed", type=int, default=11)
    a = ap.parse_args(argv)
    r = evaluate(a.n, a.seed)
    sys.stdout.write(
        f"Generated by `uv run python -m neermattam_perception.person_synth --n {a.n} "
        f"--seed {a.seed}`\n\n"
        "| Measure | Value |\n|---|---|\n"
        f"| figures | {r['cases']:.0f} |\n"
        f"| measured | {r['measured']:.0f} |\n"
        f"| declined | {r['declined']:.0f} |\n"
        f"| mean absolute error | {r['mae_cm']:.1f} cm |\n"
        f"| bias | {r['bias_cm']:+.1f} cm |\n"
        f"| 90th percentile absolute error | {r['p90_abs_cm']:.1f} cm |\n"
        f"| median sigma | {r['median_sigma_cm']:.1f} cm |\n"
        f"| errors within 1 sigma (ideal 68%) | {100 * r['within_1_sigma']:.0f}% |\n"
        f"| errors within 2 sigma (ideal 95%) | {100 * r['within_2_sigma']:.0f}% |\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
