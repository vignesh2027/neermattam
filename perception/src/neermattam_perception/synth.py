"""Synthetic flooded-wheel scenes with exact ground truth.

The wheel is a real 3D object seen through a pinhole camera, not a 2D ellipse,
so the evaluation includes the perspective error that the affine depth model
ignores. World frame: metres, Y up, ground at Y = 0, the wheel in the plane
Z = 0 with its axle along Z, the water a horizontal plane at Y = depth.

Each case can include, independently:
  * tyre width (the outline becomes the hull of the two sidewall circles),
  * water visible behind the wheel, touching its outline above the waterline,
  * a body panel or mudguard hiding the top of the wheel,
  * ripples on the waterline and wobbly, noisy mask edges (SAM-like).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np
from numpy.typing import NDArray

from .geometry import Ellipse, fit_ellipse_direct

U8 = NDArray[np.uint8]
Mask = NDArray[np.bool_]
F64 = NDArray[np.float64]


@dataclass(frozen=True, slots=True)
class SynthParams:
    diameter_cm: float = 43.4
    depth_fraction: float = 0.4
    yaw_deg: float = 20.0
    pitch_deg: float = 12.0
    roll_deg: float = 0.0
    wheel_px: float = 260.0  # approximate image diameter of the wheel
    focal_scale: float = 1.1  # focal length in units of image width
    offset_x: float = 0.0  # wheel centre offset from the image centre, as a share of width
    offset_y: float = 0.0
    tyre_width_frac: float = 0.15  # tyre width as a share of the diameter
    rim_frac: float = 0.6  # rim diameter as a share of the wheel diameter
    water_behind: bool = False
    wall_behind_m: float = 2.0
    top_occlusion: float | None = None  # body edge height above the centre, in radii
    ripple_px: float = 0.0
    ripple_wavelength_px: float = 40.0
    mask_noise_px: float = 0.5
    width: int = 960
    height: int = 720


@dataclass(frozen=True, slots=True)
class SynthCase:
    params: SynthParams
    image: U8
    wheel_mask: Mask
    water_mask: Mask
    rim_mask: Mask
    depth_cm: float
    true_ellipse: Ellipse  # the near sidewall circle, fitted to its exact projection
    waterline_point_px: tuple[float, float]  # a point on the true near-face waterline
    waterline_dir: tuple[float, float]  # unit direction of the true near-face waterline

    @property
    def axis_ratio(self) -> float:
        return self.true_ellipse.axis_ratio


@dataclass(frozen=True, slots=True)
class _Camera:
    R: F64  # rows are camera x (right), y (down), z (forward) in world coordinates
    C: F64
    f: float
    cx: float
    cy: float

    def project(self, P: F64) -> F64:
        pc = (np.asarray(P) - self.C) @ self.R.T
        return np.stack(
            [self.f * pc[:, 0] / pc[:, 2] + self.cx, self.f * pc[:, 1] / pc[:, 2] + self.cy], axis=1
        )

    def rays(self, w: int, h: int) -> F64:
        u, v = np.meshgrid(np.arange(w, dtype=np.float64), np.arange(h, dtype=np.float64))
        dc = np.stack([(u - self.cx) / self.f, (v - self.cy) / self.f, np.ones_like(u)], axis=-1)
        return np.asarray(dc @ self.R, dtype=np.float64)  # world directions, shape (h, w, 3)


def _camera(p: SynthParams, radius: float) -> _Camera:
    yaw, pitch, roll = (math.radians(v) for v in (p.yaw_deg, p.pitch_deg, p.roll_deg))
    fwd = np.array(
        [math.sin(yaw) * math.cos(pitch), -math.sin(pitch), math.cos(yaw) * math.cos(pitch)]
    )
    x_c = np.cross(fwd, [0.0, 1.0, 0.0])
    x_c /= np.linalg.norm(x_c)
    y_c = np.cross(fwd, x_c)
    xr = math.cos(roll) * x_c + math.sin(roll) * y_c
    yr = -math.sin(roll) * x_c + math.cos(roll) * y_c
    f = p.focal_scale * p.width
    dist = f * (2.0 * radius) / p.wheel_px
    target = np.array([0.0, radius, 0.0])
    C = target - dist * fwd
    # Shifting the principal point moves the wheel off-centre with correct perspective.
    return _Camera(
        np.stack([xr, yr, fwd]),
        C,
        f,
        p.width / 2.0 + p.offset_x * p.width,
        p.height / 2.0 + p.offset_y * p.height,
    )


def _circle(radius: float, centre_y: float, z: float, n: int = 360) -> F64:
    t = np.linspace(0.0, 2.0 * math.pi, n, endpoint=False)
    return np.stack([radius * np.cos(t), centre_y + radius * np.sin(t), np.full(n, z)], axis=1)


def _clip_above(poly: F64, level: float) -> F64:
    """Sutherland-Hodgman clip of a closed 3D polygon to the half space Y >= level."""
    out: list[F64] = []
    n = poly.shape[0]
    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        ina, inb = a[1] >= level, b[1] >= level
        if ina:
            out.append(a)
        if ina != inb:
            t = (level - a[1]) / (b[1] - a[1])
            out.append(a + t * (b - a))
    return np.array(out) if out else np.zeros((0, 3))


def _fill_hull(pts2d: F64, w: int, h: int, ss: int = 4) -> Mask:
    """Rasterise a convex hull by pixel coverage (4x supersampled, half-coverage rule).

    Filling at native resolution includes every pixel the edge touches, which
    dilates the mask by about half a pixel and biases any edge-based fit.
    """
    if pts2d.shape[0] < 3:
        return np.zeros((h, w), bool)
    m = np.zeros((h * ss, w * ss), np.uint8)
    # Pixel centres sit at integers, so pixel i spans [i - 0.5, i + 0.5).
    hull = cv2.convexHull(np.rint((pts2d + 0.5) * ss * 16 - 8).astype(np.int32))
    cv2.fillConvexPoly(m, hull, 255, lineType=cv2.LINE_8, shift=4)
    cover = cv2.resize(m, (w, h), interpolation=cv2.INTER_AREA)
    return np.asarray(cover >= 128)


def _noisy(mask: Mask, px: float, rng: np.random.Generator) -> Mask:
    """Wobble a mask edge by roughly ``px`` pixels (RMS) with spatially smooth noise.

    The noise is added to the signed distance from the edge, so the boundary
    moves locally but no specks appear away from it.
    """
    if px <= 0 or not mask.any() or mask.all():
        return mask
    m8 = mask.astype(np.uint8)
    inside = cv2.distanceTransform(m8, cv2.DIST_L2, 5)
    outside = cv2.distanceTransform(1 - m8, cv2.DIST_L2, 5)
    sd = np.where(mask, inside - 0.5, 0.5 - outside)
    field = cv2.GaussianBlur(rng.standard_normal(mask.shape).astype(np.float32), (0, 0), 4.0)
    field = np.asarray(field, dtype=np.float64) / (float(field.std()) + 1e-9)
    return np.asarray(sd + px * field > 0)


def render(p: SynthParams, rng: np.random.Generator) -> SynthCase:
    D = p.diameter_cm / 100.0
    Rw = D / 2.0
    d = p.depth_fraction * D
    w, h = p.width, p.height
    cam = _camera(p, Rw)
    half = p.tyre_width_frac * D / 2.0
    # The near sidewall faces the camera.
    near_z = -half if cam.C[2] < 0 else half
    faces = [near_z] if half <= 0 else [near_z, -near_z]

    full_pts = np.concatenate([cam.project(_circle(Rw, Rw, z)) for z in faces])
    dry_parts = [_clip_above(_circle(Rw, Rw, z), d) for z in faces]
    dry_pts = [cam.project(q) for q in dry_parts if q.shape[0] >= 3]
    wheel = _fill_hull(np.concatenate(dry_pts), w, h) if dry_pts else np.zeros((h, w), bool)
    full_wheel = _fill_hull(full_pts, w, h)

    # Per-pixel ray casting for water, wall and body.
    dirs = cam.rays(w, h)
    dy = dirs[..., 1]
    with np.errstate(divide="ignore", invalid="ignore"):
        t_w = (d - cam.C[1]) / dy
    hit_z = cam.C[2] + t_w * dirs[..., 2]
    wall_z = (near_z + p.wall_behind_m) if p.water_behind else 0.0
    water = (dy < 0) & (t_w > 0) & (hit_z < wall_z)

    body = np.zeros((h, w), bool)
    if p.top_occlusion is not None:
        bz = near_z - 0.01 if cam.C[2] < 0 else near_z + 0.01
        with np.errstate(divide="ignore", invalid="ignore"):
            t_b = (bz - cam.C[2]) / dirs[..., 2]
        hx = cam.C[0] + t_b * dirs[..., 0]
        hy = cam.C[1] + t_b * dirs[..., 1]
        body = (t_b > 0) & (hy >= Rw * (1.0 + p.top_occlusion)) & (np.abs(hx) <= 1.4 * Rw)
        body &= ~(water & (t_w < t_b))
    wheel &= ~body
    water &= ~body

    # Exact near-face waterline chord in the image.
    near_dry = dry_parts[0]
    on_line = near_dry[np.abs(near_dry[:, 1] - d) < 1e-7]
    if on_line.shape[0] >= 2:
        chord = cam.project(on_line[:2])
    else:  # no chord (wheel dry or fully under): use the projected centre column
        chord = cam.project(np.array([[-Rw, d, near_z], [Rw, d, near_z]]))
    p0, p1 = chord[0], chord[-1]
    tvec = (p1 - p0) / (np.linalg.norm(p1 - p0) + 1e-12)
    nvec = np.array([-tvec[1], tvec[0]])
    top_img = cam.project(np.array([[0.0, 2.0 * Rw, near_z]]))[0]  # always dry
    if (top_img - p0) @ nvec < 0:
        nvec = -nvec  # nvec now points to the dry side

    if p.ripple_px > 0 and wheel.any():
        yy, xx = np.mgrid[0:h, 0:w]
        sd = (xx - p0[0]) * nvec[0] + (yy - p0[1]) * nvec[1]
        along = (xx - p0[0]) * tvec[0] + (yy - p0[1]) * tvec[1]
        phase = rng.uniform(0, 2 * math.pi)
        ripple = p.ripple_px * np.sin(2 * math.pi * along / p.ripple_wavelength_px + phase)
        band = (np.abs(sd) < p.ripple_px + 2.0) & full_wheel & ~body
        new_wheel = band & (sd > ripple)
        wheel = (wheel & ~band) | new_wheel
        water = (water & ~band) | (band & ~new_wheel)

    water &= ~wheel
    wheel_n = _noisy(wheel, p.mask_noise_px, rng)
    water_n = _noisy(water, p.mask_noise_px, rng) & ~wheel_n

    rim3d = _clip_above(_circle(Rw * p.rim_frac, Rw, near_z), d)
    rim = (
        _fill_hull(cam.project(rim3d), w, h) & wheel & ~body
        if rim3d.shape[0] >= 3
        else (np.zeros((h, w), bool))
    )
    rim_n = _noisy(rim, p.mask_noise_px, rng) & wheel_n
    image = _paint(p, cam, Rw, d, near_z, wheel, water, body, rng)
    near_full = cam.project(_circle(Rw, Rw, near_z))
    true_e = fit_ellipse_direct(near_full)
    assert true_e is not None
    mid = (p0 + p1) / 2.0
    return SynthCase(
        params=p,
        image=image,
        wheel_mask=wheel_n,
        water_mask=water_n,
        rim_mask=rim_n,
        depth_cm=d * 100.0,
        true_ellipse=true_e,
        waterline_point_px=(float(mid[0]), float(mid[1])),
        waterline_dir=(float(tvec[0]), float(tvec[1])),
    )


def _paint(
    p: SynthParams,
    cam: _Camera,
    Rw: float,
    d: float,
    near_z: float,
    wheel: Mask,
    water: Mask,
    body: Mask,
    rng: np.random.Generator,
) -> U8:
    h, w = wheel.shape
    img = np.empty((h, w, 3), np.float32)
    img[:] = (188, 184, 176)  # wall or sky, RGB
    img[water] = (122, 104, 74)  # muddy water
    img[body] = (40, 70, 140)
    img[wheel] = (32, 32, 34)  # tyre
    rim = _clip_above(_circle(Rw * p.rim_frac, Rw, near_z), d)
    if rim.shape[0] >= 3:
        rim_mask = _fill_hull(cam.project(rim), w, h) & wheel
        img[rim_mask] = (176, 178, 182)
        hub = _clip_above(_circle(Rw * 0.15, Rw, near_z), d)
        if hub.shape[0] >= 3:
            img[_fill_hull(cam.project(hub), w, h) & wheel] = (90, 92, 96)
    img += rng.normal(0.0, 6.0, img.shape).astype(np.float32)
    return np.clip(img, 0, 255).astype(np.uint8)


def random_params(rng: np.random.Generator, diameters_cm: list[float] | None = None) -> SynthParams:
    """Draw a phone-photo scene: depths 5 to 80 percent, ellipse ratios 0.5 to 1.0.

    The camera stands 1.5 to 6 m from the wheel with a main-camera to 2x lens
    (focal length 0.72 to 1.45 image widths), and the wheel spans 100 to 450 px.
    """
    for _ in range(1000):
        diam = float(rng.choice(diameters_cm)) if diameters_cm else float(rng.uniform(40.0, 66.0))
        frac = float(rng.uniform(0.05, 0.8))
        ratio = float(rng.uniform(0.5, 1.0))
        focal = float(rng.uniform(0.72, 1.45))
        dist = float(rng.uniform(1.5, 6.0))
        width = 960
        wheel_px = focal * width * diam / 100.0 / dist
        if not 100.0 <= wheel_px <= 450.0:
            continue
        p = SynthParams(
            diameter_cm=diam,
            depth_fraction=frac,
            yaw_deg=math.degrees(math.acos(ratio)) * float(rng.choice([-1.0, 1.0])),
            pitch_deg=float(rng.uniform(3.0, 30.0)),
            roll_deg=float(rng.uniform(-8.0, 8.0)),
            wheel_px=wheel_px,
            focal_scale=focal,
            offset_x=float(rng.uniform(-0.15, 0.15)),
            offset_y=float(rng.uniform(-0.1, 0.1)),
            tyre_width_frac=float(rng.uniform(0.0, 0.3)),
            water_behind=bool(rng.random() < 0.5),
            wall_behind_m=float(rng.uniform(0.5, 4.0)),
            top_occlusion=float(rng.uniform(0.55, 0.9)) if rng.random() < 0.2 else None,
            ripple_px=float(rng.uniform(0.0, 2.0)),
            ripple_wavelength_px=float(rng.uniform(20.0, 70.0)),
            mask_noise_px=float(rng.uniform(0.3, 1.5)),
            width=width,
        )
        cam = _camera(p, diam / 200.0)
        if cam.C[1] > frac * diam / 100.0 + 0.3:  # the camera is held above the water
            return p
    raise RuntimeError("could not draw a valid scene")


def distance_m(p: SynthParams) -> float:
    return p.focal_scale * p.width * p.diameter_cm / 100.0 / p.wheel_px
