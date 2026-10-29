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

from dataclasses import dataclass

import cv2
import numpy as np
from numpy.typing import NDArray

from .wheel_shape import ShapeScore, solid_wheel, wheel_shape

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
    solid, _ = solid_wheel(mask)
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
    v = _value(rgb)
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
    solid, _ = solid_wheel(mask)
    if not solid.any():
        return 1.0
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
