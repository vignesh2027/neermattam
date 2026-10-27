"""Surface velocity of floodwater from tracked floating debris.

SAM 3.1 tracks floating objects through a short clip and gives a mask per
frame; their centroids are the tracks here. The scale (cm per pixel near the
water surface) comes from the wheel's fitted ellipse: the ellipse's semi-major
axis is the wheel radius seen without foreshortening.

Each track's velocity is a robust (Theil-Sen) straight-line fit of position
against time, so a single bad mask does not move it. Tracks are then filtered:
too short, moving against the common flow direction, or far from the median
speed (median absolute deviation test). Surface speed is the median of the
kept tracks.

Depth-averaged velocity is about 0.85 times the surface velocity. Sources:
Rantz et al. (1982), USGS Water-Supply Paper 2175, vol. 1, p. 262 ("a
coefficient of 0.85 is commonly used to convert the velocity of a surface float
to mean velocity in the vertical", doi:10.3133/wsp2175); WMO (2010) Manual on
Stream Gauging, WMO-No. 1044, vol. I, section 8.3 ("a coefficient of about 0.85
is commonly used"). Hauet et al. (2018, E3S Web Conf. 40, 06015,
doi:10.1051/e3sconf/20184006015) report a mean of about 0.8 over 3611 LSPIV
gaugings, with a 5th to 95th percentile range of 0.7 to 0.9; we carry 0.06 as
the coefficient's sigma.

Known limits, stated plainly: only motion in the image plane is measured (flow
towards or away from the camera is invisible); the scale is right at the
wheel's distance and wrong for debris much nearer or farther; a moving camera
adds its own motion unless a static reference track is given.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray
from scipy.stats import theilslopes

SURFACE_TO_MEAN = 0.85
SURFACE_TO_MEAN_SIGMA = 0.06

Track = Sequence[tuple[int, float, float]]  # (frame, x_px, y_px)


@dataclass(frozen=True, slots=True)
class TrackVelocity:
    track_id: str
    vx_mps: float
    vy_mps: float
    sigma_mps: float
    n_frames: int

    @property
    def speed_mps(self) -> float:
        return math.hypot(self.vx_mps, self.vy_mps)


@dataclass(frozen=True, slots=True)
class VelocityEstimate:
    surface_mps: float
    surface_sigma_mps: float
    mean_mps: float
    mean_sigma_mps: float
    n_tracks: int
    n_used: int
    tracks: list[TrackVelocity] = field(default_factory=list, repr=False)


@dataclass(frozen=True, slots=True)
class VelocityFailure:
    reason: str


def _scale_at(scale: float | Sequence[float], frame: int) -> float:
    if isinstance(scale, int | float):
        return float(scale)
    return float(scale[min(max(frame, 0), len(scale) - 1)])


def track_velocity(
    track_id: str,
    track: Track,
    fps: float,
    cm_per_px: float | Sequence[float],
    reference: Track | None = None,
) -> TrackVelocity | None:
    """Velocity of one track in metres per second, with a Theil-Sen sigma.

    Displacements between consecutive frames are converted with the mean scale
    of the two frames, then integrated into a path in centimetres. If a static
    ``reference`` track is given (for example the wheel), its displacement on
    the same frames is subtracted first, removing camera shake and pans.
    """
    pts = sorted(track)
    if reference is not None:
        ref = {f: (x, y) for f, x, y in reference}
        pts = [(f, x - ref[f][0], y - ref[f][1]) for f, x, y in pts if f in ref]
    if len(pts) < 3:
        return None
    frames = np.array([p[0] for p in pts], dtype=np.float64)
    xy = np.array([[p[1], p[2]] for p in pts], dtype=np.float64)
    steps = np.diff(xy, axis=0)
    scales = np.array(
        [
            (_scale_at(cm_per_px, int(a)) + _scale_at(cm_per_px, int(b))) / 2.0
            for a, b in itertools.pairwise(frames)
        ]
    )
    path_cm = np.vstack([[0.0, 0.0], np.cumsum(steps * scales[:, None], axis=0)])
    t = frames / fps
    vel, sig = [], []
    for k in range(2):
        slope, _, lo, hi = theilslopes(path_cm[:, k], t, alpha=0.95)
        vel.append(slope / 100.0)
        sig.append((hi - lo) / (2.0 * 1.96) / 100.0)
    speed = math.hypot(vel[0], vel[1])
    # Sigma of the speed from the two component sigmas, projected on the motion.
    if speed > 0:
        sigma = math.sqrt((vel[0] * sig[0]) ** 2 + (vel[1] * sig[1]) ** 2) / speed
    else:
        sigma = math.hypot(sig[0], sig[1])
    return TrackVelocity(track_id, vel[0], vel[1], sigma, len(pts))


def surface_velocity(
    tracks: Mapping[str, Track],
    fps: float,
    cm_per_px: float | Sequence[float],
    *,
    scale_rel_sigma: float = 0.05,
    reference: Track | None = None,
    min_frames: int = 5,
    max_direction_deg: float = 60.0,
) -> VelocityEstimate | VelocityFailure:
    """Surface speed from debris tracks, with outlier rejection and a sigma.

    ``scale_rel_sigma`` is the relative uncertainty of ``cm_per_px`` (from the
    wheel diameter's sigma and the ellipse fit). The returned sigma combines the
    spread between tracks (or one track's own sigma) with the scale error.
    """
    if fps <= 0:
        return VelocityFailure("frame rate unknown")
    vs: list[TrackVelocity] = []
    for tid, tr in tracks.items():
        if len(tr) < min_frames:
            continue
        v = track_velocity(tid, tr, fps, cm_per_px, reference)
        if v is not None and math.isfinite(v.speed_mps):
            vs.append(v)
    if not vs:
        return VelocityFailure("no floating object was tracked for long enough")

    # Common flow direction: the median of unit vectors, weighted by speed.
    vec = np.array([[v.vx_mps, v.vy_mps] for v in vs])
    speeds = np.hypot(vec[:, 0], vec[:, 1])
    kept = np.ones(len(vs), dtype=bool)
    if len(vs) >= 3:
        mean_dir = vec.sum(axis=0)
        if np.linalg.norm(mean_dir) > 0:
            mean_dir /= np.linalg.norm(mean_dir)
            cosang = (vec @ mean_dir) / np.maximum(speeds, 1e-12)
            kept &= cosang >= math.cos(math.radians(max_direction_deg))
        med = float(np.median(speeds[kept])) if kept.any() else float(np.median(speeds))
        mad = float(np.median(np.abs(speeds[kept] - med))) if kept.any() else 0.0
        # Debris drifts at somewhat different speeds across a street, so the
        # tolerance never drops below 15 percent of the median speed.
        kept &= np.abs(speeds - med) <= max(3.0 * 1.4826 * mad, 0.15 * med)
    used = [v for v, k in zip(vs, kept, strict=True) if k]
    if not used:
        return VelocityFailure("tracks disagree on the flow")
    us = np.array([v.speed_mps for v in used])
    surface = float(np.median(us))
    if len(used) >= 2:
        stat = 1.2533 * float(np.std(us, ddof=1)) / math.sqrt(len(used))
    else:
        stat = used[0].sigma_mps
    sigma = math.hypot(stat, scale_rel_sigma * surface)
    mean = SURFACE_TO_MEAN * surface
    mean_sigma = math.hypot(SURFACE_TO_MEAN * sigma, SURFACE_TO_MEAN_SIGMA * surface)
    return VelocityEstimate(surface, sigma, mean, mean_sigma, len(vs), len(used), used)


def scale_from_ellipse_cm_per_px(diameter_cm: float, semi_major_px: float) -> float:
    """Centimetres per pixel at the wheel, from its unforeshortened semi-axis."""
    return diameter_cm / (2.0 * semi_major_px)


def centroids_to_tracks(
    centroids: dict[str, NDArray[np.float64]] | dict[str, list[tuple[int, float, float]]],
) -> dict[str, Track]:
    return {k: [(int(f), float(x), float(y)) for f, x, y in v] for k, v in centroids.items()}
