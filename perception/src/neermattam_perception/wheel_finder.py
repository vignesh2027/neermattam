"""Find wheels inside a vehicle box without relying on the scene model's points.

The scene model boxes whole vehicles well, but its points on a tyre are often
tens of pixels off. So inside the lower part of each vehicle box:

1. SAM is prompted from a regular grid of points (``grid_points``), and from
   boxes around dark, roughly elliptical blobs found classically
   (``dark_ellipse_boxes``: a dark-pixel threshold, then contours with an OpenCV
   ellipse fit), since a tyre is the darkest round thing below a car's body.
2. Every proposal is scored by :func:`score_wheel`: how well its outline fits a
   wheel (``wheel_shape``), how dark its tyre band is, and whether its size is
   plausible for a wheel of that vehicle.
3. The best two distinct proposals go to the verifier, as before.

"SAM everything, geometry decides": the scene model only has to find the
vehicle; SAM and the wheel-shape judge find the wheel.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import cv2
import numpy as np
from numpy.typing import NDArray

from .wheel_shape import ShapeScore, bbox_window, solid_wheel, wheel_shape

Box = tuple[int, int, int, int]

# Wheel width as a share of the vehicle box width: about 0.12 to 0.3 for a car
# seen from the side; up to about 0.45 for a two-wheeler; a car seen from the
# front shows narrow tyres. Outside [MIN, MAX] the size score is zero.
SIZE_MIN, SIZE_LOW, SIZE_HIGH, SIZE_MAX = 0.05, 0.1, 0.45, 0.65


def wheel_region(vehicle: Box, w: int, h: int) -> Box:
    """Where a vehicle's wheels can be: the lower 60 percent of its box, a little wider
    and a little lower (the box often stops at the waterline)."""
    x0, y0, x1, y1 = vehicle
    bw, bh = x1 - x0, y1 - y0
    return (
        max(0, round(x0 - 0.05 * bw)),
        max(0, round(y0 + 0.4 * bh)),
        min(w, round(x1 + 0.05 * bw)),
        min(h, round(y1 + 0.15 * bh)),
    )


def grow_region(region: Box, boxes: Sequence[Box], w: int, h: int, pad: float = 0.1) -> Box:
    """``region`` grown to hold each of ``boxes`` with ``pad`` of its size around it."""
    x0, y0, x1, y1 = region
    for b in boxes:
        px, py = pad * (b[2] - b[0]), pad * (b[3] - b[1])
        x0, y0 = min(x0, round(b[0] - px)), min(y0, round(b[1] - py))
        x1, y1 = max(x1, round(b[2] + px)), max(y1, round(b[3] + py))
    return max(0, x0), max(0, y0), min(w, x1), min(h, y1)


def grid_points(region: Box, nx: int = 6, ny: int = 4) -> list[tuple[int, int]]:
    x0, y0, x1, y1 = region
    xs = np.linspace(x0, x1, nx + 2)[1:-1]
    ys = np.linspace(y0, y1, ny + 2)[1:-1]
    return [(round(x), round(y)) for y in ys for x in xs]


def _value(rgb: NDArray[np.uint8]) -> NDArray[np.uint8]:
    hsv = cv2.cvtColor(np.ascontiguousarray(rgb), cv2.COLOR_RGB2HSV)
    return np.asarray(hsv[:, :, 2], dtype=np.uint8)


def dark_ellipse_boxes(
    rgb: NDArray[np.uint8], region: Box, vehicle_width: int, limit: int = 4
) -> list[Box]:
    """Boxes around dark, roughly elliptical blobs of wheel size in ``region``."""
    x0, y0, x1, y1 = region
    crop = rgb[y0:y1, x0:x1]
    if crop.size == 0:
        return []
    v = _value(crop)
    thr = min(float(np.percentile(v, 15)), 70.0)  # tyres are among the darkest pixels
    dark: NDArray[np.uint8] = (v <= thr).astype(np.uint8)
    k = max(3, round(0.01 * vehicle_width) | 1)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    dark = np.asarray(cv2.morphologyEx(dark, cv2.MORPH_OPEN, kernel), dtype=np.uint8)
    dark = np.asarray(cv2.morphologyEx(dark, cv2.MORPH_CLOSE, kernel), dtype=np.uint8)
    contours, _ = cv2.findContours(dark, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    found: list[tuple[float, Box]] = []
    for c in contours:
        if len(c) < 20:
            continue
        (cx, cy), (ma, mi), ang = cv2.fitEllipse(c)
        big, small = max(ma, mi), min(ma, mi)
        rel = big / max(vehicle_width, 1)
        if not SIZE_MIN <= rel <= SIZE_MAX or small / max(big, 1e-6) < 0.3:
            continue
        ell = np.zeros_like(dark)
        cv2.ellipse(ell, ((cx, cy), (ma, mi), ang), 1, -1)
        # A tyre with a bright rim is a dark ring or arch: compare the ellipse
        # with the blob's convex hull, which closes the ring.
        blob = np.zeros_like(dark)
        cv2.fillPoly(blob, [cv2.convexHull(c)], 1)
        inter = float(np.logical_and(ell, blob).sum())
        union = float(np.logical_or(ell, blob).sum())
        fit = inter / union if union else 0.0
        if fit < 0.5:
            continue
        pts = cv2.boxPoints(((cx, cy), (ma, mi), ang))
        bx0, by0 = pts.min(axis=0)
        bx1, by1 = pts.max(axis=0)
        pad = 0.05 * big
        found.append(
            (
                fit * min(1.0, rel / SIZE_LOW),
                (
                    max(0, round(bx0 - pad) + x0),
                    max(0, round(by0 - pad) + y0),
                    min(rgb.shape[1], round(bx1 + pad) + x0),
                    min(rgb.shape[0], round(by1 + pad) + y0),
                ),
            )
        )
    found.sort(key=lambda t: -t[0])
    return [b for _, b in found[:limit]]


@dataclass(frozen=True, slots=True)
class WheelScore:
    score: float
    shape: ShapeScore
    darkness: float
    size: float

    def text(self) -> str:
        return (
            f"wheel {self.score:.2f} = {self.shape.text()} x dark {self.darkness:.2f} "
            f"x size {self.size:.2f}"
        )


def size_score(mask: NDArray[np.bool_], vehicle_width: int) -> float:
    xs = np.nonzero(mask.any(axis=0))[0]
    if xs.size == 0 or vehicle_width <= 0:
        return 0.0
    rel = (xs.max() - xs.min() + 1) / vehicle_width
    if rel < SIZE_MIN or rel > SIZE_MAX:
        return 0.0
    if rel < SIZE_LOW:
        return float((rel - SIZE_MIN) / (SIZE_LOW - SIZE_MIN))
    if rel > SIZE_HIGH:
        return float((SIZE_MAX - rel) / (SIZE_MAX - SIZE_HIGH))
    return 1.0


def darkness_score(rgb: NDArray[np.uint8], mask: NDArray[np.bool_], region: Box) -> float:
    """How dark the outer band of the solid wheel is, against the region around it.

    A tyre is a dark ring: the share of the band's pixels darker than the
    region's 40th percentile of brightness.
    """
    win = bbox_window(mask, 2)
    if win is None:
        return 0.0
    # The band lies inside the mask: a window around it gives the same answer.
    solid, _ = solid_wheel(mask[win[0] : win[1], win[2] : win[3]])
    if not solid.any():
        return 0.0
    xs = np.nonzero(solid.any(axis=0))[0]
    width = max(3, round(0.08 * (xs.max() - xs.min() + 1)))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * width + 1, 2 * width + 1))
    inner = cv2.erode(solid.astype(np.uint8), kernel).astype(bool)
    band = solid & ~inner
    if not band.any():
        return 0.0
    x0, y0, x1, y1 = region
    v_region = _value(rgb[y0:y1, x0:x1])
    thr = float(np.percentile(v_region, 40))
    v = _value(rgb[win[0] : win[1], win[2] : win[3]])
    return float(np.mean(v[band] <= thr))


def score_wheel(
    rgb: NDArray[np.uint8], mask: NDArray[np.bool_], region: Box, vehicle_width: int
) -> WheelScore:
    shape = wheel_shape(mask)
    dark = darkness_score(rgb, mask, region)
    size = size_score(mask, vehicle_width)
    return WheelScore(round(shape.score * (0.5 + 0.5 * dark) * size, 3), shape, dark, size)


def outside_inside_ratio(rgb: NDArray[np.uint8], mask: NDArray[np.bool_]) -> float:
    """Median brightness just outside the solid wheel over just inside it.

    A tyre is the darkest ring of a wheel. An outline that follows the rim
    or the spokes, inside the tyre, has the dark rubber just outside it, so
    this ratio falls well below one; an outline on the tyre's outer edge has
    the rubber inside and the road, water or bodywork outside. The upper part
    of the outline is used, where the tyre is above the water.
    """
    full, _ = solid_wheel(mask)
    if not full.any():
        return 1.0
    fx = np.flatnonzero(full.any(axis=0))
    win = bbox_window(full, round(0.1 * (fx[-1] - fx[0] + 1)) + 4)
    assert win is not None
    rgb = rgb[win[0] : win[1], win[2] : win[3]]
    solid = full[win[0] : win[1], win[2] : win[3]]
    ys, xs = np.nonzero(solid)
    width = max(3, round(0.06 * (xs.max() - xs.min() + 1)))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * width + 1, 2 * width + 1))
    s8 = solid.astype(np.uint8)
    inner = solid & ~cv2.erode(s8, kernel).astype(bool)
    outer = cv2.dilate(s8, kernel).astype(bool) & ~solid
    top = np.zeros_like(solid)
    top[: int(np.median(ys)) + 1] = True  # the half above the wheel's middle row
    inner &= top
    outer &= top
    if inner.sum() < 10 or outer.sum() < 10:
        return 1.0
    v = _value(rgb).astype(np.float64)
    return float(np.median(v[outer]) / max(np.median(v[inner]), 1.0))


# Below this ratio the outline is taken to sit inside the tyre (on the rim).
INSIDE_TYRE_RATIO = 0.8


@dataclass(frozen=True, slots=True)
class TyreEvidence:
    """Numbers that tell a tyre's outer edge from an outline inside the tyre (rim,
    spokes) or a cut one (half a tyre closed by a chord). See :func:`tyre_evidence`."""

    dark_inside: float  # share of dark pixels in the band just inside the outline
    dark_outside: float  # ... just outside it
    tilt_deg: float  # fitted ellipse's major axis away from the nearest image axis
    axis_ratio: float
    diameter_px: float  # fitted ellipse's major axis


def tyre_evidence(rgb: NDArray[np.uint8], solid: NDArray[np.bool_]) -> TyreEvidence | None:
    """Darkness either side of the upper outline, and the fitted ellipse's tilt.

    A tyre is the darkest ring of a wheel, so an outline on its outer edge has
    dark rubber just inside and road, water or bodywork just outside; an outline
    on the rim has the dark rubber just outside (a darker annulus around it).
    "Dark" is relative to the neighbourhood: below its 35th percentile of
    brightness. The bands are 7 percent of the outline's width, above the
    outline's middle row (the part above the water).
    """
    win = bbox_window(solid, 0)
    if win is None:
        return None
    pad0 = win[3] - win[2]  # one outline width around it holds every band and window
    win = bbox_window(solid, pad0)
    assert win is not None
    rgb = rgb[win[0] : win[1], win[2] : win[3]]
    solid = solid[win[0] : win[1], win[2] : win[3]]
    ys, xs = np.nonzero(solid)
    if xs.size < 200:
        return None
    width = float(xs.max() - xs.min() + 1)
    band = max(2, round(0.07 * width))
    s8 = solid.astype(np.uint8)
    dist_in = cv2.distanceTransform(s8, cv2.DIST_L2, 3)
    dist_out = cv2.distanceTransform(1 - s8, cv2.DIST_L2, 3)
    top = np.zeros_like(solid)
    top[: int(np.median(ys)) + 1] = True
    inner = solid & (dist_in <= band) & top
    outer = ~solid & (dist_out <= band) & top
    if inner.sum() < 10 or outer.sum() < 10:
        return None
    h, w = solid.shape
    pad = round(width)
    y0, y1 = max(0, int(ys.min()) - pad), min(h, int(ys.max()) + pad)
    x0, x1 = max(0, int(xs.min()) - pad), min(w, int(xs.max()) + pad)
    v = _value(rgb).astype(np.float64)
    thr = float(np.percentile(v[y0:y1, x0:x1], 35))
    dark = v <= thr
    contours, _ = cv2.findContours(s8, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    c = max(contours, key=len)
    tilt, ratio, diam = 0.0, 1.0, width
    if len(c) >= 5:
        (_, _), (ma, mi), ang = cv2.fitEllipse(c)
        big, small = max(ma, mi), min(ma, mi)
        ratio, diam = small / max(big, 1e-6), big
        # OpenCV's angle is that of the first axis; turn it into the major axis's.
        major = ang if ma >= mi else ang + 90.0
        m = major % 90.0
        tilt = min(m, 90.0 - m)
    return TyreEvidence(
        round(float(dark[inner].mean()), 3),
        round(float(dark[outer].mean()), 3),
        round(tilt, 1),
        round(ratio, 3),
        round(diam, 1),
    )


# Wheel diameter (the fitted ellipse's major axis) as a share of the vehicle box
# width. Side-on: a 55 cm two-wheeler wheel on a 1.8 to 2.1 m machine is about
# 0.28; a 60 to 65 cm car wheel on a 3.6 to 4.5 m car about 0.15; an auto-
# rickshaw's 45 cm wheel on 2.6 m about 0.17; a bus's 1 m wheel on 10 to 12 m
# about 0.09. A turned vehicle has a narrower box and a relatively larger wheel,
# and boxes take in neighbours, so the limits are wide: they catch a hub or a
# rim fragment, not a slightly small tyre.
WHEEL_SHARE: dict[str, tuple[float, float]] = {
    "two_wheeler": (0.18, 0.7),
    "car": (0.08, 0.45),
    "auto_rickshaw": (0.09, 0.55),
    "bus": (0.04, 0.3),
}
# An ellipse tilted further than this from both image axes, and clearly oval,
# is not an upright wheel: real wheels stand on the road, and camera roll and
# oblique views tilt them less. Cut outlines (half a tyre closed by a chord
# through the wheel) are tilted ovals.
MAX_TILT_DEG = 25.0
TILT_RATIO = 0.75
# A darker ring just outside the outline than just inside: the rubber is outside.
ANNULUS_GAP = 0.2
ANNULUS_MAX_INSIDE = 0.5
ANNULUS_SOLID_OUTSIDE = 0.85  # or an almost wholly dark ring outside


def tyre_failure(
    ev: TyreEvidence | None,
    vehicle_kind: str | None = None,
    vehicle_width: int | None = None,
) -> str | None:
    """Why an outline is not a tyre's outer edge, or None when the numbers allow it."""
    if ev is None:
        return None
    if ev.dark_outside - ev.dark_inside >= ANNULUS_GAP and (
        ev.dark_inside < ANNULUS_MAX_INSIDE or ev.dark_outside >= ANNULUS_SOLID_OUTSIDE
    ):
        return (
            f"a darker ring lies just outside the outline (dark {ev.dark_outside:.2f} outside, "
            f"{ev.dark_inside:.2f} inside): it follows the rim, inside the tyre"
        )
    if ev.tilt_deg > MAX_TILT_DEG and ev.axis_ratio < TILT_RATIO:
        return (
            f"the outline is an oval tilted {ev.tilt_deg:.0f} degrees: a cut or partial "
            "outline, not an upright wheel"
        )
    if vehicle_kind in WHEEL_SHARE and vehicle_width:
        lo, hi = WHEEL_SHARE[vehicle_kind]
        share = ev.diameter_px / vehicle_width
        if not lo <= share <= hi:
            return (
                f"the outline is {100 * share:.0f}% of the vehicle's width; a "
                f"{vehicle_kind.replace('_', ' ')} wheel is {100 * lo:.0f} to {100 * hi:.0f}%"
            )
    return None


def points_outside(
    solid: NDArray[np.bool_], points: list[tuple[int, int]], tolerance: float = 0.06
) -> int:
    """How many points lie outside the outline by more than ``tolerance`` of its width.

    The verifier's points on the tyre rubber should be inside an outline of the
    tyre's outer edge; outside it, the outline is inside the tyre."""
    xs = np.nonzero(solid.any(axis=0))[0]
    if xs.size == 0:
        return 0
    limit = tolerance * float(xs.max() - xs.min() + 1)
    dist_out = cv2.distanceTransform((~solid).astype(np.uint8), cv2.DIST_L2, 3)
    h, w = solid.shape
    n = 0
    for x, y in points:
        if 0 <= x < w and 0 <= y < h and dist_out[y, x] > limit:
            n += 1
    return n
