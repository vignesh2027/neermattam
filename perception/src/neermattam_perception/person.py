"""People as a depth reference: an adult standing in the water, scaled by stature.

Indian flood photos almost always show someone wading. A person is a far worse
ruler than a wheel (people differ in height, stand at angles and move), but a
usable one when no wheel shows. The method (docs/GEOMETRY.md, section 9):

* SAM outlines the visible person; the top of the mask is the top of the head,
  and the bottom, where it meets the water, is the waterline on the body.
* The scale comes from the body itself: the shoulder line, found where the mask
  widens below the neck, sits at a known share of stature (Drillis and Contini,
  1966: shoulder 0.818 H, so the top of the head to the shoulder is 0.182 H).
  With the visible length ``L`` and the head-to-shoulder length ``s`` in pixels,
  the visible share of stature is ``0.182 L / s`` and the depth is
  ``H (1 - 0.182 L / s)``.
* ``H`` is an adult stature prior for India: NCD Risk Factor Collaboration
  (NCD-RisC), "A century of trends in adult human height", eLife 2016;5:e13410,
  country estimates for India, people born 1976 to 1996: men about 165.0 cm,
  women about 152.5 cm. Sex is unknown, so the prior is an equal mixture. The
  within-sex standard deviation (6.5 cm) is an assumption, not from that
  source, which gives means only.

A second, coarser reading comes from the body landmark the scene model names
at the waterline (knee, mid-thigh, waist, chest), with the same segment model.

Uncertainty adds, in quadrature: the stature prior, the spread of the
shoulder proportion between people, where the shoulder and the waterline are
found in the mask, and a perspective and posture term (people are rarely
straight on to the camera and rarely stand perfectly straight). Expect plus
or minus 10 to 15 cm. People who are bending, sitting, swimming, carrying
something on their head, or children, are declined.

:func:`blur_heads` blurs the head of every person found before an image leaves
the service.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

import cv2
import numpy as np
from numpy.typing import NDArray

# --------------------------------------------------------------------------- priors

# NCD-RisC (eLife 2016;5:e13410), India, birth cohorts 1976 to 1996: men 165.0 cm
# (164.9 to 165.1), women 152.5 cm (152.4 to 152.6).
STATURE_MEN_CM = 165.0
STATURE_WOMEN_CM = 152.5
# Assumed within-sex standard deviation of adult stature (not in the source).
STATURE_SD_WITHIN_CM = 6.5


def stature_prior() -> tuple[float, float]:
    """Mean and standard deviation of an equal mixture of the two sexes."""
    mean = (STATURE_MEN_CM + STATURE_WOMEN_CM) / 2.0
    var = STATURE_SD_WITHIN_CM**2 + ((STATURE_MEN_CM - STATURE_WOMEN_CM) / 2.0) ** 2
    return mean, math.sqrt(var)


# Segment heights as shares of stature, Drillis and Contini (1966), as drawn in
# D. A. Winter, Biomechanics and Motor Control of Human Movement (4th ed., 2009),
# figure 4.1. "mid_thigh" and "waist" are interpolations (between knee and hip,
# and between hip and elbow), not in the figure.
LANDMARK_SHARE: dict[str, float] = {
    "ankle": 0.039,
    "shin": 0.16,  # halfway between ankle and knee
    "knee": 0.285,
    "mid_thigh": 0.41,
    "hip": 0.530,
    "waist": 0.60,
    "chest": 0.720,
    "shoulder": 0.818,
    "neck": 0.85,
    "chin": 0.870,
}
HEAD_TO_SHOULDER = 1.0 - LANDMARK_SHARE["shoulder"]  # 0.182
# Spread of that share between adults (assumption: about 4 percent of it).
HEAD_TO_SHOULDER_SD = 0.008
# Perspective and posture: a person turned or leaning, or a camera above or below
# them, changes the vertical lengths unevenly. Taken as 5 percent of stature,
# added in quadrature (an assumption, checked only on synthetic figures).
POSTURE_REL_SD = 0.05

UPRIGHT = {"standing", "walking", "wading"}


class PersonFailure(StrEnum):
    POSTURE = "posture"
    CHILD = "child"
    MASK_EMPTY = "mask_empty"
    TOO_SMALL = "too_small"
    NO_SHOULDERS = "no_shoulders"
    MERGED = "merged"
    BOX_MISMATCH = "box_mismatch"
    NO_WATER_BELOW = "no_water_below"
    IMPLAUSIBLE = "implausible"


FAILURE_TEXT = {
    PersonFailure.POSTURE: "the person is not standing upright",
    PersonFailure.CHILD: "the person looks like a child; the stature prior is for adults",
    PersonFailure.MASK_EMPTY: "the person could not be outlined",
    PersonFailure.TOO_SMALL: "the person is too small in the photo to measure",
    PersonFailure.NO_SHOULDERS: "the shoulder line could not be found",
    PersonFailure.MERGED: "the outline joins more than one person at the head",
    PersonFailure.BOX_MISMATCH: "the outline does not match the person the scene model boxed",
    PersonFailure.NO_WATER_BELOW: "the person's outline does not end at the water",
    PersonFailure.IMPLAUSIBLE: "the measured depth is outside the plausible range",
}


@dataclass(frozen=True, slots=True)
class PersonDecline:
    reason: PersonFailure
    detail: str = ""

    @property
    def text(self) -> str:
        base = FAILURE_TEXT[self.reason]
        return f"{base} ({self.detail})" if self.detail else base


@dataclass(frozen=True, slots=True)
class PersonMeasurement:
    depth_cm: float
    depth_sigma_cm: float
    visible_share: float  # of stature
    top_y: float
    shoulder_y: float
    waterline_y: float
    x_centre: float
    height_cm: float
    height_sigma_cm: float
    landmark: str | None = None  # the scene model's landmark at the waterline
    landmark_depth_cm: float | None = None


def _row_widths(mask: NDArray[np.bool_]) -> tuple[NDArray[np.int_], int, int]:
    rows = np.nonzero(mask.any(axis=1))[0]
    top, bottom = int(rows.min()), int(rows.max())
    widths = mask[top : bottom + 1].sum(axis=1).astype(np.int_)
    return widths, top, bottom


def find_shoulder_row(mask: NDArray[np.bool_], jump: float = 1.4) -> int | None:
    """The first row below the head where the mask is ``jump`` times wider than the head.

    Going down the smoothed row widths, the head's width is the widest row seen so
    far. The head counts as complete once the width narrows (the neck) or holds
    steady, within 10 percent, for 40 percent of its width in rows (a short neck
    hidden by a collar or hair).
    The first row after that at least ``jump`` times the head's width is the
    shoulder line. Returns an image row, or None.
    """
    widths, top, _ = _row_widths(mask)
    if widths.size < 12:
        return None
    # Smooth over a few rows: masks are ragged.
    k = max(1, widths.size // 60)
    w = np.convolve(widths, np.ones(2 * k + 1) / (2 * k + 1), mode="same")
    head = float(max(w[0], w[1]))
    anchor = head  # width where the current steady run began
    steady = 0
    complete = False
    for i in range(2, w.size):
        v = float(w[i])
        if complete:
            if v >= jump * head:
                return top + i
        elif v < 0.92 * head:
            complete = True  # the neck
        elif v <= 1.1 * anchor:
            head = max(head, v)
            steady += 1
            # Steady for 40 percent of its width: the head is whole.
            complete = steady >= max(3, round(0.4 * anchor))
        else:
            head = anchor = v
            steady = 0
    return None


def shoulder_band(mask: NDArray[np.bool_], first: int) -> int:
    """Where the shoulder widening ends, below the first wide row ``first``.

    On a real outline the first row 1.4 times the head's width is on the slope
    from neck to shoulder, at or above the shoulder joint; the joint is where the
    width has nearly reached its full shoulder breadth. Looks half a head-to-
    shoulder length further down (not as far as the elbows) and returns the first
    row at 85 percent of the widest row there. Equal to ``first`` for square
    shoulders."""
    widths, top, bottom = _row_widths(mask)
    reach = max(2, (first - top) // 2)
    lo, hi = first - top, min(bottom, first + reach) - top
    window = widths[lo : hi + 1]
    if window.size == 0:
        return first
    target = 0.85 * float(window.max())
    above = np.nonzero(window >= target)[0]
    return first + int(above[0]) if above.size else first


def merged_heads(mask: NDArray[np.bool_], shoulder: int) -> bool:
    """True when, above the shoulders, many rows hold two separate pieces of
    similar size: a neighbour's head joined into this person's outline."""
    _, top, _ = _row_widths(mask)
    rows = mask[top:shoulder]
    if rows.shape[0] < 6:
        return False
    widest = 0
    split_rows = 0
    runs_per_row = []
    for r in rows:
        padded = np.concatenate(([False], r, [False])).astype(np.int8)
        d = np.diff(padded)
        starts, ends = np.nonzero(d == 1)[0], np.nonzero(d == -1)[0]
        lengths = ends - starts
        runs_per_row.append(lengths)
        if lengths.size:
            widest = max(widest, int(lengths.max()))
    for lengths in runs_per_row:
        if (lengths >= max(3, 0.4 * widest)).sum() >= 2:
            split_rows += 1
    return bool(split_rows >= 0.3 * rows.shape[0])


def fits_box(mask: NDArray[np.bool_], box: tuple[int, int, int, int]) -> bool:
    """The outline spans the scene model's box of the person above the water: at
    least 60 percent of its height, ending within 20 percent of its height of the
    box's bottom. A fragment (a head, an arm) has a bottom that is not a waterline."""
    _, top, bottom = _row_widths(mask)
    bh = max(1, box[3] - box[1])
    return (bottom - top + 1) >= 0.6 * bh and abs(bottom + 1 - box[3]) <= 0.2 * bh


def below_band(mask: NDArray[np.bool_]) -> tuple[int, int, int, int] | None:
    """The strip just below the bottom of a person's mask, as wide as the bottom
    rows: where the water must be if the bottom is the waterline. ``(left, top,
    right, bottom)``, half-open, or None at the image edge."""
    _, top, bottom = _row_widths(mask)
    h = mask.shape[0]
    y0, y1 = min(h - 1, bottom + 2), min(h, bottom + 2 + max(4, (bottom - top + 1) // 15))
    xs = np.nonzero(mask[max(top, bottom - 2) : bottom + 1].any(axis=0))[0]
    if xs.size == 0 or y0 >= y1:
        return None
    return int(xs.min()), y0, int(xs.max()) + 1, y1


def water_below(mask: NDArray[np.bool_], water: NDArray[np.bool_]) -> float:
    """Share of :func:`below_band` that is water (0 when there is no band)."""
    band = below_band(mask)
    if band is None:
        return 0.0
    x0, y0, x1, y1 = band
    return float(water[y0:y1, x0:x1].mean())


def measure_person_depth(
    mask: NDArray[np.bool_],
    water: NDArray[np.bool_] | None,
    *,
    posture: str = "standing",
    adult: bool = True,
    landmark: str | None = None,
    min_height_px: int = 60,
) -> PersonMeasurement | PersonDecline:
    """Water depth at a person standing in it, from their outline (see the module notes)."""
    if posture not in UPRIGHT:
        return PersonDecline(PersonFailure.POSTURE, posture)
    if not adult:
        return PersonDecline(PersonFailure.CHILD)
    if not mask.any():
        return PersonDecline(PersonFailure.MASK_EMPTY)
    _, top, bottom = _row_widths(mask)
    visible = bottom - top + 1
    if visible < min_height_px:
        return PersonDecline(PersonFailure.TOO_SMALL, f"{visible} px tall")
    first = find_shoulder_row(mask)
    if first is None or first - top < 4:
        return PersonDecline(PersonFailure.NO_SHOULDERS)
    if merged_heads(mask, first):
        return PersonDecline(PersonFailure.MERGED)
    # The shoulder joint lies between the first wide row and the end of the
    # widening: take the middle, and the spread of a uniform guess between them.
    full = shoulder_band(mask, first)
    shoulder = (first + full) / 2.0
    sd_band = (full - first) / math.sqrt(12.0)
    cols = np.nonzero(mask[bottom - max(2, visible // 20) : bottom + 1].any(axis=0))[0]
    x_centre = float(cols.mean()) if cols.size else float(np.nonzero(mask.any(axis=0))[0].mean())
    if water is not None:
        # The outline must end at water: look just below the bottom rows.
        share = water_below(mask, water)
        if share < 0.5:
            return PersonDecline(PersonFailure.NO_WATER_BELOW, f"{100 * share:.0f}% water")
    s_px = float(shoulder - top)
    L = float(visible)
    k = HEAD_TO_SHOULDER
    vis_share = k * L / s_px
    H, sd_H = stature_prior()
    depth = H * (1.0 - vis_share)
    if not 0.0 <= depth <= 0.9 * H:
        return PersonDecline(PersonFailure.IMPLAUSIBLE, f"{depth:.0f} cm")
    # Propagate: stature prior, shoulder share, shoulder row (+-2 px plus 3 percent
    # of s for a ragged mask, plus the shoulder band), waterline row (+-2 px plus
    # 1.5 percent of L, for ripples and the soft mask edge), and posture/perspective.
    sd_s = math.hypot(2.0 + 0.03 * s_px, sd_band)
    sd_L = 2.0 + 0.015 * L
    d_H = 1.0 - vis_share
    d_k = -H * L / s_px
    d_L = -H * k / s_px
    d_s = H * k * L / s_px**2
    var = (
        (d_H * sd_H) ** 2
        + (d_k * HEAD_TO_SHOULDER_SD) ** 2
        + (d_L * sd_L) ** 2
        + (d_s * sd_s) ** 2
        + (POSTURE_REL_SD * H) ** 2
    )
    lm_depth = None
    if landmark in LANDMARK_SHARE:
        lm_depth = LANDMARK_SHARE[landmark] * H
    return PersonMeasurement(
        depth_cm=depth,
        depth_sigma_cm=math.sqrt(var),
        visible_share=vis_share,
        top_y=float(top),
        shoulder_y=float(shoulder),
        waterline_y=float(bottom),
        x_centre=x_centre,
        height_cm=H,
        height_sigma_cm=sd_H,
        landmark=landmark if lm_depth is not None else None,
        landmark_depth_cm=lm_depth,
    )


# --------------------------------------------------------------------------- privacy


def head_region(
    mask: NDArray[np.bool_], shoulder_y: float | None = None
) -> tuple[int, int, int, int]:
    """A box around a person's head: from above the top of the mask to the
    shoulders, widened by half its width. It reaches above the mask by half the
    head's width plus, because SAM sometimes leaves the head out of a person's
    mask (a dark head against a dark background). Without a shoulder row (given or
    found), it reaches down to half the mask: a person in water to the chest shows
    little more than head and shoulders, and blurring too much is the safe side."""
    ys, xs = np.nonzero(mask)
    top, bottom = int(ys.min()), int(ys.max())
    if shoulder_y is None:
        found = find_shoulder_row(mask)
        shoulder_y = None if found is None or found - top < 4 else float(found)
    end = int(shoulder_y) if shoulder_y is not None else top + max(4, (bottom - top) // 2)
    band = mask[top : end + 1]
    cols = np.nonzero(band.any(axis=0))[0]
    if cols.size == 0:
        cols = xs
    x0, x1 = int(cols.min()), int(cols.max())
    pad = max(4, (x1 - x0) // 2)
    up = max(pad, round(0.6 * (x1 - x0)))
    h, w = mask.shape
    return max(0, x0 - pad), max(0, top - up), min(w, x1 + pad + 1), min(h, end + 1)


def blur_heads(
    rgb: NDArray[np.uint8],
    regions: Sequence[tuple[int, int, int, int]],
) -> NDArray[np.uint8]:
    """A copy of ``rgb`` with every region strongly blurred (pixelated, then smoothed),
    so faces cannot be recognised."""
    out = rgb.copy()
    h, w = out.shape[:2]
    for bx0, by0, bx1, by1 in regions:
        x0, y0 = max(0, int(bx0)), max(0, int(by0))
        x1, y1 = min(w, int(bx1)), min(h, int(by1))
        if x1 - x0 < 2 or y1 - y0 < 2:
            continue
        patch = out[y0:y1, x0:x1]
        small = cv2.resize(
            patch, (max(1, (x1 - x0) // 12), max(1, (y1 - y0) // 12)), interpolation=cv2.INTER_AREA
        )
        big = cv2.resize(small, (x1 - x0, y1 - y0), interpolation=cv2.INTER_NEAREST)
        k = max(3, ((x1 - x0) // 6) | 1)
        out[y0:y1, x0:x1] = cv2.GaussianBlur(big, (k, k), 0)
    return out


def box_head_region(box: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """Head region from a person box alone (no mask): its top half, full width (the
    box may hold only the part above water, where the head can be half of it),
    reaching above the box by a quarter of its height (boxes are not always tight)."""
    x0, y0, x1, y1 = box
    return x0, max(0, y0 - (y1 - y0) // 4), x1, y0 + max(2, (y1 - y0) // 2)


def face_box_region(box: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """A face or head box from the scene model, grown by 30 percent on each side."""
    x0, y0, x1, y1 = box
    gx, gy = max(3, round(0.3 * (x1 - x0))), max(3, round(0.3 * (y1 - y0)))
    return max(0, x0 - gx), max(0, y0 - gy), x1 + gx, y1 + gy
