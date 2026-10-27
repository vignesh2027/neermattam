from __future__ import annotations

import pytest

from neermattam_perception import references as r


@pytest.mark.parametrize(
    ("code", "cm"),
    [
        ("90/100-10", 43.4),  # 254 + 2 x 90 mm
        ("90/90-12", 46.68),  # 304.8 + 2 x 81
        ("80/100-18", 61.72),
        ("120/80-17", 62.38),
        ("165/80 R14", 61.96),  # 355.6 + 2 x 132
        ("185/65R15", 62.15),
        ("195/55 R16", 62.09),
        ("295/80 R22.5", 104.35),
        ("2.75-18", 59.69),  # (18 + 2 x 2.75) x 25.4 mm
        ("4.00-8", 40.64),
        ("10.00 R20", 101.6),
    ],
)
def test_tyre_formula(code: str, cm: float) -> None:
    assert r.tyre_diameter_cm(code) == pytest.approx(cm, abs=0.01)


def test_parse_reports_system() -> None:
    assert r.parse_tyre_code("90/100-10").system == "metric"
    assert r.parse_tyre_code("2.75-18").system == "inch"
    assert r.parse_tyre_code("80/100-18 M/C 47P").rim_in == 18


@pytest.mark.parametrize("bad", ["", "R14", "abc", "90-"])
def test_bad_codes_raise(bad: str) -> None:
    with pytest.raises(ValueError):
        r.parse_tyre_code(bad)


def test_catalogue_entries_are_complete() -> None:
    kinds = {e.kind for e in r.CATALOGUE}
    assert kinds == set(r.WHEEL_KINDS)
    for e in r.CATALOGUE:
        assert e.source_url.startswith("https://")
        assert 30 < e.diameter_cm < 120 and e.sigma_cm > 0
        assert e.source.startswith("catalogue:")
    keys = set(r.vehicles())
    for needed in (
        "honda_activa_110",
        "tvs_jupiter",
        "hero_splendor_plus",
        "bajaj_pulsar_150",
        "re_classic_350",
        "maruti_swift",
        "hyundai_i20",
        "maruti_wagonr",
        "bajaj_re_auto",
        "city_bus",
    ):
        assert needed in keys


def test_measured_diameters_override_formula_for_bias_tyres() -> None:
    auto = next(e for e in r.CATALOGUE if e.vehicle == "bajaj_re_auto")
    assert auto.diameter_cm == pytest.approx(42.6)
    assert auto.diameter_cm > r.tyre_diameter_cm("4.00-8")


def test_resolve_specific_wheel() -> None:
    c = r.resolve_reference("two_wheeler_wheel", vehicle="honda_activa_110", position="rear")
    assert c.diameter_cm == pytest.approx(43.4)
    assert c.diameter_sigma_cm == pytest.approx(r.SIGMA_TWO_WHEELER_CM)
    assert c.source == "catalogue:90/100-10"


def test_unknown_position_is_a_mixture() -> None:
    c = r.resolve_reference("two_wheeler_wheel", vehicle="honda_activa_110")
    assert 43.4 < c.diameter_cm < 46.7
    assert c.diameter_sigma_cm > r.SIGMA_TWO_WHEELER_CM  # covers both wheel sizes
    assert c.source == "catalogue:90/100-10|90/90-12"


def test_generic_priors_are_wider_than_one_tyre() -> None:
    two = r.resolve_reference("two_wheeler_wheel")
    car = r.resolve_reference("car_wheel")
    assert two.source == "prior:two_wheeler_wheel"
    assert two.diameter_sigma_cm > 3 * r.SIGMA_TWO_WHEELER_CM
    assert car.diameter_sigma_cm > r.SIGMA_CAR_CM
    assert 55 < car.diameter_cm < 65


def test_hint_overrides_catalogue() -> None:
    c = r.resolve_reference("car_wheel", vehicle="maruti_swift", hint_diameter_cm=58.0)
    assert (c.diameter_cm, c.source) == (58.0, "hint")
    assert c.diameter_sigma_cm == pytest.approx(1.16)
    with pytest.raises(ValueError):
        r.resolve_reference("car_wheel", hint_diameter_cm=500.0)


def test_unknown_vehicle_falls_back_to_prior() -> None:
    assert r.resolve_reference("car_wheel", vehicle="tesla").source == "prior:car_wheel"


def test_kerb_is_not_a_wheel_but_is_cited() -> None:
    with pytest.raises(ValueError):
        r.resolve_reference("kerb")
    assert r.KERB.height_cm == 15.0
    assert "IRC:103-2012" in r.KERB.citation


def test_mixture_maths() -> None:
    a = r.CATALOGUE[0]
    mean, sd = r.mixture([a, a])
    assert mean == pytest.approx(a.diameter_cm)
    assert sd == pytest.approx(a.sigma_cm)
    with pytest.raises(ValueError):
        r.mixture([])


def test_squash_only_where_verified() -> None:
    assert r.resolve_reference("auto_wheel", vehicle="bajaj_re_auto").squash_cm == pytest.approx(
        1.9
    )
    assert r.resolve_reference("bus_wheel", vehicle="city_bus").squash_cm == pytest.approx(3.2)
    assert r.resolve_reference("car_wheel", vehicle="maruti_swift").squash_cm == 0.0
    assert r.resolve_reference("car_wheel").squash_cm == 0.0


def test_secondary_sources_are_not_marked_verified() -> None:
    from neermattam_perception.references import CATALOGUE

    for e in CATALOGUE:
        if "secondary" in e.note:
            assert not e.verified, e
