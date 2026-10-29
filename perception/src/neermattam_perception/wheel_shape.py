"""A cheap judge of whether a mask looks like a wheel standing in water.

The visible part of a wheel above the water is an ellipse with its lower part
cut off by a straight chord (the waterline). A good wheel mask therefore has an
outer outline that is, apart from straight runs (the waterline, bodywork edges,
the photo border), close to one ellipse over a long arc, with little of the
mask outside that ellipse. Masks that take in an exhaust, a fork or a mudguard
bulge out of the ellipse; masks of a reflection or a patch of water do not
follow an ellipse at all.

:func:`solid_wheel` turns the masks SAM gives for a tyre into the solid shape the
geometry expects. With ring prompting (points on the rubber, negative points on
the hub) SAM often returns only the tyre: an arch open at the waterline. The
visible wheel is convex (an ellipse cut by a line), so the convex hull of the
arch is the wheel above water, rim included.

:func:`wheel_shape` scores a mask from 0 to 1 with the same robust ellipse fit
the geometry uses. It is a ranking score, not a probability.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np
from numpy.typing import NDArray

from .geometry import (
    arc_coverage_deg,
    fill_holes,
    fit_ellipse_ransac,
    limits_for_points,
    outer_boundary,
    straight_runs,
)

# Below this solidity (mask area over convex hull area) a mask is treated as a
# tyre ring or arch and replaced by its convex hull.
RING_SOLIDITY = 0.85


def solid_wheel(mask: NDArray[np.bool_]) -> tuple[NDArray[np.bool_], bool]:
    """The mask as a solid wheel shape, and whether the convex hull was used."""
    filled = fill_holes(mask)
    if not filled.any():
        return filled, False
    contours, _ = cv2.findContours(
        np.ascontiguousarray(filled, dtype=np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE
    )
    hull = cv2.convexHull(np.vstack(contours))
    hull_area = float(cv2.contourArea(hull))
    if hull_area <= 0 or filled.sum() / hull_area >= RING_SOLIDITY:
        return filled, False
    out = np.zeros(filled.shape, dtype=np.uint8)
    cv2.fillPoly(out, [hull], 1)
    return out.astype(bool), True


@dataclass(frozen=True, slots=True)
class ShapeScore:
    score: float
    inlier_share: float  # share of the curved outline within tolerance of the ellipse
    arc_deg: float  # visible arc of the fitted ellipse covered by inliers
    axis_ratio: float
    spill: float  # share of the solid mask outside the fitted ellipse
    hull: bool  # the mask was a ring or arch and was closed with its convex hull
    note: str = ""

    def text(self) -> str:
        if self.note:
            return self.note
        return (
            f"shape {self.score:.2f} (fit {self.inlier_share:.2f}, arc {self.arc_deg:.0f} deg, "
            f"ratio {self.axis_ratio:.2f}, spill {self.spill:.2f}"
            + (", closed ring" if self.hull else "")
            + ")"
        )


def wheel_shape(mask: NDArray[np.bool_], seed: int = 3) -> ShapeScore:
    rng = np.random.default_rng(seed)
    solid, hull = solid_wheel(mask)
    area = int(solid.sum())
    if area < 200:
        return ShapeScore(0.0, 0.0, 0.0, 0.0, 1.0, hull, "mask too small")
    pts, _ = outer_boundary(solid)
    if pts.shape[0] < 20:
        return ShapeScore(0.0, 0.0, 0.0, 0.0, 1.0, hull, "outline too short")
    ys, xs = np.nonzero(solid)
    extent = float(max(xs.max() - xs.min(), ys.max() - ys.min()) + 1)
    tol = max(1.5, 0.01 * extent)  # as in the geometry
    h, w = solid.shape
    border = (pts[:, 0] < 1) | (pts[:, 1] < 1) | (pts[:, 0] > w - 2) | (pts[:, 1] > h - 2)
    straight = straight_runs(pts, rng, tol=tol, min_len=0.3 * extent)
    curved = pts[~straight & ~border]
    if curved.shape[0] < 12:
        return ShapeScore(0.0, 0.0, 0.0, 0.0, 1.0, hull, "no curved outline")
    limits = limits_for_points(curved, 0.3)
    fit = fit_ellipse_ransac(curved, rng, tol=tol, limits=limits, iters=200)
    if fit is None:
        return ShapeScore(0.0, 0.0, 0.0, 0.0, 1.0, hull, "no ellipse fits the outline")
    e = fit.ellipse
    inl = np.abs(e.sampson_distance(curved)) < 2.0 * tol
    share = float(inl.mean())
    covered, _ = arc_coverage_deg(e, curved[inl]) if inl.sum() >= 3 else (0.0, 0.0)
    grow = (1.0 + 2.0 * tol / max(e.b, 1e-6)) ** 2
    inside = np.sum(e.to_unit(np.stack([xs, ys], axis=1).astype(np.float64)) ** 2, axis=1) <= grow
    spill = 1.0 - float(inside.mean())
    ratio = e.axis_ratio
    score = (
        share**2
        * min(1.0, covered / 180.0)
        * max(0.0, 1.0 - spill / 0.2)
        * (1.0 if ratio >= 0.3 else 0.0)
    )
    return ShapeScore(
        round(score, 3), round(share, 3), round(covered, 1), round(ratio, 3), round(spill, 3), hull
    )


def mask_iou(a: NDArray[np.bool_], b: NDArray[np.bool_]) -> float:
    inter = float(np.logical_and(a, b).sum())
    union = float(np.logical_or(a, b).sum())
    return inter / union if union > 0 else 0.0


def dedupe(masks: list[NDArray[np.bool_]], threshold: float = 0.95) -> list[int]:
    """Indices of masks to keep, dropping near duplicates of earlier ones."""
    keep: list[int] = []
    for i, m in enumerate(masks):
        if all(mask_iou(m, masks[j]) < threshold for j in keep):
            keep.append(i)
    return keep
