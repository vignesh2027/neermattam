from __future__ import annotations

import math

import numpy as np
import pytest

from neermattam_perception import geometry as g
from neermattam_perception.synth import SynthParams, _camera, _circle, render


def ellipse_points(e: g.Ellipse, t0: float, t1: float, n: int = 200) -> np.ndarray:
    t = np.linspace(t0, t1, n)
    return e.from_unit(np.stack([np.cos(t), np.sin(t)], axis=1))


def test_direct_fit_recovers_exact_ellipse() -> None:
    truth = g.Ellipse(320.0, 240.0, 120.0, 70.0, 0.4)
    fit = g.fit_ellipse_direct(ellipse_points(truth, 0, 2 * math.pi))
    assert fit is not None
    assert fit.cx == pytest.approx(320.0, abs=1e-6)
    assert fit.cy == pytest.approx(240.0, abs=1e-6)
    assert fit.a == pytest.approx(120.0, rel=1e-6)
    assert fit.b == pytest.approx(70.0, rel=1e-6)
    assert math.remainder(fit.theta - 0.4, math.pi) == pytest.approx(0.0, abs=1e-6)


def test_direct_fit_needs_five_points_and_rejects_lines() -> None:
    assert g.fit_ellipse_direct(np.zeros((4, 2))) is None
    line = np.stack([np.arange(20.0), 2 * np.arange(20.0)], axis=1)
    assert g.fit_ellipse_direct(line) is None or g.fit_ellipse_direct(line).axis_ratio < 0.05


def test_conic_matrix_round_trip() -> None:
    e = g.Ellipse(10.0, -5.0, 9.0, 4.0, -0.7)
    M = g.conic_matrix(e)
    back = g.conic_to_ellipse(
        np.array([M[0, 0], 2 * M[0, 1], M[1, 1], 2 * M[0, 2], 2 * M[1, 2], M[2, 2]])
    )
    assert back is not None
    assert (back.cx, back.cy, back.a, back.b) == pytest.approx((10.0, -5.0, 9.0, 4.0))


def test_orthogonal_distance_matches_brute_force() -> None:
    a, b = 100.0, 45.0
    pts = np.array([[130.0, 10.0], [-20.0, 60.0], [5.0, -5.0], [-90.0, -30.0], [0.0, 52.0]])
    d = g._sampson_raw(pts, 0.0, 0.0, a, b, 1.0, 0.0)
    tt = np.linspace(0, 2 * np.pi, 400_001)
    curve = np.stack([a * np.cos(tt), b * np.sin(tt)], axis=1)
    brute = np.array([np.min(np.hypot(*(curve - p).T)) for p in pts])
    inside = (pts[:, 0] / a) ** 2 + (pts[:, 1] / b) ** 2 < 1
    assert np.abs(d) == pytest.approx(brute, abs=1e-3)
    assert np.all((d < 0) == inside)


def test_ransac_partial_arc_with_outliers() -> None:
    truth = g.Ellipse(400.0, 300.0, 150.0, 110.0, 0.2)
    rng = np.random.default_rng(3)
    arc = ellipse_points(truth, math.radians(150), math.radians(390), 300)  # 240 degrees
    arc += rng.normal(0, 0.5, arc.shape)
    junk = rng.uniform(250, 550, (60, 2))  # 17 percent gross outliers
    pts = np.vstack([arc, junk])
    fit = g.fit_ellipse_ransac(pts, rng, tol=1.5, limits=g.limits_for_points(arc, 0.3))
    assert fit is not None
    assert fit.ellipse.a == pytest.approx(150.0, rel=0.02)
    assert fit.ellipse.b == pytest.approx(110.0, rel=0.02)
    assert fit.inliers[:300].mean() > 0.9 and fit.inliers[300:].mean() < 0.2


def test_short_arc_cannot_explode() -> None:
    truth = g.Ellipse(300.0, 300.0, 100.0, 100.0, 0.0)
    arc = ellipse_points(truth, math.radians(250), math.radians(290), 60)  # a 40 degree sliver
    limits = g.limits_for_points(arc, 0.3)
    fit = g.fit_ellipse_ransac(arc, np.random.default_rng(0), tol=1.0, limits=limits)
    if fit is not None:
        assert max(fit.ellipse.a, fit.ellipse.b) <= limits.max_semi_major


@pytest.mark.parametrize("s", [-0.6, -0.2, 0.0, 0.3, 0.8])
def test_submerged_fraction_on_unit_circle(s: float) -> None:
    e = g.Ellipse(0.0, 0.0, 1.0, 1.0, 0.0)
    line = g.Line(np.array([0.0, s]), np.array([1.0, 0.0]))  # y = s, water below (y grows down)
    assert g.submerged_fraction(e, line, np.array([0.0, 1.0])) == pytest.approx((1 - s) / 2)


def test_fraction_is_affine_invariant() -> None:
    """A sheared, rotated, scaled view gives the same fraction (the core of the method)."""
    circle = g.Ellipse(0.0, 0.0, 1.0, 1.0, 0.0)
    line = g.Line(np.array([0.0, 0.25]), np.array([1.0, 0.0]))
    f0 = g.submerged_fraction(circle, line, np.array([0.0, 1.0]))
    A = np.array([[80.0, 25.0], [-10.0, 55.0]])
    off = np.array([300.0, 200.0])
    pts = ellipse_points(circle, 0, 2 * math.pi) @ A.T + off
    e = g.fit_ellipse_direct(pts)
    assert e is not None
    p = A @ line.point + off
    d = A @ line.direction
    img_line = g.Line(p, d / np.linalg.norm(d))
    f1 = g.submerged_fraction(e, img_line, A @ np.array([0.0, 1.0]))
    assert f1 == pytest.approx(f0, abs=1e-9)


def test_projective_fraction_is_exact_under_perspective() -> None:
    """Close range, steep pitch: affine is off, the calibrated correction is exact."""
    p = SynthParams(yaw_deg=40, pitch_deg=25, tyre_width_frac=0.0, depth_fraction=0.4)
    R = p.diameter_cm / 200
    cam = _camera(p, R)
    d = p.depth_fraction * 2 * R
    e = g.fit_ellipse_direct(cam.project(_circle(R, R, 0.0)))
    chord = cam.project(np.array([[-R, d, 0.0], [R, d, 0.0]]))
    line = g.Line(chord[0], (chord[1] - chord[0]) / np.linalg.norm(chord[1] - chord[0]))
    down = (
        cam.project(np.array([[0.0, d - 0.05, 0.0]]))[0] - cam.project(np.array([[0, d, 0.0]]))[0]
    )
    assert e is not None
    pin = g.PinholeCamera(cam.f, cam.cx, cam.cy)
    exact = g.projective_fraction(e, line, down, pin)
    affine = g.submerged_fraction(e, line, down)
    assert exact is not None
    assert exact.fraction == pytest.approx(0.4, abs=1e-4)
    assert abs(affine - 0.4) > 0.01  # the correction matters at this range


def test_tread_shift_is_along_minor_axis() -> None:
    sh = g.tread_shift(100.0, 60.0, 0.0, 0.5)
    assert sh[0] == pytest.approx(0.0, abs=1e-12)
    assert abs(sh[1]) == pytest.approx(0.5 * 100.0 * 0.8)
    assert np.allclose(g.tread_shift(100.0, 100.0, 0.3, 0.5), 0.0)


def test_straight_runs_flag_occluder_edges_only() -> None:
    rng = np.random.default_rng(0)
    arc = ellipse_points(g.Ellipse(200.0, 200.0, 100.0, 100.0, 0.0), math.pi, 2 * math.pi, 300)
    edge = np.stack([np.linspace(100, 300, 150), np.full(150, 140.0)], axis=1)
    marked = g.straight_runs(np.vstack([arc, edge]), rng, tol=1.5, min_len=60.0)
    assert marked[300:].mean() > 0.9
    assert marked[:300].mean() < 0.1


# --------------------------------------------------------------------------- end to end


def test_measures_clean_scene_accurately(easy_case) -> None:  # type: ignore[no-untyped-def]
    m = g.measure_wheel_depth(
        easy_case.wheel_mask,
        easy_case.water_mask,
        43.4,
        0.0,
        g.GeometryConfig(mc_draws=20),
        rim_mask=easy_case.rim_mask,
    )
    assert isinstance(m, g.DepthMeasurement), m
    assert m.depth_cm == pytest.approx(easy_case.depth_cm, abs=1.5)
    assert m.depth_sigma_cm > 0
    assert m.arc_covered_deg >= 180


def test_diameter_sigma_propagates(easy_case) -> None:  # type: ignore[no-untyped-def]
    cfg = g.GeometryConfig(mc_draws=60, model_sigma_table=())
    a = g.measure_wheel_depth(easy_case.wheel_mask, easy_case.water_mask, 43.4, 0.0, cfg)
    b = g.measure_wheel_depth(easy_case.wheel_mask, easy_case.water_mask, 43.4, 4.0, cfg)
    assert isinstance(a, g.DepthMeasurement) and isinstance(b, g.DepthMeasurement)
    # Diameter error contributes about fraction * sigma_D.
    assert b.depth_sigma_cm > a.depth_sigma_cm
    assert b.depth_sigma_cm >= 0.6 * a.fraction * 4.0


def test_monte_carlo_is_deterministic(easy_case) -> None:  # type: ignore[no-untyped-def]
    cfg = g.GeometryConfig(mc_draws=15, seed=5)
    a = g.measure_wheel_depth(easy_case.wheel_mask, easy_case.water_mask, 43.4, 1.0, cfg)
    b = g.measure_wheel_depth(easy_case.wheel_mask, easy_case.water_mask, 43.4, 1.0, cfg)
    assert isinstance(a, g.DepthMeasurement) and isinstance(b, g.DepthMeasurement)
    assert a.depth_sigma_cm == b.depth_sigma_cm


def test_failure_empty_and_tiny_masks() -> None:
    z = np.zeros((100, 100), bool)
    r = g.measure_wheel_depth(z, z, 43.4, 1.0)
    assert isinstance(r, g.DepthFailure) and r.reason is g.FailureReason.WHEEL_MASK_EMPTY
    tiny = z.copy()
    tiny[40:50, 40:50] = True
    r = g.measure_wheel_depth(tiny, z, 43.4, 1.0)
    assert isinstance(r, g.DepthFailure) and r.reason is g.FailureReason.WHEEL_TOO_SMALL
    assert "too small" in r.text


def test_failure_no_water(easy_case) -> None:  # type: ignore[no-untyped-def]
    r = g.measure_wheel_depth(easy_case.wheel_mask, np.zeros_like(easy_case.water_mask), 43.4, 1.0)
    assert isinstance(r, g.DepthFailure) and r.reason is g.FailureReason.NO_WATER_CONTACT


def test_failure_water_only_behind_a_dry_wheel() -> None:
    """Water seen around a dry wheel touches the outline but cuts no chord."""
    h, w = 400, 400
    yy, xx = np.mgrid[0:h, 0:w]
    wheel = (xx - 200) ** 2 + (yy - 200) ** 2 <= 100**2
    water = ~wheel & ((xx - 200) ** 2 + (yy - 200) ** 2 <= 180**2)
    r = g.measure_wheel_depth(wheel, water, 43.4, 1.0)
    assert isinstance(r, g.DepthFailure)
    assert r.reason in (g.FailureReason.NO_WATER_CONTACT, g.FailureReason.WATERLINE_AMBIGUOUS)


def test_failure_nearly_submerged_arc_too_short() -> None:
    p = SynthParams(
        depth_fraction=0.92,
        yaw_deg=10,
        pitch_deg=30,
        tyre_width_frac=0.0,
        mask_noise_px=0.0,
        width=640,
        height=480,
    )
    case = render(p, np.random.default_rng(2))
    assert case.water_mask.any()  # the camera is above the water
    r = g.measure_wheel_depth(case.wheel_mask, case.water_mask, p.diameter_cm, 1.0)
    assert isinstance(r, g.DepthFailure)
    assert r.reason in (
        g.FailureReason.ARC_TOO_SHORT,
        g.FailureReason.WHEEL_TOO_SMALL,
        g.FailureReason.AMBIGUOUS_OUTLINE,
        g.FailureReason.ELLIPSE_FIT_FAILED,
    )


def test_occluded_top_is_not_fitted_as_wheel() -> None:
    """Bodywork cutting the top of the wheel must not flatten the fitted ellipse."""
    p = SynthParams(
        depth_fraction=0.3,
        yaw_deg=10,
        pitch_deg=8,
        tyre_width_frac=0.0,
        top_occlusion=0.6,
        mask_noise_px=0.3,
        width=640,
        height=480,
    )
    case = render(p, np.random.default_rng(4))
    r = g.measure_wheel_depth(
        case.wheel_mask, case.water_mask, p.diameter_cm, 0.0, g.GeometryConfig(mc_draws=0)
    )
    assert isinstance(r, g.DepthMeasurement), r
    assert r.depth_cm == pytest.approx(case.depth_cm, abs=2.5)


def test_second_waterline_depth(easy_case) -> None:  # type: ignore[no-untyped-def]
    m = g.measure_wheel_depth(
        easy_case.wheel_mask, easy_case.water_mask, 43.4, 0.0, g.GeometryConfig(mc_draws=0)
    )
    assert isinstance(m, g.DepthMeasurement)
    true_line = g.Line(np.array(easy_case.waterline_point_px), np.array(easy_case.waterline_dir))
    assert g.depth_from_line(m, true_line) == pytest.approx(m.depth_cm, abs=1.5)
    top, cross, bottom = g.vertical_diameter(m.ellipse, m.waterline.line, m.waterline.water_normal)
    assert np.linalg.norm(bottom - cross) / np.linalg.norm(bottom - top) == pytest.approx(
        m.fraction_affine, abs=1e-6
    )


def test_model_sigma_interpolates() -> None:
    table = ((100.0, 0.2), (200.0, 0.05))
    assert g.model_sigma(150.0, table) == pytest.approx(0.125)
    assert g.model_sigma(50.0, table) == pytest.approx(0.2)
    assert g.model_sigma(150.0, ()) == 0.0
