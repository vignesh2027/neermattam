from __future__ import annotations

import math

import numpy as np
import pytest

from neermattam_perception import velocity as v


def make_track(
    vx_cm_s: float,
    vy_cm_s: float,
    fps: float,
    n: int,
    cm_per_px: float,
    x0: float = 100.0,
    y0: float = 200.0,
    noise_px: float = 0.0,
    rng: np.random.Generator | None = None,
) -> list[tuple[int, float, float]]:
    rng = rng or np.random.default_rng(0)
    out = []
    for f in range(n):
        t = f / fps
        out.append(
            (
                f,
                x0 + vx_cm_s * t / cm_per_px + rng.normal(0, noise_px),
                y0 + vy_cm_s * t / cm_per_px + rng.normal(0, noise_px),
            )
        )
    return out


def test_single_track_speed() -> None:
    tr = make_track(60.0, 80.0, fps=30, n=60, cm_per_px=0.5)  # 1.0 m/s
    tv = v.track_velocity("a", tr, 30.0, 0.5)
    assert tv is not None
    assert tv.speed_mps == pytest.approx(1.0, rel=1e-6)
    assert tv.sigma_mps == pytest.approx(0.0, abs=1e-6)


def test_robust_to_a_bad_frame() -> None:
    tr = make_track(50.0, 0.0, fps=25, n=50, cm_per_px=0.4)
    tr[20] = (20, tr[20][1] + 300.0, tr[20][2] - 200.0)  # one wild mask centroid
    tv = v.track_velocity("a", tr, 25.0, 0.4)
    assert tv is not None and tv.speed_mps == pytest.approx(0.5, rel=0.02)


def test_surface_velocity_rejects_outlier_tracks() -> None:
    rng = np.random.default_rng(1)
    tracks = {
        str(i): make_track(40.0 + rng.normal(0, 1), 5.0, 30, 45, 0.3, noise_px=0.5, rng=rng)
        for i in range(6)
    }
    tracks["upstream"] = make_track(-40.0, -5.0, 30, 45, 0.3)  # against the flow
    tracks["stuck"] = make_track(0.5, 0.0, 30, 45, 0.3)  # caught on a kerb
    tracks["short"] = make_track(40.0, 5.0, 30, 3, 0.3)  # too few frames
    est = v.surface_velocity(tracks, 30.0, 0.3, scale_rel_sigma=0.05)
    assert isinstance(est, v.VelocityEstimate)
    assert est.n_tracks == 8 and est.n_used == 6
    assert est.surface_mps == pytest.approx(math.hypot(0.40, 0.05), rel=0.05)
    assert est.mean_mps == pytest.approx(v.SURFACE_TO_MEAN * est.surface_mps)
    assert est.mean_sigma_mps > v.SURFACE_TO_MEAN_SIGMA * est.surface_mps * 0.99
    assert est.surface_sigma_mps >= 0.05 * est.surface_mps


def test_reference_track_removes_camera_pan() -> None:
    fps, scale = 30.0, 0.5
    pan = make_track(20.0, 0.0, fps, 40, scale, x0=0, y0=0)  # the camera pans; the wheel "moves"
    debris = make_track(20.0 + 50.0, 0.0, fps, 40, scale)
    est = v.surface_velocity({"d": debris}, fps, scale, reference=pan)
    assert isinstance(est, v.VelocityEstimate)
    assert est.surface_mps == pytest.approx(0.5, rel=1e-3)


def test_per_frame_scale() -> None:
    # The scale doubles halfway (zoom); pixel motion halves, metric speed is constant.
    fps = 10.0
    track, x = [], 0.0
    scales = [1.0] * 10 + [2.0] * 10
    for f in range(20):
        track.append((f, x, 0.0))
        x += 10.0 / scales[f]  # 10 cm per frame
    tv = v.track_velocity("z", track, fps, scales)
    assert tv is not None and tv.speed_mps == pytest.approx(1.0, rel=0.06)


def test_failures() -> None:
    assert isinstance(v.surface_velocity({}, 30.0, 0.5), v.VelocityFailure)
    assert isinstance(
        v.surface_velocity({"a": make_track(1, 1, 30, 30, 1)}, 0.0, 0.5), v.VelocityFailure
    )
    assert v.scale_from_ellipse_cm_per_px(43.4, 100.0) == pytest.approx(0.217)


def test_every_rejection_has_a_reason() -> None:
    rng = np.random.default_rng(1)
    tracks = {
        str(i): make_track(40.0 + rng.normal(0, 1), 5.0, 30, 45, 0.3, noise_px=0.5, rng=rng)
        for i in range(6)
    }
    tracks["upstream"] = make_track(-40.0, -5.0, 30, 45, 0.3)
    tracks["stuck"] = make_track(0.5, 0.0, 30, 45, 0.3)
    tracks["short"] = make_track(40.0, 5.0, 30, 3, 0.3)
    est = v.surface_velocity(tracks, 30.0, 0.3)
    assert isinstance(est, v.VelocityEstimate)
    assert est.rejected == {
        "upstream": v.REJECT_DIRECTION,
        "stuck": v.REJECT_SPEED,
        "short": v.REJECT_SHORT,
    }


def test_a_jittering_still_object_is_not_a_tracer() -> None:
    # A mask on something fixed: centroid noise only. Its fitted speed is not zero
    # (the speed of noise never is), but it is within two sigma of zero.
    rng = np.random.default_rng(3)
    still = make_track(0.0, 0.0, 25, 50, 0.5, noise_px=1.5, rng=rng)
    moving = make_track(50.0, 0.0, 25, 50, 0.5, noise_px=1.5, rng=rng)
    est = v.surface_velocity({"still": still, "moving": moving}, 25.0, 0.5)
    assert isinstance(est, v.VelocityEstimate)
    assert est.rejected == {"still": v.REJECT_STILL} and est.n_used == 1
    alone = v.surface_velocity({"still": still}, 25.0, 0.5)
    assert isinstance(alone, v.VelocityFailure)
    assert alone.reason == "no tracked object moved measurably"
    assert alone.rejected == {"still": v.REJECT_STILL}


def test_opposite_tracks_share_no_direction() -> None:
    # Before: the mean direction was zero, the direction test was skipped and the
    # two speeds were averaged into a confident answer.
    a = make_track(40.0, 0.0, 30, 30, 0.5)
    b = make_track(-40.0, 0.0, 30, 30, 0.5)
    est = v.surface_velocity({"a": a, "b": b}, 30.0, 0.5)
    assert isinstance(est, v.VelocityFailure)
    assert est.reason == "tracks move in no common direction"
    assert est.rejected == {"a": v.REJECT_DIRECTION, "b": v.REJECT_DIRECTION}


def test_two_agreeing_tracks_keep_their_own_sigma() -> None:
    # Two noisy tracks whose fitted speeds happen to match: the spread between
    # them is near zero, but the sigma must not fall below what each track's own
    # fit supports (before, it fell to the scale term alone).
    rng = np.random.default_rng(7)
    a = make_track(50.0, 0.0, 25, 20, 0.5, noise_px=4.0, rng=rng)
    tv = v.track_velocity("a", a, 25.0, 0.5)
    assert tv is not None
    b = [(f, x + 30.0, y + 40.0) for f, x, y in a]  # the same path, shifted
    est = v.surface_velocity({"a": a, "b": b}, 25.0, 0.5, scale_rel_sigma=0.0)
    assert isinstance(est, v.VelocityEstimate) and est.n_used == 2
    assert est.surface_sigma_mps == pytest.approx(tv.sigma_mps / math.sqrt(2), rel=1e-6)
    assert est.surface_sigma_mps > 0.01


def test_pixel_speed() -> None:
    tr = make_track(60.0, 80.0, fps=30, n=30, cm_per_px=1.0)  # 100 px/s
    ps = v.pixel_speed("a", tr, 30.0)
    assert ps is not None and ps.speed_mps == pytest.approx(100.0)
