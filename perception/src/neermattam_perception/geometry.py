"""Wheel-as-ruler geometry: ellipse from a partial arc, waterline, depth with uncertainty.

The derivation lives in ``docs/GEOMETRY.md``. In short: under an affine (weak
perspective) camera a wheel, which is a circle in a vertical plane, images to an
ellipse, and the horizontal waterline chord images to a straight chord of that
ellipse. Map the ellipse to the unit circle with the affine map that sends it
there. Ratios along parallel lines survive affine maps, so the submerged share
of the vertical diameter equals the submerged share of the unit-circle diameter
perpendicular to the mapped waterline: ``f = (1 - s) / 2``, where ``s`` is the
signed distance of the mapped waterline from the centre, measured towards the
water. Depth is ``f * diameter``. Camera distance and focal length cancel.

Image coordinates throughout: x to the right, y down, pixel centres at integers.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import cv2
import numpy as np
from numpy.typing import NDArray
from scipy.optimize import least_squares

FloatArray = NDArray[np.float64]
BoolArray = NDArray[np.bool_]


class FailureReason(StrEnum):
    """Why a depth could not be measured from a wheel. Values are stable API strings."""

    WHEEL_MASK_EMPTY = "wheel_mask_empty"
    WHEEL_TOO_SMALL = "wheel_too_small"
    NO_WATER_CONTACT = "no_water_contact"
    ELLIPSE_FIT_FAILED = "ellipse_fit_failed"
    IMPLAUSIBLE_ELLIPSE = "implausible_ellipse"
    ARC_TOO_SHORT = "arc_too_short"
    WATERLINE_AMBIGUOUS = "waterline_ambiguous"
    WATERLINE_BELOW_WHEEL = "waterline_below_wheel"
    FULLY_SUBMERGED = "fully_submerged"
    AMBIGUOUS_OUTLINE = "ambiguous_outline"


FAILURE_TEXT: dict[FailureReason, str] = {
    FailureReason.WHEEL_MASK_EMPTY: "wheel mask is empty",
    FailureReason.WHEEL_TOO_SMALL: "wheel is too small in the image to measure",
    FailureReason.NO_WATER_CONTACT: "water does not meet the wheel",
    FailureReason.ELLIPSE_FIT_FAILED: "could not fit the wheel outline",
    FailureReason.IMPLAUSIBLE_ELLIPSE: "fitted wheel outline is implausible",
    FailureReason.ARC_TOO_SHORT: "too little of the wheel rim is visible above water",
    FailureReason.WATERLINE_AMBIGUOUS: "waterline on the wheel is ambiguous",
    FailureReason.WATERLINE_BELOW_WHEEL: "waterline lies below the wheel",
    FailureReason.FULLY_SUBMERGED: "wheel appears fully submerged",
    FailureReason.AMBIGUOUS_OUTLINE: "visible part of the wheel fits several wheel sizes",
}


# --------------------------------------------------------------------------- ellipse


@dataclass(frozen=True, slots=True)
class Ellipse:
    """An ellipse by centre, semi-axes and the angle of the ``a`` axis from +x (radians).

    ``a >= b`` after :meth:`normalised`; intermediate optimiser states may break it.
    """

    cx: float
    cy: float
    a: float
    b: float
    theta: float

    @property
    def axis_ratio(self) -> float:
        return min(self.a, self.b) / max(self.a, self.b)

    @property
    def centre(self) -> FloatArray:
        return np.array([self.cx, self.cy], dtype=np.float64)

    def normalised(self) -> Ellipse:
        if self.a >= self.b:
            theta = math.remainder(self.theta, math.pi)
            return Ellipse(self.cx, self.cy, self.a, self.b, theta)
        theta = math.remainder(self.theta + math.pi / 2, math.pi)
        return Ellipse(self.cx, self.cy, self.b, self.a, theta)

    def to_unit(self, pts: FloatArray) -> FloatArray:
        """Affine map sending this ellipse to the unit circle (rotate, then scale axes)."""
        d = np.asarray(pts, dtype=np.float64) - self.centre
        c, s = math.cos(self.theta), math.sin(self.theta)
        xr = d[..., 0] * c + d[..., 1] * s
        yr = -d[..., 0] * s + d[..., 1] * c
        return np.stack([xr / self.a, yr / self.b], axis=-1)

    def from_unit(self, u: FloatArray) -> FloatArray:
        u = np.asarray(u, dtype=np.float64)
        c, s = math.cos(self.theta), math.sin(self.theta)
        xr = u[..., 0] * self.a
        yr = u[..., 1] * self.b
        return np.stack([self.cx + xr * c - yr * s, self.cy + xr * s + yr * c], axis=-1)

    def unit_direction(self, v: FloatArray) -> FloatArray:
        """Linear part of :meth:`to_unit`, for direction vectors."""
        v = np.asarray(v, dtype=np.float64)
        c, s = math.cos(self.theta), math.sin(self.theta)
        xr = v[..., 0] * c + v[..., 1] * s
        yr = -v[..., 0] * s + v[..., 1] * c
        return np.stack([xr / self.a, yr / self.b], axis=-1)

    def sampson_distance(self, pts: FloatArray) -> FloatArray:
        """Signed first-order geometric distance (Sampson); positive outside."""
        d = np.asarray(pts, dtype=np.float64) - self.centre
        c, s = math.cos(self.theta), math.sin(self.theta)
        xr = d[..., 0] * c + d[..., 1] * s
        yr = -d[..., 0] * s + d[..., 1] * c
        a2, b2 = self.a * self.a, self.b * self.b
        g = xr * xr / a2 + yr * yr / b2 - 1.0
        grad = 2.0 * np.sqrt((xr / a2) ** 2 + (yr / b2) ** 2)
        return np.asarray(g / np.maximum(grad, 1e-12), dtype=np.float64)

    def sample(self, n: int = 180) -> FloatArray:
        t = np.linspace(0.0, 2.0 * math.pi, n, endpoint=False)
        return self.from_unit(np.stack([np.cos(t), np.sin(t)], axis=-1))


def conic_to_ellipse(coeffs: FloatArray) -> Ellipse | None:
    """Convert ``A x^2 + B xy + C y^2 + D x + E y + F = 0`` to an :class:`Ellipse`."""
    A, B, C, D, E, F = (float(v) for v in coeffs)
    if 4.0 * A * C - B * B <= 0.0:
        return None
    try:
        x0, y0 = np.linalg.solve(np.array([[2 * A, B], [B, 2 * C]]), np.array([-D, -E]))
    except np.linalg.LinAlgError:
        return None
    f0 = A * x0 * x0 + B * x0 * y0 + C * y0 * y0 + D * x0 + E * y0 + F
    evals, evecs = np.linalg.eigh(np.array([[A, B / 2], [B / 2, C]]))
    with np.errstate(divide="ignore", invalid="ignore"):
        sq = -f0 / evals
    if not np.all(np.isfinite(sq)) or np.any(sq <= 0):
        return None
    lengths = np.sqrt(sq)
    i = int(np.argmax(lengths))
    vec = evecs[:, i]
    return Ellipse(
        float(x0),
        float(y0),
        float(lengths[i]),
        float(lengths[1 - i]),
        math.atan2(float(vec[1]), float(vec[0])),
    ).normalised()


def fit_ellipse_direct(pts: FloatArray) -> Ellipse | None:
    """Direct least-squares ellipse fit (Fitzgibbon, Pilu and Fisher 1999).

    Uses the numerically stable partitioned form of Halir and Flusser (1998) on
    centred, scaled coordinates. The constraint ``4AC - B^2 = 1`` guarantees an
    ellipse whatever the data, which is what makes it safe inside RANSAC.
    """
    pts = np.asarray(pts, dtype=np.float64)
    if pts.shape[0] < 5:
        return None
    mean = pts.mean(axis=0)
    scale = math.sqrt(float(np.mean(np.sum((pts - mean) ** 2, axis=1))) / 2.0)
    if scale <= 1e-9:
        return None
    q = (pts - mean) / scale
    x, y = q[:, 0], q[:, 1]
    D1 = np.column_stack([x * x, x * y, y * y])
    D2 = np.column_stack([x, y, np.ones_like(x)])
    S1, S2, S3 = D1.T @ D1, D1.T @ D2, D2.T @ D2
    try:
        T = -np.linalg.solve(S3, S2.T)
    except np.linalg.LinAlgError:
        return None
    M = S1 + S2 @ T
    M = np.array([M[2] / 2.0, -M[1], M[0] / 2.0])
    try:
        _, evecs = np.linalg.eig(M)
    except np.linalg.LinAlgError:
        return None
    evecs = np.real(evecs)
    cond = 4.0 * evecs[0] * evecs[2] - evecs[1] ** 2
    ok = np.nonzero(cond > 0)[0]
    if ok.size == 0:
        return None
    a1 = evecs[:, ok[0]]
    unit = conic_to_ellipse(np.concatenate([a1, T @ a1]))
    if unit is None:
        return None
    return Ellipse(
        mean[0] + scale * unit.cx,
        mean[1] + scale * unit.cy,
        scale * unit.a,
        scale * unit.b,
        unit.theta,
    )


@dataclass(frozen=True, slots=True)
class EllipseLimits:
    """Physical sanity limits that stop a short arc from exploding into a huge ellipse."""

    min_ratio: float
    min_semi_major: float
    max_semi_major: float
    centre_box: tuple[float, float, float, float]  # x0, y0, x1, y1

    def accepts(self, e: Ellipse) -> bool:
        x0, y0, x1, y1 = self.centre_box
        return (
            e.axis_ratio >= self.min_ratio
            and self.min_semi_major <= max(e.a, e.b) <= self.max_semi_major
            and x0 <= e.cx <= x1
            and y0 <= e.cy <= y1
        )


def limits_for_points(pts: FloatArray, min_ratio: float) -> EllipseLimits:
    """Limits derived from the visible arc.

    A visible arc of at least 100 degrees spans a chord of at least 2R sin(50deg),
    about 1.53 R, so the full diameter can be at most about 1.3 times the arc's
    extent. We allow 1.6 times for noise. The centre must lie within one such
    diameter of the arc's bounding box.
    """
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    extent = float(np.max(hi - lo))
    max_a = 0.8 * extent + 2.0
    return EllipseLimits(
        min_ratio=min_ratio,
        min_semi_major=0.3 * extent,
        max_semi_major=max_a,
        centre_box=(lo[0] - max_a, lo[1] - max_a, hi[0] + max_a, hi[1] + max_a),
    )


def refine_ellipse(
    e: Ellipse, pts: FloatArray, *, tol: float, limits: EllipseLimits, max_nfev: int = 60
) -> Ellipse:
    """Geometric refinement: robust least squares on Sampson distances.

    The algebraic fit is biased towards small ellipses on partial arcs. Minimising
    the (approximate) geometric distance removes most of that bias. A soft
    penalty keeps the axis ratio above the configured minimum.
    """
    p0 = np.array([e.cx, e.cy, math.log(e.a), math.log(e.b), e.theta])
    n = pts.shape[0]
    pen_w = math.sqrt(n) * tol * 10.0

    def residuals(p: FloatArray) -> FloatArray:
        cand = Ellipse(p[0], p[1], math.exp(p[2]), math.exp(p[3]), p[4])
        r = cand.sampson_distance(pts)
        short = max(0.0, limits.min_ratio - cand.axis_ratio)
        return np.concatenate([r, [pen_w * short]])

    try:
        sol = least_squares(
            residuals, p0, loss="soft_l1", f_scale=max(tol, 0.5), max_nfev=max_nfev, method="trf"
        )
    except (ValueError, np.linalg.LinAlgError):
        return e
    if not np.all(np.isfinite(sol.x)):
        return e
    out = Ellipse(
        float(sol.x[0]), float(sol.x[1]), math.exp(sol.x[2]), math.exp(sol.x[3]), float(sol.x[4])
    ).normalised()
    return out if limits.accepts(out) else e


@dataclass(frozen=True, slots=True)
class EllipseFit:
    ellipse: Ellipse
    inliers: BoolArray
    rms_px: float


def fit_ellipse_ransac(
    pts: FloatArray,
    rng: np.random.Generator,
    *,
    tol: float,
    limits: EllipseLimits,
    iters: int = 400,
) -> EllipseFit | None:
    """RANSAC (MSAC scoring) around the direct fit, then local optimisation and refinement.

    Minimal samples are 5 points. Each candidate must pass the physical limits
    before it is scored, so a degenerate sample cannot win.
    """
    n = pts.shape[0]
    if n < 8:
        return None
    best: Ellipse | None = None
    best_cost = math.inf
    tol2 = tol * tol
    for _ in range(iters):
        idx = rng.choice(n, 5, replace=False)
        cand = fit_ellipse_direct(pts[idx])
        if cand is None or not limits.accepts(cand):
            continue
        d = cand.sampson_distance(pts)
        cost = float(np.sum(np.minimum(d * d, tol2)))
        if cost < best_cost:
            best, best_cost = cand, cost
    if best is None:
        # Fall back to a direct fit on everything; it still has to be plausible.
        best = fit_ellipse_direct(pts)
        if best is None or not limits.accepts(best):
            return None
    inl = np.abs(best.sampson_distance(pts)) < tol
    # Local optimisation: refit on inliers until the inlier set settles.
    for _ in range(3):
        if inl.sum() < 6:
            break
        lo = fit_ellipse_direct(pts[inl])
        if lo is None or not limits.accepts(lo):
            break
        new_inl = np.abs(lo.sampson_distance(pts)) < tol
        best = lo
        if np.array_equal(new_inl, inl):
            break
        inl = new_inl
    refined = refine_ellipse(best, pts[inl], tol=tol, limits=limits)
    inl = np.abs(refined.sampson_distance(pts)) < 2.0 * tol
    if inl.sum() < 6:
        return None
    rms = float(np.sqrt(np.mean(refined.sampson_distance(pts[inl]) ** 2)))
    return EllipseFit(refined, inl, rms)


def arc_coverage_deg(e: Ellipse, pts: FloatArray, bin_deg: float = 3.0) -> tuple[float, float]:
    """Visible arc and angular span of ``pts`` around ``e``, in unit-circle degrees.

    Returns ``(covered, span)``: ``covered`` sums the angular bins holding at
    least one point; ``span`` is 360 minus the largest empty gap.
    """
    u = e.to_unit(pts)
    ang = np.degrees(np.arctan2(u[:, 1], u[:, 0])) % 360.0
    nbins = round(360.0 / bin_deg)
    hist = np.bincount((ang // bin_deg).astype(int) % nbins, minlength=nbins)
    covered = float(np.count_nonzero(hist)) * bin_deg
    srt = np.sort(ang)
    gaps = np.diff(np.concatenate([srt, [srt[0] + 360.0]]))
    span = 360.0 - float(gaps.max()) if srt.size else 0.0
    return covered, span


# --------------------------------------------------------------------------- lines


@dataclass(frozen=True, slots=True)
class Line:
    """An image line through ``point`` with unit ``direction``."""

    point: FloatArray
    direction: FloatArray

    @property
    def normal(self) -> FloatArray:
        return np.array([-self.direction[1], self.direction[0]])

    def signed_distance(self, pts: FloatArray) -> FloatArray:
        return np.asarray((np.asarray(pts) - self.point) @ self.normal, dtype=np.float64)

    def y_at(self, x: float) -> float:
        dx, dy = float(self.direction[0]), float(self.direction[1])
        if abs(dx) < 1e-9:
            return float(self.point[1])
        return float(self.point[1] + (x - self.point[0]) * dy / dx)

    def shifted(self, offset: float) -> Line:
        return Line(self.point + offset * self.normal, self.direction)


def fit_line_tls(pts: FloatArray) -> Line:
    """Total least squares line (principal axis of the points)."""
    c = pts.mean(axis=0)
    _, _, vt = np.linalg.svd(pts - c, full_matrices=False)
    d = vt[0]
    if d[0] < 0:  # canonical orientation, left to right
        d = -d
    return Line(c, d / np.linalg.norm(d))


@dataclass(frozen=True, slots=True)
class LineFit:
    line: Line
    inliers: BoolArray
    rms_px: float


def fit_line_ransac(
    pts: FloatArray, rng: np.random.Generator, *, tol: float, iters: int = 300
) -> LineFit | None:
    n = pts.shape[0]
    if n < 4:
        return None
    best_cost, best_line = math.inf, None
    tol2 = tol * tol
    for _ in range(iters):
        i, j = rng.choice(n, 2, replace=False)
        d = pts[j] - pts[i]
        norm = float(np.hypot(d[0], d[1]))
        if norm < 2.0 * tol:
            continue
        line = Line(pts[i], d / norm)
        r = line.signed_distance(pts)
        cost = float(np.sum(np.minimum(r * r, tol2)))
        if cost < best_cost:
            best_cost, best_line = cost, line
    if best_line is None:
        return None
    inl = np.abs(best_line.signed_distance(pts)) < tol
    for _ in range(3):
        if inl.sum() < 3:
            return None
        line = fit_line_tls(pts[inl])
        new_inl = np.abs(line.signed_distance(pts)) < tol
        if np.array_equal(new_inl, inl):
            break
        inl = new_inl
    line = fit_line_tls(pts[inl])
    rms = float(np.sqrt(np.mean(line.signed_distance(pts[inl]) ** 2)))
    return LineFit(line, inl, rms)


# --------------------------------------------------------------------------- masks


def fill_holes(mask: NDArray[np.bool_]) -> NDArray[np.bool_]:
    """Largest connected outer component of ``mask`` with interior holes filled."""
    m = np.ascontiguousarray(mask, dtype=np.uint8)
    contours, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    out = np.zeros_like(m)
    if contours:
        biggest = max(contours, key=cv2.contourArea)
        cv2.drawContours(out, [biggest], -1, 1, thickness=cv2.FILLED)
    return out.astype(bool)


def outer_boundary(mask: NDArray[np.bool_]) -> tuple[FloatArray, FloatArray]:
    """Outer boundary points of a filled mask, moved half a pixel onto the true edge.

    OpenCV traces the centres of the outermost pixels, which sit half a pixel
    inside the object's edge. We shift each point half a pixel along its outward
    normal so the ellipse and the waterline share the same, unbiased edge.
    Returns ``(points, outward_normals)``.
    """
    m = np.ascontiguousarray(mask, dtype=np.uint8)
    contours, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return np.zeros((0, 2)), np.zeros((0, 2))
    c = max(contours, key=cv2.contourArea)[:, 0, :].astype(np.float64)
    if c.shape[0] < 3:
        return c, np.zeros_like(c)
    k = 4  # an 8 px baseline: smooth enough for pixel staircases, local enough for R > 40 px
    tangent = np.roll(c, -k, axis=0) - np.roll(c, k, axis=0)
    tn = np.hypot(tangent[:, 0], tangent[:, 1])
    tn[tn == 0] = 1.0
    tangent /= tn[:, None]
    normal = np.stack([tangent[:, 1], -tangent[:, 0]], axis=1)
    # Orient normals outwards: probe two pixels along the normal.
    h, w = m.shape
    probe = np.rint(c + 2.0 * normal).astype(int)
    px = np.clip(probe[:, 0], 0, w - 1)
    py = np.clip(probe[:, 1], 0, h - 1)
    inside = m[py, px] > 0
    if inside.mean() > 0.5:
        normal = -normal
    return c + 0.5 * normal, normal


# --------------------------------------------------------------------------- tyre outline


@dataclass(frozen=True, slots=True)
class Outline:
    """A tyre's image outline: the convex hull of its two sidewall ellipses.

    A tyre is close to a short cylinder, so seen obliquely its outline is the
    hull of the images of its two sidewall circles. Under perspective the far
    sidewall is the near one shifted by the image of the axle vector and scaled
    a little smaller. ``near`` is the sidewall whose waterline we see; ``shift``
    points from its centre to the other's and ``scale`` is the other's size
    relative to it. A thin wheel, or a square-on view, has ``shift`` near zero.
    """

    near: Ellipse
    shift: FloatArray
    scale: float
    rms_px: float
    inliers: BoolArray = field(repr=False)
    # Other fits whose robust cost is within ALT_COST_RATIO of this one. If they
    # imply different depths, the outline is ambiguous (see measure_wheel_depth).
    alternatives: tuple[Outline, ...] = field(default=(), repr=False)
    tread: float = 0.0  # the fitted t of tread_shift (tyre width over radius, signed)
    rim_scale: float | None = None  # rim size relative to the near sidewall, when fitted

    @property
    def far(self) -> Ellipse:
        return Ellipse(
            self.near.cx + float(self.shift[0]),
            self.near.cy + float(self.shift[1]),
            self.near.a * self.scale,
            self.near.b * self.scale,
            self.near.theta,
        )

    @property
    def tread_px(self) -> float:
        return float(np.hypot(self.shift[0], self.shift[1]))

    def swapped(self) -> Outline:
        far = self.far
        return Outline(
            far,
            -self.shift,
            1.0 / self.scale,
            self.rms_px,
            self.inliers,
            self.alternatives,
            -self.tread,
            self.rim_scale,
        )


def _sampson_raw(
    pts: FloatArray, cx: float, cy: float, a: float, b: float, c: float, s: float
) -> FloatArray:
    """Signed orthogonal distance from points to an ellipse (positive outside).

    Solves for the closest point's parameter ``t`` with a few vectorised Newton
    steps on ``f(t) = (a^2 - b^2) sin t cos t - x a sin t + y b cos t``, working
    in the first quadrant by symmetry. The Sampson approximation is cheaper
    but biased on partial arcs of eccentric ellipses, which showed up as a
    systematic depth underestimate in the synthetic evaluation.
    """
    dx, dy = pts[:, 0] - cx, pts[:, 1] - cy
    x = np.abs(dx * c + dy * s)
    y = np.abs(-dx * s + dy * c)
    t = np.arctan2(a * y, b * x)
    d2 = a * a - b * b
    for _ in range(4):
        st, ct = np.sin(t), np.cos(t)
        f = d2 * st * ct - x * a * st + y * b * ct
        fp = d2 * (ct * ct - st * st) - x * a * ct - y * b * st
        t = np.clip(t - f / np.where(np.abs(fp) < 1e-12, -1e-12, fp), 0.0, math.pi / 2)
    qx, qy = a * np.cos(t), b * np.sin(t)
    dist = np.hypot(x - qx, y - qy)
    outside = (x / a) ** 2 + (y / b) ** 2 > 1.0
    return np.asarray(np.where(outside, dist, -dist), dtype=np.float64)


def tread_shift(a: float, b: float, theta: float, t: float) -> FloatArray:
    """Image shift between the two sidewall centres for a tread parameter ``t``.

    Under an affine camera a circle of radius R whose normal is tilted by phi
    from the viewing axis images to an ellipse with axes R and R cos(phi), and
    its unit normal images along the minor axis with length sin(phi). The axle
    vector of a tyre of width w therefore images to ``(w / R) * a * sin(phi)``
    along the minor axis, with ``sin(phi) = sqrt(1 - (b / a)^2)``. The fit
    estimates ``t = w / R``.
    """
    big, small = (a, b) if a >= b else (b, a)
    ang = theta if a >= b else theta + math.pi / 2
    length = t * big * math.sqrt(max(0.0, 1.0 - (small / big) ** 2))
    return np.array([-math.sin(ang) * length, math.cos(ang) * length])


# Tyre width over radius: catalogue tyres span about 0.26 (80/100-18) to 0.63
# (195/55 R16), so 0.75 leaves margin.
MAX_TREAD = 0.75


def _outline_residuals(
    p: FloatArray,
    pts: FloatArray,
    min_ratio: float,
    pen_w: float,
    anchors: FloatArray | None = None,
    anchor_w: float = 0.0,
    rim_pts: FloatArray | None = None,
) -> FloatArray:
    """Signed distance from each point to the nearer sidewall ellipse, plus extras.

    The dry outline is made of an arc of the near sidewall, an arc of the far
    sidewall and two short straight runs across the tread. Every point on the
    two arcs lies on one of the ellipses, so the smaller distance is the right
    residual; the tread runs are short and the robust loss discounts them.

    Parameters: centre, log axes, angle; then, for the tyre model, the tread
    ``t`` (see :func:`tread_shift`) and the far sidewall's log scale; then, when
    ``rim_pts`` is given, the rim's log scale. Output order: outline points,
    anchors (the waterline's ends, which lie on the near sidewall, weighted by
    ``anchor_w``), rim points (the rim is concentric with the near sidewall),
    and a final axis-ratio penalty entry.
    """
    core = p[:-1] if rim_pts is not None else p
    a, b = math.exp(core[2]), math.exp(core[3])
    c, s = math.cos(core[4]), math.sin(core[4])
    r = _sampson_raw(pts, core[0], core[1], a, b, c, s)
    if core.shape[0] > 5:
        k = math.exp(core[6])
        sh = tread_shift(a, b, core[4], core[5])
        r2 = _sampson_raw(pts, core[0] + sh[0], core[1] + sh[1], k * a, k * b, c, s)
        r = np.where(np.abs(r2) < np.abs(r), r2, r)
    parts: list[FloatArray] = [r]
    if anchors is not None and anchor_w > 0:
        parts.append(anchor_w * _sampson_raw(anchors, core[0], core[1], a, b, c, s))
    if rim_pts is not None:
        rho = math.exp(p[-1])
        parts.append(_sampson_raw(rim_pts, core[0], core[1], rho * a, rho * b, c, s))
    short = max(0.0, min_ratio - min(a, b) / max(a, b))
    parts.append(np.array([pen_w * short]))
    return np.concatenate(parts)


# Far over near sidewall size: 1 / (1 + w / Z) for tyre width w at distance Z;
# w / Z stays under about 0.1 for a phone photo taken 1.5 m or more away.
_LOG_SCALE_MIN = math.log(0.9)


def _coherent_split(x: FloatArray, pts: FloatArray, max_switch_rate: float = 0.05) -> bool:
    """Whether a two-sidewall fit splits the outline into long runs, not noise.

    Taking the nearer of two ellipses always soaks up some noise, so a thin
    wheel can look like a tyre. A real tread puts long contiguous stretches of
    the (contour-ordered) points on each sidewall; a noise fit alternates. We
    require few switches between the two and at least 10 percent on each side.
    """
    a, b = math.exp(x[2]), math.exp(x[3])
    c, s = math.cos(x[4]), math.sin(x[4])
    k = math.exp(x[6])
    sh = tread_shift(a, b, x[4], x[5])
    r1 = np.abs(_sampson_raw(pts, x[0], x[1], a, b, c, s))
    r2 = np.abs(_sampson_raw(pts, x[0] + sh[0], x[1] + sh[1], k * a, k * b, c, s))
    far = r2 < r1
    share = float(far.mean())
    if not 0.1 <= share <= 0.9:
        return False
    switches = int(np.count_nonzero(far[1:] != far[:-1]))
    return bool(switches <= max(4, max_switch_rate * pts.shape[0]))


def fit_outline(
    pts: FloatArray,
    e0: Ellipse,
    *,
    tol: float,
    limits: EllipseLimits,
    tread0: tuple[float, float] | None = None,
    allow_tread: bool = True,
    max_nfev: int = 80,
    anchors: FloatArray | None = None,
    rim_pts: FloatArray | None = None,
    rim0: float = 0.6,
    min_gain: float = 0.15,
) -> Outline:
    """Fit the thin-wheel model and, if allowed, the tyre model; keep the better one.

    The tyre model is kept only when it lowers the robust cost by ``min_gain``,
    its tread is wider than the noise and it splits the outline coherently, so a
    thin wheel stays a plain ellipse. ``tread0 = (t, scale)`` fixes the model to
    the tyre form and starts it there (used by the Monte Carlo refits). Every
    fit within ``ALT_COST_RATIO`` of the best is kept in ``alternatives``.
    """
    pen_w = math.sqrt(pts.shape[0]) * tol * 10.0
    f_scale = max(tol, 0.5)
    # Each anchor counts as much as about a tenth of the outline points.
    anchor_w = math.sqrt(max(1.0, 0.1 * pts.shape[0])) if anchors is not None else 0.0
    has_rim = rim_pts is not None and rim_pts.shape[0] >= 8
    rim = rim_pts if has_rim else None
    args = (pts, limits.min_ratio, pen_w, anchors, anchor_w, rim)

    def bounds_for(n_core: int) -> tuple[FloatArray, FloatArray]:
        lo = [-np.inf] * 5 + ([-MAX_TREAD, _LOG_SCALE_MIN] if n_core > 5 else [])
        hi = [np.inf] * 5 + ([MAX_TREAD, -_LOG_SCALE_MIN] if n_core > 5 else [])
        if has_rim:
            lo.append(_LOG_RIM_MIN)
            hi.append(_LOG_RIM_MAX)
        return np.array(lo), np.array(hi)

    def solve(core0: FloatArray, nfev: int) -> tuple[FloatArray, float]:
        x0 = np.concatenate([core0, [math.log(rim0)]]) if has_rim else core0
        lo, hi = bounds_for(core0.shape[0])
        x0 = np.clip(x0, lo + 1e-9, hi - 1e-9)
        try:
            sol = least_squares(
                _outline_residuals,
                x0,
                args=args,
                loss="soft_l1",
                f_scale=f_scale,
                max_nfev=nfev,
                method="trf",
                bounds=(lo, hi),
            )
        except (ValueError, np.linalg.LinAlgError):
            return x0, math.inf
        return np.asarray(sol.x, dtype=np.float64), float(sol.cost)

    def core_of(x: FloatArray) -> FloatArray:
        return x[:-1] if has_rim else x

    base = np.array([e0.cx, e0.cy, math.log(e0.a), math.log(e0.b), e0.theta])
    candidates: list[tuple[FloatArray, float]] = []
    if tread0 is not None:
        candidates.append(solve(np.concatenate([base, [tread0[0], math.log(tread0[1])]]), max_nfev))
    else:
        candidates.append(solve(base, max_nfev))
        if allow_tread:
            # The plain fit straddles both sidewalls, so start each tyre candidate
            # with the near ellipse moved back by half the trial tread.
            for t in (-0.5, -0.25, -0.1, 0.1, 0.25, 0.5):
                sh = tread_shift(e0.a, e0.b, e0.theta, t)
                candidates.append(
                    solve(np.concatenate([base[:2] - sh / 2.0, base[2:], [t, 0.0]]), max_nfev)
                )

    def usable(x: FloatArray, cost: float) -> bool:
        if not math.isfinite(cost):
            return False
        core = core_of(x)
        e = Ellipse(core[0], core[1], math.exp(core[2]), math.exp(core[3]), core[4]).normalised()
        if not limits.accepts(e):
            return False
        if core.shape[0] == 5 or tread0 is not None:
            return True
        # A tyre model must earn its two extra parameters with a visible tread.
        sh = tread_shift(math.exp(core[2]), math.exp(core[3]), core[4], core[5])
        if float(np.hypot(sh[0], sh[1])) < 2.0 * tol:
            return False
        return _coherent_split(core, pts)

    ok = [(x, c) for x, c in candidates if usable(x, c)]
    if not ok:
        x = np.concatenate([base, [math.log(rim0)]]) if has_rim else base
        return _to_outline(x, pts, tol, limits, has_rim)
    plain = [(x, c) for x, c in ok if core_of(x).shape[0] == 5]
    tread = [(x, c) for x, c in ok if core_of(x).shape[0] > 5]
    best_x, best_cost = min(ok, key=lambda xc: xc[1])
    if core_of(best_x).shape[0] > 5 and plain and best_cost >= (1.0 - min_gain) * plain[0][1]:
        best_x, best_cost = plain[0]  # not enough gain to prefer the tyre model
    alts = tuple(
        _to_outline(x, pts, tol, limits, has_rim)
        for x, c in (plain + tread)
        if c <= ALT_COST_RATIO * best_cost and x is not best_x
    )
    out = _to_outline(best_x, pts, tol, limits, has_rim)
    return Outline(
        out.near, out.shift, out.scale, out.rms_px, out.inliers, alts, out.tread, out.rim_scale
    )


ALT_COST_RATIO = 1.15
_LOG_RIM_MIN = math.log(0.35)
_LOG_RIM_MAX = math.log(0.9)


def _to_outline(
    x: FloatArray, pts: FloatArray, tol: float, limits: EllipseLimits, has_rim: bool = False
) -> Outline:
    core = x[:-1] if has_rim else x
    a, b = math.exp(core[2]), math.exp(core[3])
    e = Ellipse(float(core[0]), float(core[1]), a, b, float(core[4])).normalised()
    if core.shape[0] > 5:
        shift = tread_shift(a, b, float(core[4]), float(core[5]))
        scale = math.exp(float(core[6]))
        t = float(core[5])
    else:
        shift, scale, t = np.zeros(2), 1.0, 0.0
    res = _outline_residuals(core, pts, limits.min_ratio, 0.0)[: pts.shape[0]]
    inl = np.abs(res) < 2.0 * tol
    rms = float(np.sqrt(np.mean(res[inl] ** 2))) if inl.any() else math.inf
    rim_scale = math.exp(float(x[-1])) if has_rim else None
    return Outline(e, shift, scale, rms, inl, (), t, rim_scale)


# --------------------------------------------------------------------------- perspective


@dataclass(frozen=True, slots=True)
class PinholeCamera:
    """Intrinsics for the optional perspective correction (square pixels, no skew)."""

    focal_px: float
    cx: float
    cy: float

    @property
    def K(self) -> FloatArray:  # noqa: N802 (the intrinsic matrix is K by convention)
        return np.array([[self.focal_px, 0.0, self.cx], [0.0, self.focal_px, self.cy], [0, 0, 1.0]])

    @staticmethod
    def from_35mm(focal_35mm: float, width: int, height: int) -> PinholeCamera:
        """From an EXIF 35 mm equivalent focal length, which matches the 43.27 mm diagonal."""
        diag = math.hypot(width, height)
        return PinholeCamera(focal_35mm * diag / 43.27, (width - 1) / 2.0, (height - 1) / 2.0)


def conic_matrix(e: Ellipse) -> FloatArray:
    c, s = math.cos(e.theta), math.sin(e.theta)
    A = c * c / e.a**2 + s * s / e.b**2
    B = 2.0 * c * s * (1.0 / e.a**2 - 1.0 / e.b**2)
    C = s * s / e.a**2 + c * c / e.b**2
    D = -2.0 * A * e.cx - B * e.cy
    E = -B * e.cx - 2.0 * C * e.cy
    F = A * e.cx**2 + B * e.cx * e.cy + C * e.cy**2 - 1.0
    return np.array([[A, B / 2, D / 2], [B / 2, C, E / 2], [D / 2, E / 2, F]])


def _apply_h(H: FloatArray, p: FloatArray) -> FloatArray:
    q = H @ np.array([p[0], p[1], 1.0])
    return np.asarray(q[:2] / q[2], dtype=np.float64)


@dataclass(frozen=True, slots=True)
class ProjectiveResult:
    fraction: float
    up_score: float  # how well the implied vertical matches the camera's up direction
    normal: FloatArray  # wheel-plane normal in camera coordinates


def projective_fraction(
    e: Ellipse, line: Line, water_n: FloatArray, cam: PinholeCamera
) -> ProjectiveResult | None:
    """Exact submerged fraction under a calibrated pinhole camera.

    The image of a circle and the intrinsics fix the circle's plane up to two
    solutions (the eigen-decomposition of the back-projected cone). For each,
    rotate the camera virtually until the plane is fronto-parallel: the ellipse
    becomes a circle and the affine formula becomes exact. The two solutions are
    told apart by gravity: a wheel stands upright, so its vertical, taken as the
    in-plane direction perpendicular to the waterline pointing away from the
    water, should point roughly up in the photo.
    """
    K = cam.K
    Kinv = np.linalg.inv(K)
    C = conic_matrix(e)
    w, v = np.linalg.eigh(K.T @ C @ K)
    if (w > 0).sum() < 2:
        w = -w
    if (w > 0).sum() != 2:
        return None
    order = np.argsort(-w)
    w, v = w[order], v[:, order]
    l1, l2, l3 = (float(x) for x in w)
    if l1 - l3 <= 0:
        return None
    g1 = math.sqrt(max(0.0, (l1 - l2) / (l1 - l3)))
    g3 = math.sqrt(max(0.0, (l2 - l3) / (l1 - l3)))
    ray = Kinv @ np.array([e.cx, e.cy, 1.0])
    best: ProjectiveResult | None = None
    for sgn in (1.0, -1.0):
        n = sgn * g1 * v[:, 0] + g3 * v[:, 2]
        n /= np.linalg.norm(n)
        if float(n @ ray) < 0:
            n = -n
        x_new = np.cross([0.0, 1.0, 0.0], n)
        if np.linalg.norm(x_new) < 1e-6:
            x_new = np.cross([1.0, 0.0, 0.0], n)
        x_new /= np.linalg.norm(x_new)
        y_new = np.cross(n, x_new)
        Rr = np.stack([x_new, y_new, n])
        H = K @ Rr @ Kinv
        Hinv = np.linalg.inv(H)
        M = Hinv.T @ C @ Hinv
        e2 = conic_to_ellipse(
            np.array([M[0, 0], 2 * M[0, 1], M[1, 1], 2 * M[0, 2], 2 * M[1, 2], M[2, 2]])
        )
        if e2 is None:
            continue
        q0 = _apply_h(H, line.point)
        q1 = _apply_h(H, line.point + 10.0 * line.direction)
        q2 = _apply_h(H, line.point + 10.0 * np.asarray(water_n))
        d2 = (q1 - q0) / np.linalg.norm(q1 - q0)
        wn2 = q2 - q0
        frac = submerged_fraction(e2, Line(q0, d2), wn2)
        up_img = -(wn2 - float(wn2 @ d2) * d2)
        up_img /= np.linalg.norm(up_img)
        up3 = Rr.T @ np.array([up_img[0], up_img[1], 0.0])
        score = float(-up3[1])
        if best is None or score > best.up_score:
            best = ProjectiveResult(frac, score, n)
    return best


def straight_runs(
    pts: FloatArray, rng: np.random.Generator, *, tol: float, min_len: float, max_runs: int = 3
) -> BoolArray:
    """Mark boundary points on long straight runs.

    A wheel's outline has no straight edges: a straight stretch longer than an
    arc could mimic (an arc of radius R stays within ``tol`` of a chord only over
    about ``sqrt(8 R tol)``) is an occluder edge, such as bodywork, a mudguard
    or the image border, and must not be fitted as wheel.
    """
    marked = np.zeros(pts.shape[0], dtype=bool)
    for _ in range(max_runs):
        free = np.nonzero(~marked)[0]
        if free.size < 8:
            break
        lf = fit_line_ransac(pts[free], rng, tol=tol, iters=200)
        if lf is None:
            break
        idx = free[lf.inliers]
        t = (pts[idx] - lf.line.point) @ lf.line.direction
        order = np.argsort(t)
        ts = t[order]
        breaks = np.nonzero(np.diff(ts) > 4.0 * tol)[0]
        starts = np.concatenate([[0], breaks + 1])
        ends = np.concatenate([breaks, [ts.size - 1]])
        lengths = ts[ends] - ts[starts]
        k = int(np.argmax(lengths))
        if lengths[k] < min_len:
            break
        lo, hi = ts[starts[k]] - 2.0 * tol, ts[ends[k]] + 2.0 * tol
        tp = (pts - lf.line.point) @ lf.line.direction
        near = np.abs(lf.line.signed_distance(pts)) < 2.0 * tol
        marked |= near & (tp >= lo) & (tp <= hi)
    return marked


# --------------------------------------------------------------------------- depth


# (visible arc in degrees, model sigma as a share of the diameter). Produced by
# `uv run python -m neermattam_perception.synth_eval --calibrate`.
MODEL_SIGMA_TABLE: tuple[tuple[float, float], ...] = (
    (115.0, 0.1326),  # calibration seed 9945, 391 usable cases; n per bin 59
    (145.0, 0.1326),  # n=88
    (175.0, 0.0766),  # n=93
    (205.0, 0.0766),  # n=74
    (240.0, 0.0175),  # n=52
    (310.0, 0.0175),  # n=25
)


# (visible arc in degrees, signed bias as a share of the diameter), from the same
# calibration run as MODEL_SIGMA_TABLE.
MODEL_BIAS_TABLE: tuple[tuple[float, float], ...] = (
    (115.0, -0.0339),  # calibration seed 9945, 391 usable cases; n per bin 59
    (145.0, -0.0339),  # n=88
    (175.0, -0.0136),  # n=93
    (205.0, -0.0113),  # n=74
    (240.0, -0.0037),  # n=52
    (310.0, -0.0033),  # n=25
)


def model_bias(arc_deg: float, table: tuple[tuple[float, float], ...]) -> float:
    """Signed systematic error (share of the diameter) at ``arc_deg``; never positive."""
    return min(0.0, model_sigma(arc_deg, table))


def model_sigma(arc_deg: float, table: tuple[tuple[float, float], ...]) -> float:
    if not table:
        return 0.0
    xs = [t[0] for t in table]
    ys = [t[1] for t in table]
    return float(np.interp(arc_deg, xs, ys))


@dataclass(frozen=True, slots=True)
class GeometryConfig:
    min_arc_deg: float = 100.0
    min_axis_ratio: float = 0.3
    min_wheel_area_px: int = 600
    contact_px: float = 2.5
    inside_margin: float = 0.03
    min_chord_points: int = 8
    ransac_iters: int = 400
    line_iters: int = 300
    mc_draws: int = 200
    max_waterline_rms_frac: float = 0.04
    tyre_model: bool = True
    max_ambiguity: float = 0.15  # largest spread in submerged fraction across near-optimal fits
    tread_min_gain: float = 0.15  # cost reduction the tyre model needs over a plain ellipse
    # Model error, as a share of the diameter, against the visible arc in degrees,
    # added in quadrature to the Monte Carlo sigma. It covers what resampling the
    # same masks cannot see: fit bias on short arcs, residual perspective, tyre
    # shape. Calibrated with `synth_eval --calibrate` on seeds disjoint from the
    # evaluation; see docs/GEOMETRY.md.
    model_sigma_table: tuple[tuple[float, float], ...] = MODEL_SIGMA_TABLE
    # Systematic error, as a signed share of the diameter, against the visible
    # arc. It is subtracted from the fraction, and it is never positive, so the
    # correction only ever raises a depth. Calibrated with the sigma table.
    model_bias_table: tuple[tuple[float, float], ...] = MODEL_BIAS_TABLE
    seed: int = 7


@dataclass(frozen=True, slots=True)
class Waterline:
    line: Line
    water_normal: FloatArray  # unit image vector from the wheel side to the water side
    rms_px: float
    n_points: int
    method: str = "sam_mask_intersection"


@dataclass(frozen=True, slots=True)
class DepthMeasurement:
    ellipse: Ellipse  # the near sidewall
    outline: Outline
    waterline: Waterline
    fraction: float
    fraction_affine: float
    fraction_sigma: float
    depth_cm: float
    depth_sigma_cm: float
    diameter_cm: float
    perspective_corrected: bool
    arc_covered_deg: float
    arc_span_deg: float
    fit_rms_px: float
    n_arc_points: int
    cm_per_px: float
    arc_points: FloatArray = field(repr=False)
    chord_points: FloatArray = field(repr=False)
    # Calibrated bias already removed from ``fraction`` (a share of the diameter,
    # never positive). ``fraction_affine`` is the raw affine value.
    fraction_bias: float = 0.0

    @property
    def waterline_y_px(self) -> float:
        """Waterline height in the image at the wheel centre's x."""
        return self.waterline.line.y_at(self.ellipse.cx)


@dataclass(frozen=True, slots=True)
class DepthFailure:
    reason: FailureReason
    detail: str = ""

    @property
    def text(self) -> str:
        base = FAILURE_TEXT[self.reason]
        return f"{base} ({self.detail})" if self.detail else base


def submerged_fraction(e: Ellipse, line: Line, water_normal: FloatArray) -> float:
    """Affine submerged share of the wheel's vertical diameter (may leave [0, 1]).

    ``water_normal`` is any image vector pointing from the dry side to the water.
    """
    p0 = e.to_unit(line.point[None, :])[0]
    du = e.unit_direction(line.direction[None, :])[0]
    du = du / np.linalg.norm(du)
    nu = np.array([-du[1], du[0]])
    wu = e.unit_direction(np.asarray(water_normal)[None, :])[0]
    if float(nu @ wu) < 0:
        nu = -nu
    s = float(nu @ p0)
    return (1.0 - s) / 2.0


def vertical_diameter(
    e: Ellipse, line: Line, water_normal: FloatArray
) -> tuple[FloatArray, FloatArray, FloatArray]:
    """Image points of the wheel's top, the waterline crossing and the bottom.

    These lie on the diameter conjugate to the waterline direction, which is the
    image of the wheel's vertical diameter under the affine model.
    """
    p0 = e.to_unit(line.point[None, :])[0]
    du = e.unit_direction(line.direction[None, :])[0]
    du = du / np.linalg.norm(du)
    nu = np.array([-du[1], du[0]])
    if float(nu @ e.unit_direction(np.asarray(water_normal)[None, :])[0]) < 0:
        nu = -nu
    s = float(nu @ p0)
    pts = e.from_unit(np.stack([-nu, s * nu, nu]))
    return pts[0], pts[1], pts[2]


def _fraction(
    e: Ellipse, line: Line, water_n: FloatArray, cam: PinholeCamera | None
) -> tuple[float, bool]:
    if cam is not None:
        pr = projective_fraction(e, line, water_n, cam)
        if pr is not None:
            return pr.fraction, True
    return submerged_fraction(e, line, water_n), False


def _line_hits(e: Ellipse, line: Line) -> tuple[float, float] | None:
    """Parameters along ``line.direction`` (px) where the line crosses ``e``."""
    u0 = e.to_unit(line.point[None, :])[0]
    du = e.unit_direction(line.direction[None, :])[0]
    A = float(du @ du)
    B = 2.0 * float(u0 @ du)
    C = float(u0 @ u0) - 1.0
    disc = B * B - 4 * A * C
    if disc <= 0:
        return None
    r = math.sqrt(disc)
    return (-B - r) / (2 * A), (-B + r) / (2 * A)


def _pick_near(outline: Outline, line: Line, chord_pts: FloatArray) -> Outline:
    """The waterline we see is cut in the near sidewall: its ends sit on that ellipse."""
    if outline.tread_px == 0.0:
        return outline
    t = (chord_pts - line.point) @ line.direction
    lo, hi = float(t.min()), float(t.max())

    def score(e: Ellipse) -> float:
        hits = _line_hits(e, line)
        if hits is None:
            return math.inf
        return abs(hits[0] - lo) + abs(hits[1] - hi)

    return outline if score(outline.near) <= score(outline.far) else outline.swapped()


def _rim_points(
    rim_mask: NDArray[np.bool_] | None,
    wheel: NDArray[np.bool_],
    dist_to_water: NDArray[Any],
    margin: float,
) -> FloatArray | None:
    """Visible rim edge points, without the stretch the waterline cuts off."""
    if rim_mask is None:
        return None
    rim = fill_holes(np.asarray(rim_mask, dtype=bool) & wheel)
    if rim.sum() < 50:
        return None
    rp, _ = outer_boundary(rim)
    h, w = rim.shape
    ip = np.rint(rp).astype(int)
    d = dist_to_water[np.clip(ip[:, 1], 0, h - 1), np.clip(ip[:, 0], 0, w - 1)]
    rp = rp[d > margin]
    return rp if rp.shape[0] >= 8 else None


def chord_ends(line: Line, chord_pts: FloatArray) -> FloatArray:
    """The two ends of the visible waterline, projected onto the fitted line."""
    t = (chord_pts - line.point) @ line.direction
    return np.stack([line.point + t.min() * line.direction, line.point + t.max() * line.direction])


def _water_side(
    line: Line, wheel: NDArray[np.bool_], water: NDArray[np.bool_], chord: FloatArray, probe: float
) -> tuple[FloatArray, float] | None:
    """Unit normal pointing to the water side and the share of water found there."""
    ys, xs = np.nonzero(wheel)
    sd = line.signed_distance(np.stack([xs, ys], axis=1).astype(np.float64))
    n = line.normal
    wheel_sign = 1.0 if float(np.mean(sd)) > 0 else -1.0
    water_n = -wheel_sign * n
    h, w = water.shape
    pr = np.rint(chord + probe * water_n).astype(int)
    ok = (pr[:, 0] >= 0) & (pr[:, 0] < w) & (pr[:, 1] >= 0) & (pr[:, 1] < h)
    if not ok.any():
        return None
    share = float(water[pr[ok, 1], pr[ok, 0]].mean())
    return water_n, share


_FACING_COS = 0.5  # within 60 degrees of facing the water


def measure_wheel_depth(
    wheel_mask: NDArray[np.bool_],
    water_mask: NDArray[np.bool_],
    diameter_cm: float,
    diameter_sigma_cm: float,
    config: GeometryConfig | None = None,
    camera: PinholeCamera | None = None,
    rim_mask: NDArray[np.bool_] | None = None,
) -> DepthMeasurement | DepthFailure:
    """Measure water depth at one wheel from its mask and the water mask.

    ``rim_mask`` (optional) is the visible rim. It sits on the near sidewall,
    concentric with it, so its arc is fitted jointly with the outline (with its
    own free scale). That pins the centre and shape when little of the tyre is
    above water, and identifies the near sidewall on a wide tyre.

    Steps:
      1. Outer boundary of the (hole-filled) wheel mask.
      2. Boundary points within ``contact_px`` of water are contact points.
      3. A first robust ellipse, fitted without the dominant straight run of
         contact points.
      4. The waterline chord is the contact points lying clearly inside that
         ellipse. Water seen behind the wheel touches the outline on the ellipse
         itself, so it is not mistaken for the waterline.
      5. Refit on the arc above the waterline: RANSAC ellipse, then the tyre
         outline model. Check the visible arc.
      6. Submerged fraction (affine, or exact under ``camera``), then Monte Carlo.
    """
    cfg = config or GeometryConfig()
    rng = np.random.default_rng(cfg.seed)
    wheel = fill_holes(np.asarray(wheel_mask, dtype=bool))
    water = np.asarray(water_mask, dtype=bool) & ~wheel
    area = int(wheel.sum())
    if area == 0:
        return DepthFailure(FailureReason.WHEEL_MASK_EMPTY)
    if area < cfg.min_wheel_area_px:
        return DepthFailure(FailureReason.WHEEL_TOO_SMALL, f"{area} px")

    pts, normals = outer_boundary(wheel)
    ys, xs = np.nonzero(wheel)
    extent = float(max(xs.max() - xs.min(), ys.max() - ys.min()) + 1)
    # Fit tolerances: SAM-like masks wobble by a pixel or two, and ripples move the
    # waterline by about as much, so the line tolerance is the looser one.
    tol = max(1.5, 0.01 * extent)
    line_tol = max(2.0, 0.015 * extent)

    dist_to_water = cv2.distanceTransform((~water).astype(np.uint8), cv2.DIST_L2, 5)
    h, w = wheel.shape
    ip = np.rint(pts).astype(int)
    ipx, ipy = np.clip(ip[:, 0], 0, w - 1), np.clip(ip[:, 1], 0, h - 1)
    contact = dist_to_water[ipy, ipx] <= cfg.contact_px
    if contact.sum() < cfg.min_chord_points:
        return DepthFailure(FailureReason.NO_WATER_CONTACT)

    # Pass 1: rough waterline and its water side.
    line1 = fit_line_ransac(pts[contact], rng, tol=line_tol, iters=cfg.line_iters)
    if line1 is None:
        return DepthFailure(FailureReason.WATERLINE_AMBIGUOUS, "no straight waterline")
    side1 = _water_side(line1.line, wheel, water, pts[contact][line1.inliers], cfg.contact_px + 2.0)
    if side1 is None or side1[1] < 0.5:
        return DepthFailure(FailureReason.WATERLINE_AMBIGUOUS, "no water beyond the waterline")
    # A boundary point that touches water and faces it lies on the waterline (or
    # a ripple of it). One that touches water but faces sideways or up is the
    # wheel's true edge with water seen behind it, and stays in the fit.
    facing = contact & (normals @ side1[0] > _FACING_COS)
    keep1 = ~facing & ~(contact & (np.abs(line1.line.signed_distance(pts)) < 2.0 * line_tol))
    # Points on the image border or on long straight occluder edges are not wheel.
    on_border = (
        (pts[:, 0] < 1.0) | (pts[:, 1] < 1.0) | (pts[:, 0] > w - 2.0) | (pts[:, 1] > h - 2.0)
    )
    occluded = on_border.copy()
    dry = np.nonzero(~contact & ~on_border)[0]
    if dry.size >= 8:
        occluded[dry] |= straight_runs(pts[dry], rng, tol=tol, min_len=0.3 * extent)
    keep1 &= ~occluded
    if keep1.sum() < 8:
        return DepthFailure(FailureReason.ARC_TOO_SHORT, "no arc left above the waterline")
    limits = limits_for_points(pts[keep1], cfg.min_axis_ratio)
    fit1 = fit_ellipse_ransac(pts[keep1], rng, tol=tol, limits=limits, iters=cfg.ransac_iters)
    if fit1 is None:
        return DepthFailure(FailureReason.ELLIPSE_FIT_FAILED)
    out1 = fit_outline(
        pts[keep1],
        fit1.ellipse,
        tol=tol,
        limits=limits,
        allow_tread=cfg.tyre_model,
        min_gain=cfg.tread_min_gain,
    )
    covered1, _ = arc_coverage_deg(out1.near, pts[keep1])
    if covered1 < cfg.min_arc_deg:
        return DepthFailure(FailureReason.ARC_TOO_SHORT, f"{covered1:.0f} degrees visible")
    # A narrow ellipse is nearly straight at the ends of its minor axis, so a
    # straight run that lies on the fitted outline is wheel after all.
    on_outline = (
        np.minimum(np.abs(out1.near.sampson_distance(pts)), np.abs(out1.far.sampson_distance(pts)))
        < 2.0 * tol
    )
    occluded &= on_border | ~on_outline

    # Pass 2: the chord is the contact run cutting through the outline. (Points
    # facing the water were kept out of the first fit, so they cannot shape the
    # outline they are tested against.)
    r1 = np.minimum(
        np.linalg.norm(out1.near.to_unit(pts), axis=1),
        np.linalg.norm(out1.far.to_unit(pts), axis=1),
    )
    chord_mask = contact & (r1 < 1.0 - cfg.inside_margin)
    if chord_mask.sum() < cfg.min_chord_points:
        if (r1[contact] > 1.0 - cfg.inside_margin).all():
            return DepthFailure(FailureReason.NO_WATER_CONTACT, "water only touches the outline")
        return DepthFailure(FailureReason.WATERLINE_AMBIGUOUS, "too few waterline points")
    lfit = fit_line_ransac(pts[chord_mask], rng, tol=line_tol, iters=cfg.line_iters)
    if lfit is None or lfit.inliers.sum() < cfg.min_chord_points:
        return DepthFailure(FailureReason.WATERLINE_AMBIGUOUS, "no straight waterline")
    line = lfit.line
    chord_pts = pts[chord_mask][lfit.inliers]

    side = _water_side(line, wheel, water, chord_pts, cfg.contact_px + 2.0)
    if side is None or side[1] < 0.5:
        return DepthFailure(FailureReason.WATERLINE_AMBIGUOUS, "no water beyond the waterline")
    water_n = side[0]
    facing = contact & (normals @ water_n > _FACING_COS)

    # Arc: everything clearly on the dry side of the waterline.
    sd_w = (pts - line.point) @ water_n
    arc_sel = (sd_w < -2.0 * line_tol) & ~chord_mask & ~occluded & ~facing
    if arc_sel.sum() < 8:
        return DepthFailure(FailureReason.ARC_TOO_SHORT, "no arc left above the waterline")
    limits = limits_for_points(pts[arc_sel], cfg.min_axis_ratio)
    fit2 = fit_ellipse_ransac(pts[arc_sel], rng, tol=tol, limits=limits, iters=cfg.ransac_iters)
    if fit2 is None:
        return DepthFailure(FailureReason.ELLIPSE_FIT_FAILED)
    a_pts = pts[arc_sel]
    anchors = chord_ends(line, chord_pts)
    rim_pts = _rim_points(rim_mask, wheel, dist_to_water, cfg.contact_px + 1.0)
    outline = fit_outline(
        a_pts,
        fit2.ellipse,
        tol=tol,
        limits=limits,
        allow_tread=cfg.tyre_model,
        anchors=anchors,
        rim_pts=rim_pts,
        min_gain=cfg.tread_min_gain,
    )
    if outline.rim_scale is None:
        outline = _pick_near(outline, line, chord_pts)
    e = outline.near
    arc_pts = a_pts[outline.inliers]
    if arc_pts.shape[0] < 8:
        return DepthFailure(FailureReason.ELLIPSE_FIT_FAILED, "too few points on the outline")
    covered, span = arc_coverage_deg(e, arc_pts)
    if covered < cfg.min_arc_deg:
        return DepthFailure(FailureReason.ARC_TOO_SHORT, f"{covered:.0f} degrees visible")

    wheel_pts = np.stack([xs, ys], axis=1).astype(np.float64)
    grow = (1.0 + 2.0 * tol / e.b) ** 2
    inside = (np.sum(e.to_unit(wheel_pts) ** 2, axis=1) <= grow) | (
        np.sum(outline.far.to_unit(wheel_pts) ** 2, axis=1) <= grow
    )
    # Points between the two sidewall ellipses belong to the tread band.
    if outline.tread_px > 0:
        for t in np.linspace(0.25, 0.75, 3):
            k = 1.0 + t * (outline.scale - 1.0)
            mid = Ellipse(
                e.cx + t * outline.shift[0], e.cy + t * outline.shift[1], k * e.a, k * e.b, e.theta
            )
            inside |= np.sum(mid.to_unit(wheel_pts) ** 2, axis=1) <= grow
    if float(inside.mean()) < 0.9:
        return DepthFailure(FailureReason.IMPLAUSIBLE_ELLIPSE, "wheel mask spills outside the fit")
    if lfit.rms_px > cfg.max_waterline_rms_frac * 2.0 * e.b + 1.0:
        return DepthFailure(FailureReason.WATERLINE_AMBIGUOUS, "waterline points are scattered")

    frac_affine = submerged_fraction(e, line, water_n)
    frac, corrected = _fraction(e, line, water_n, camera)
    # Model ambiguity: other outlines that fit nearly as well but imply another depth.
    alt_fracs = [frac]
    for alt in outline.alternatives:
        an = _pick_near(alt, line, chord_pts).near
        if np.all(np.isfinite([an.cx, an.cy, an.a, an.b])):
            alt_fracs.append(_fraction(an, line, water_n, camera)[0])
    spread = float(max(alt_fracs) - min(alt_fracs))
    if spread > cfg.max_ambiguity:
        return DepthFailure(
            FailureReason.AMBIGUOUS_OUTLINE,
            f"depth could be {min(alt_fracs) * diameter_cm:.0f} "
            f"to {max(alt_fracs) * diameter_cm:.0f} cm",
        )
    if frac <= 0.0:
        return DepthFailure(FailureReason.WATERLINE_BELOW_WHEEL)
    if frac >= 1.0:
        return DepthFailure(FailureReason.FULLY_SUBMERGED)

    fr_sigma, d_sigma = _monte_carlo(
        outline,
        arc_pts,
        chord_pts,
        water_n,
        lfit.rms_px,
        tol,
        limits,
        diameter_cm,
        diameter_sigma_cm,
        cfg.mc_draws,
        rng,
        camera if corrected else None,
        rim_pts if outline.rim_scale is not None else None,
    )
    if spread > 0:
        # Treat the spread of near-optimal fits as a uniform range.
        amb = spread / math.sqrt(12.0)
        fr_sigma = math.hypot(fr_sigma, amb)
        d_sigma = math.hypot(d_sigma, amb * diameter_cm)
    if cfg.model_sigma_table:
        rel = model_sigma(covered, cfg.model_sigma_table)
        d_sigma = math.hypot(d_sigma, rel * diameter_cm)
        fr_sigma = math.hypot(fr_sigma, rel)
    # Short arcs read low (see docs/GEOMETRY.md, section 4); remove the calibrated
    # bias. It is never positive, so this can only raise the depth.
    bias = model_bias(covered, cfg.model_bias_table)
    frac = min(1.0, frac - bias)
    return DepthMeasurement(
        ellipse=e,
        outline=outline,
        waterline=Waterline(line, water_n, lfit.rms_px, int(chord_pts.shape[0])),
        fraction=frac,
        fraction_affine=frac_affine,
        fraction_sigma=fr_sigma,
        depth_cm=frac * diameter_cm,
        depth_sigma_cm=d_sigma,
        diameter_cm=diameter_cm,
        perspective_corrected=corrected,
        arc_covered_deg=covered,
        arc_span_deg=span,
        fit_rms_px=outline.rms_px,
        n_arc_points=int(arc_pts.shape[0]),
        cm_per_px=diameter_cm / (2.0 * e.a),
        arc_points=arc_pts,
        chord_points=chord_pts,
        fraction_bias=bias,
    )


def _monte_carlo(
    outline: Outline,
    arc_pts: FloatArray,
    chord_pts: FloatArray,
    water_n: FloatArray,
    line_rms: float,
    tol: float,
    limits: EllipseLimits,
    diameter_cm: float,
    diameter_sigma_cm: float,
    draws: int,
    rng: np.random.Generator,
    camera: PinholeCamera | None,
    rim_pts: FloatArray | None = None,
) -> tuple[float, float]:
    """Propagate boundary noise, waterline noise and diameter uncertainty.

    Each draw bootstraps the outline points (with pixel-quantisation jitter) and
    refits the outline; bootstraps the waterline points and refits the line;
    shifts the line by a level error the size of the observed ripple (ripples are
    spatially correlated, so they do not average out along the chord); and draws
    a diameter. Returns the standard deviations of the fraction and the depth.
    """
    base_line = fit_line_tls(chord_pts)
    if draws <= 1:
        f0, _ = _fraction(outline.near, base_line, water_n, camera)
        return 0.0, diameter_sigma_cm * f0
    quant = 1.0 / math.sqrt(12.0)
    level_sigma = max(line_rms, quant)
    fr = np.empty(draws)
    dep = np.empty(draws)
    na, nc = arc_pts.shape[0], chord_pts.shape[0]
    tread0 = (outline.tread, outline.scale) if outline.tread_px > 0 else None
    for k in range(draws):
        ia = rng.integers(0, na, na)
        ap = arc_pts[ia] + rng.normal(0.0, quant, (na, 2))
        cp = chord_pts[rng.integers(0, nc, nc)] + rng.normal(0.0, quant, (nc, 2))
        lk = fit_line_tls(cp).shifted(float(rng.normal(0.0, level_sigma)))
        rk = None
        if rim_pts is not None:
            rk = rim_pts[rng.integers(0, rim_pts.shape[0], rim_pts.shape[0])]
        ok = fit_outline(
            ap,
            outline.near,
            tol=tol,
            limits=limits,
            tread0=tread0,
            allow_tread=False,
            max_nfev=15,
            anchors=chord_ends(lk, cp),
            rim_pts=rk,
            rim0=outline.rim_scale or 0.6,
        )
        f, _ = _fraction(ok.near, lk, water_n, camera)
        dk = diameter_cm + (rng.normal(0.0, diameter_sigma_cm) if diameter_sigma_cm > 0 else 0.0)
        fr[k] = f
        dep[k] = f * dk
    return float(np.std(fr, ddof=1)), float(np.std(dep, ddof=1))


def depth_from_line(
    m: DepthMeasurement,
    line: Line,
    camera: PinholeCamera | None = None,
    *,
    apply_bias: bool = True,
) -> float:
    """Depth (cm) implied by a second, independent waterline on the same fitted wheel.

    With ``apply_bias`` the same calibrated bias as ``m.depth_cm`` is removed, so
    the two depths are directly comparable.
    """
    frac, _ = _fraction(m.ellipse, line, m.waterline.water_normal, camera)
    if apply_bias:
        frac = min(1.0, frac - m.fraction_bias)
    return frac * m.diameter_cm
