"""Surface velocity of floodwater from tracked floating debris.

SAM (2.1 from boxes on one frame, or 3.1 from a noun phrase) tracks floating
objects through a short clip and gives a mask per
frame; their centroids are the tracks here. The scale (cm per pixel near the
water surface) comes from the wheel's fitted ellipse: the ellipse's semi-major
axis is the wheel radius seen without foreshortening.

Each track's velocity is a robust (Theil-Sen) straight-line fit of position
against time, so a single bad mask does not move it. Tracks are then filtered:
too short, not moving measurably (speed within two sigma of zero), moving
against the common flow direction, or far from the median speed (median
absolute deviation test); each rejection keeps its reason. Surface speed is
the median of the kept tracks.

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
    # Every track left out, with the reason: track id to a short phrase.
    rejected: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class VelocityFailure:
    reason: str
    rejected: dict[str, str] = field(default_factory=dict)


REJECT_SHORT = "tracked on too few frames"
REJECT_NO_FIT = "no straight-line fit"
REJECT_STILL = "not moving: speed within two sigma of zero"
REJECT_DIRECTION = "moves against the common flow direction"
REJECT_SPEED = "speed far from the median of the other tracks"
# The summed velocity vectors must keep at least this share of the summed speeds
# for the tracks to have a common direction.
NO_COMMON_DIRECTION = 0.25


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
    min_significance: float = 2.0,
) -> VelocityEstimate | VelocityFailure:
    """Surface speed from debris tracks, with outlier rejection and a sigma.

    ``scale_rel_sigma`` is the relative uncertainty of ``cm_per_px`` (from the
    wheel diameter's sigma and the ellipse fit). The returned sigma combines the
    spread between tracks (or one track's own sigma) with the scale error.

    A track is left out, with its reason in ``rejected``, when it has fewer than
    ``min_frames`` points, when its speed is less than ``min_significance``
    times its own sigma (something caught on a kerb, or a mask that sat on a
    fixed object, is not a tracer of the flow), when it points more than
    ``max_direction_deg`` away from the speed-weighted mean direction (two or
    more tracks), or when its speed is far from the median (three or more).
    """
    if fps <= 0:
        return VelocityFailure("frame rate unknown")
    rejected: dict[str, str] = {}
    vs: list[TrackVelocity] = []
    for tid, tr in tracks.items():
        if len(tr) < min_frames:
            rejected[tid] = REJECT_SHORT
            continue
        v = track_velocity(tid, tr, fps, cm_per_px, reference)
        if v is None or not math.isfinite(v.speed_mps):
            rejected[tid] = REJECT_NO_FIT
            continue
        vs.append(v)
    if not vs:
        return VelocityFailure("no floating object was tracked for long enough", rejected)
    n_fitted = len(vs)
    moving = [v for v in vs if v.speed_mps > min_significance * v.sigma_mps]
    for v in vs:
        if v not in moving:
            rejected[v.track_id] = REJECT_STILL
    vs = moving
    if not vs:
        return VelocityFailure("no tracked object moved measurably", rejected)

    vec = np.array([[v.vx_mps, v.vy_mps] for v in vs])
    speeds = np.hypot(vec[:, 0], vec[:, 1])
    kept = np.ones(len(vs), dtype=bool)
    if len(vs) >= 2:
        # Common flow direction: the mean of the velocity vectors, so faster
        # (better resolved) tracks weigh more.
        mean_dir = vec.sum(axis=0)
        # Tracks that mostly cancel out (for example two moving in opposite
        # directions) have no common direction at all: nothing can be kept.
        if np.linalg.norm(mean_dir) < NO_COMMON_DIRECTION * float(speeds.sum()):
            for v in vs:
                rejected[v.track_id] = REJECT_DIRECTION
            return VelocityFailure("tracks move in no common direction", rejected)
        if np.linalg.norm(mean_dir) > 0:
            mean_dir /= np.linalg.norm(mean_dir)
            cosang = (vec @ mean_dir) / np.maximum(speeds, 1e-12)
            off = cosang < math.cos(math.radians(max_direction_deg))
            for v, o in zip(vs, off, strict=True):
                if o:
                    rejected[v.track_id] = REJECT_DIRECTION
            kept &= ~off
    if len(vs) >= 3 and kept.any():
        med = float(np.median(speeds[kept]))
        mad = float(np.median(np.abs(speeds[kept] - med)))
        # Debris drifts at somewhat different speeds across a street, so the
        # tolerance never drops below 15 percent of the median speed.
        far = np.abs(speeds - med) > max(3.0 * 1.4826 * mad, 0.15 * med)
        for v, f, k in zip(vs, far, kept, strict=True):
            if f and k:
                rejected[v.track_id] = REJECT_SPEED
        kept &= ~far
    used = [v for v, k in zip(vs, kept, strict=True) if k]
    if not used:
        return VelocityFailure("tracks disagree on the flow", rejected)
    us = np.array([v.speed_mps for v in used])
    surface = float(np.median(us))
    # The standard error of the median from the spread between tracks, but never
    # less than the tracks' own fit sigma allows: two tracks that agree by
    # chance must not look more certain than either of them.
    own = math.sqrt(float(np.mean([v.sigma_mps**2 for v in used])) / len(used))
    if len(used) >= 2:
        stat = max(1.2533 * float(np.std(us, ddof=1)) / math.sqrt(len(used)), own)
    else:
        stat = used[0].sigma_mps
    sigma = math.hypot(stat, scale_rel_sigma * surface)
    mean = SURFACE_TO_MEAN * surface
    mean_sigma = math.hypot(SURFACE_TO_MEAN * sigma, SURFACE_TO_MEAN_SIGMA * surface)
    return VelocityEstimate(surface, sigma, mean, mean_sigma, n_fitted, len(used), used, rejected)


def pixel_speed(
    track_id: str, track: Track, fps: float, reference: Track | None = None
) -> TrackVelocity | None:
    """A track's velocity in pixels per second (no scale), for reporting motion
    when no reference was measured. The fields named ``_mps`` hold px/s here."""
    return track_velocity(track_id, track, fps, 100.0, reference)


def scale_from_ellipse_cm_per_px(diameter_cm: float, semi_major_px: float) -> float:
    """Centimetres per pixel at the wheel, from its unforeshortened semi-axis."""
    return diameter_cm / (2.0 * semi_major_px)


def centroids_to_tracks(
    centroids: dict[str, NDArray[np.float64]] | dict[str, list[tuple[int, float, float]]],
) -> dict[str, Track]:
    return {k: [(int(f), float(x), float(y)) for f, x, y in v] for k, v in centroids.items()}
