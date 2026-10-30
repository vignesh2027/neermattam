"""The person reference (person.py), its synthetic check and its pipeline path,
including the privacy rule: no face leaves the service unblurred."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
import pytest

from neermattam_perception.adapters import SceneObject
from neermattam_perception.geometry import GeometryConfig
from neermattam_perception.media import image_from_rgb
from neermattam_perception.models import MeasureResponse
from neermattam_perception.person import (
    HEAD_TO_SHOULDER,
    LANDMARK_SHARE,
    PersonDecline,
    PersonFailure,
    PersonMeasurement,
    below_band,
    blur_heads,
    box_head_region,
    face_box_region,
    find_shoulder_row,
    fits_box,
    head_region,
    measure_person_depth,
    merged_heads,
    shoulder_band,
    stature_prior,
    water_below,
)
from neermattam_perception.person_synth import draw_figure, evaluate
from neermattam_perception.pipeline import Pipeline, PipelineConfig
from neermattam_perception.references import RefKind
from neermattam_perception.service import _store

from .conftest import FakeMasks, FakeScene, reading_for

FAST = PipelineConfig(geometry=GeometryConfig(mc_draws=10))


def figure(depth_cm: float, seed: int = 3, px_per_cm: float = 3.0):  # type: ignore[no-untyped-def]
    return draw_figure(np.random.default_rng(seed), depth_cm, px_per_cm)


# --------------------------------------------------------------------------- method


def test_stature_prior_is_the_cited_mixture() -> None:
    mean, sd = stature_prior()
    assert mean == pytest.approx((165.0 + 152.5) / 2)
    # within-sex 6.5 cm plus half the 12.5 cm gap between the sexes
    assert sd == pytest.approx(np.hypot(6.5, 12.5 / 2), abs=0.05)
    assert 1.0 - LANDMARK_SHARE["shoulder"] == pytest.approx(HEAD_TO_SHOULDER)


def test_shoulder_row_found_on_a_drawn_figure() -> None:
    f = figure(60.0)
    ys = np.nonzero(f.mask.any(axis=1))[0]
    top = int(ys.min())
    s = find_shoulder_row(f.mask)
    assert s is not None
    # The drawn shoulder is HEAD_TO_SHOULDER of stature (3 px per cm) below the top.
    expected = HEAD_TO_SHOULDER * f.stature_cm * 3.0
    assert s - top == pytest.approx(expected, rel=0.15)


@pytest.mark.parametrize("depth", [25.0, 60.0, 100.0])
def test_depth_from_a_drawn_figure(depth: float) -> None:
    f = figure(depth)
    m = measure_person_depth(f.mask, f.water)
    assert isinstance(m, PersonMeasurement), m
    assert 8.0 <= m.depth_sigma_cm <= 20.0
    assert abs(m.depth_cm - depth) <= 2.5 * m.depth_sigma_cm
    assert m.height_cm == pytest.approx(stature_prior()[0])


def test_landmark_depth_is_reported() -> None:
    f = figure(60.0)
    m = measure_person_depth(f.mask, f.water, landmark="waist")
    assert isinstance(m, PersonMeasurement)
    assert m.landmark == "waist"
    assert m.landmark_depth_cm == pytest.approx(LANDMARK_SHARE["waist"] * m.height_cm)


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"posture": "bending"}, PersonFailure.POSTURE),
        ({"posture": "sitting"}, PersonFailure.POSTURE),
        ({"posture": "swimming"}, PersonFailure.POSTURE),
        ({"posture": "carrying_on_head"}, PersonFailure.POSTURE),
        ({"adult": False}, PersonFailure.CHILD),
    ],
)
def test_declines_postures_it_cannot_measure(kw: dict[str, object], reason: PersonFailure) -> None:
    f = figure(60.0)
    m = measure_person_depth(f.mask, f.water, **kw)  # type: ignore[arg-type]
    assert isinstance(m, PersonDecline) and m.reason == reason
    assert m.text


def test_declines_empty_small_and_dry() -> None:
    f = figure(60.0)
    empty = measure_person_depth(np.zeros_like(f.mask), f.water)
    assert isinstance(empty, PersonDecline) and empty.reason == PersonFailure.MASK_EMPTY
    small = measure_person_depth(f.mask, f.water, min_height_px=10_000)
    assert isinstance(small, PersonDecline) and small.reason == PersonFailure.TOO_SMALL
    dry = measure_person_depth(f.mask, np.zeros_like(f.water))
    assert isinstance(dry, PersonDecline) and dry.reason == PersonFailure.NO_WATER_BELOW


def test_shoulders_found_without_a_visible_neck() -> None:
    """A short neck hidden by a collar: the head's width holds, then the shoulders."""
    m = np.zeros((300, 200), bool)
    m[20:30, 90:110] = True  # crown
    m[30:60, 84:116] = True  # head, 32 px wide, no narrowing below it
    m[60:200, 60:140] = True  # shoulders and body, 80 px wide
    assert find_shoulder_row(m) == pytest.approx(60, abs=3)


def test_slowly_widening_head_is_not_taken_for_shoulders() -> None:
    m = np.zeros((300, 200), bool)
    for y in range(20, 60):  # widens by 1 px a row: never steady
        half = 4 + (y - 20) // 2
        m[y, 100 - half : 100 + half] = True
    m[60:70, 94:106] = True  # neck
    m[70:200, 60:140] = True
    assert find_shoulder_row(m) == pytest.approx(70, abs=3)


def test_shoulder_band_on_sloping_shoulders() -> None:
    """The first wide row is on the slope; the band ends where the shoulders are full."""
    m = np.zeros((300, 200), bool)
    m[20:60, 85:115] = True  # head, 30 px
    m[60:66, 92:108] = True  # neck
    for y in range(66, 86):  # trapezius: 16 px to 80 px over 20 rows
        half = 8 + round((y - 66) * 32 / 20)
        m[y, 100 - half : 100 + half] = True
    m[86:220, 60:140] = True
    first = find_shoulder_row(m)
    assert first is not None and 66 < first < 80
    assert shoulder_band(m, first) == pytest.approx(83, abs=3)
    square = np.zeros_like(m)
    square[20:60, 85:115] = True
    square[60:220, 60:140] = True
    f2 = find_shoulder_row(square)
    assert f2 is not None and shoulder_band(square, f2) == pytest.approx(f2, abs=3)


def test_two_heads_in_one_outline_are_declined() -> None:
    m = np.zeros((300, 300), bool)
    m[20:60, 80:110] = True  # this person's head
    m[25:60, 130:160] = True  # a neighbour's head, joined at the shoulders
    m[60:200, 60:180] = True
    water = np.zeros_like(m)
    water[200:] = True
    first = find_shoulder_row(m)
    assert first is not None and merged_heads(m, first)
    r = measure_person_depth(m, water)
    assert isinstance(r, PersonDecline) and r.reason == PersonFailure.MERGED
    f = figure(60.0)
    s = find_shoulder_row(f.mask)
    assert s is not None and not merged_heads(f.mask, s)


def test_fits_box() -> None:
    m = np.zeros((300, 200), bool)
    m[100:200, 80:120] = True
    assert fits_box(m, (70, 95, 130, 205))
    assert not fits_box(m, (70, 95, 130, 300))  # a fragment of a taller person
    assert not fits_box(m, (70, 20, 130, 200))


def test_person_fragment_is_declined(easy_case) -> None:  # type: ignore[no-untyped-def]
    p, f, rgb, _ = person_pipeline(easy_case, 70.0)
    ys = np.nonzero(f.mask.any(axis=1))[0]
    upper = f.mask.copy()  # head and chest only: a measurable outline, but a fragment
    upper[int(ys.min()) + int(0.55 * (ys.max() - ys.min())) :] = False
    p.masks.by_id[30] = upper  # type: ignore[attr-defined]
    p.masks.by_id[50] = np.ones_like(f.mask)  # type: ignore[attr-defined]  # water below it
    r = run(p, image=image_from_rgb(rgb))
    assert r.method != "person_reference"
    assert any("does not match the person" in x for x in r.reasons)


def test_water_below_band() -> None:
    f = figure(60.0)
    assert water_below(f.mask, f.water) > 0.9
    assert water_below(f.mask, np.zeros_like(f.water)) == 0.0
    band = below_band(f.mask)
    assert band is not None and band[1] > np.nonzero(f.mask.any(axis=1))[0].max()


def test_declines_a_blob_without_shoulders() -> None:
    m = np.zeros((300, 200), bool)
    m[20:200, 80:120] = True  # a post, not a person
    water = np.zeros_like(m)
    water[200:, :] = True
    r = measure_person_depth(m, water)
    assert isinstance(r, PersonDecline) and r.reason == PersonFailure.NO_SHOULDERS


def test_synthetic_check_is_calibrated() -> None:
    r = evaluate(60, seed=5)
    assert r["measured"] >= 55
    assert r["mae_cm"] < 10.0
    assert r["within_2_sigma"] >= 0.9


# --------------------------------------------------------------------------- privacy


def test_blur_heads_destroys_detail_only_inside_the_region() -> None:
    rgb = np.zeros((200, 200, 3), np.uint8)
    rgb[(np.indices((200, 200)).sum(axis=0) // 4) % 2 == 0] = 255  # fine checkerboard
    out = blur_heads(rgb, [(50, 40, 130, 120)])
    inside = out[50:110, 60:120].astype(float)
    assert inside.std() < 0.15 * rgb[50:110, 60:120].astype(float).std()
    assert np.array_equal(out[150:, :], rgb[150:, :])
    assert np.array_equal(out[:, :40], rgb[:, :40])
    # Regions reaching past the image edge are clipped, not an error.
    blur_heads(rgb, [(-10, -10, 30, 30), (190, 190, 260, 260)])


def test_head_region_covers_the_head() -> None:
    f = figure(60.0)
    s = find_shoulder_row(f.mask)
    x0, y0, x1, y1 = head_region(f.mask, s)
    head = f.mask[: s - 3]
    ys, xs = np.nonzero(head)
    assert x0 <= xs.min() and xs.max() < x1 and y0 <= ys.min() and ys.max() < y1
    bx = box_head_region((10, 20, 110, 420))
    assert bx == (10, 0, 110, 220)  # reaches above a loose box


# --------------------------------------------------------------------------- pipeline


def person_scene(depth_cm: float, seed: int = 3, px_per_cm: float = 1.5):  # type: ignore[no-untyped-def]
    """A photo of one person standing in water, the face a fine checkerboard."""
    f = figure(depth_cm, seed, px_per_cm)
    h, w = f.mask.shape
    rgb = np.full((h, w, 3), 120, np.uint8)
    rgb[f.water] = (60, 90, 140)
    rgb[f.mask] = (180, 140, 110)
    s = find_shoulder_row(f.mask)
    assert s is not None
    face = np.zeros_like(f.mask)
    face[:s] = f.mask[:s]
    checker = (np.indices((h, w)).sum(axis=0) // 3) % 2 == 0
    rgb[face & checker] = 255
    rgb[face & ~checker] = 0
    return f, rgb, face


def person_object(mask: np.ndarray, **kw: object) -> SceneObject:
    ys, xs = np.nonzero(mask)
    box = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
    cx = int(np.median(xs))
    top = int(ys.min())
    return SceneObject(
        RefKind.PERSON,
        (cx, int(np.median(ys))),
        box,
        head_top=(cx, top + 2),
        posture="standing",
        **kw,  # type: ignore[arg-type]
    )


def person_pipeline(easy_case, depth_cm: float, **kw: object):  # type: ignore[no-untyped-def]
    f, rgb, face = person_scene(depth_cm)
    base = reading_for(easy_case)
    wy, wx = np.nonzero(f.water)
    pts = tuple((int(wx[i]), int(wy[i])) for i in (0, len(wx) // 2, len(wx) - 1))
    reading = replace(
        base,
        references=(person_object(f.mask, **kw),),
        water_points=pts,
        waterline_points=(),
    )
    masks = FakeMasks({30: f.mask, 16: f.water})
    return Pipeline(FakeScene(reading), masks, FAST), f, rgb, face


def run(p: Pipeline, **kw: object) -> MeasureResponse:
    return asyncio.run(p.measure(**kw))  # type: ignore[arg-type]


def decode(b64: str | None) -> np.ndarray:
    png = base64.b64decode(b64 or "")
    bgr = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)


def face_detail(rgb: np.ndarray, face: np.ndarray) -> float:
    """Share of the face that still shows the checkerboard: pure black or pure white
    pixels (the overlay's axis and outlines are coloured, so they do not count)."""
    inner = cv2.erode(face.astype(np.uint8), np.ones((5, 5), np.uint8)).astype(bool)
    assert inner.sum() > 50
    px = rgb[inner].astype(int)
    black = (px < 40).all(axis=1)
    white = (px > 215).all(axis=1)
    return float((black | white).mean())


def test_person_reference_path(easy_case) -> None:  # type: ignore[no-untyped-def]
    p, _f, rgb, _ = person_pipeline(easy_case, 70.0, landmark="hip")
    r = run(p, image=image_from_rgb(rgb))
    assert r.usable, r.reasons
    assert r.method == "person_reference"
    assert r.depth_cm is not None and r.depth_sigma_cm is not None
    assert abs(r.depth_cm - 70.0) <= 2.5 * r.depth_sigma_cm
    assert 8.0 <= r.depth_sigma_cm <= 25.0
    assert r.reference is not None and r.reference.kind == "person"
    assert r.reference.diameter_cm is None
    assert r.reference.height_cm == pytest.approx(stature_prior()[0], abs=0.1)
    assert r.reference.source.startswith("prior:")
    assert r.waterline is not None and r.waterline.method == "person_mask_bottom"
    assert r.waterline.agreement_cm is not None
    assert any("10 to 15 cm" in x for x in r.reasons)
    assert 0 < r.confidence < 0.6
    assert any(s.target == "person 1" for s in r.agent_trace)


def test_person_overlay_and_store_never_show_the_face(easy_case, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    p, _, rgb, face = person_pipeline(easy_case, 70.0)
    image = image_from_rgb(rgb)
    r = run(p, image=image)
    assert r.method == "person_reference" and r._faces
    before = face_detail(image.rgb, face)
    assert before > 0.6
    overlay = decode(r.overlay_png_b64)
    assert overlay.shape[:2] == rgb.shape[:2]
    assert face_detail(overlay, face) < 0.05
    _store(tmp_path, r, image)
    stored = cv2.cvtColor(cv2.imread(str(tmp_path / f"{r.measurement_id}.jpg")), cv2.COLOR_BGR2RGB)
    assert face_detail(stored, face) < 0.05
    # Body pixels well below the head are left alone.
    assert np.abs(stored[-60:].astype(int) - image.rgb[-60:].astype(int)).mean() < 6


def test_person_face_blurred_even_on_the_estimate_path(easy_case) -> None:  # type: ignore[no-untyped-def]
    p, _, rgb, face = person_pipeline(easy_case, 70.0)
    p.scene.reading = replace(  # type: ignore[attr-defined]
        p.scene.reading,  # type: ignore[attr-defined]
        references=(replace(p.scene.reading.references[0], posture="bending"),),  # type: ignore[attr-defined]
        overall_depth_cm=60.0,
    )
    image = image_from_rgb(rgb)
    r = run(p, image=image)
    assert r.method == "model_estimate"
    assert any("bending" in x for x in r.reasons)
    assert face_detail(image.rgb, face) > 0.6
    assert face_detail(decode(r.overlay_png_b64), face) < 0.05


def side_by_side(easy_case, person_depth_cm: float):  # type: ignore[no-untyped-def]
    """The synthetic wheel scene with a person standing in water to its right."""
    f, prgb, _ = person_scene(person_depth_cm)
    hw, ww = easy_case.image.shape[:2]
    hp, wp = f.mask.shape
    assert hp <= hw
    rgb = np.full((hw, ww + wp, 3), 120, np.uint8)
    rgb[:, :ww] = easy_case.image
    rgb[:hp, ww:] = prgb

    def place(left: np.ndarray | None, right: np.ndarray | None) -> np.ndarray:
        out = np.zeros((hw, ww + wp), bool)
        if left is not None:
            out[:, :ww] = left
        if right is not None:
            out[:hp, ww:] = right
        return out

    base = reading_for(easy_case)
    pobj = person_object(place(None, f.mask))
    reading = replace(base, references=(*base.references, pobj))
    masks = FakeMasks(
        {
            1: place(easy_case.wheel_mask, None),
            2: place(easy_case.rim_mask, None),
            16: place(easy_case.water_mask, f.water),
            30: place(None, f.mask),
        }
    )
    return Pipeline(FakeScene(reading), masks, FAST), image_from_rgb(rgb)


def test_wheel_preferred_and_agreeing_person_noted(easy_case) -> None:  # type: ignore[no-untyped-def]
    p, image = side_by_side(easy_case, easy_case.depth_cm)
    r = run(p, image=image, hint_diameter_cm=43.4)
    assert r.method == "wheel_geometry"
    assert any("consistent with the wheel" in x for x in r.reasons)
    assert r.confidence > 0.3


def test_wheel_and_person_disagreeing_lowers_confidence(easy_case) -> None:  # type: ignore[no-untyped-def]
    agree, image = side_by_side(easy_case, easy_case.depth_cm)
    base = run(agree, image=image, hint_diameter_cm=43.4)
    p, image = side_by_side(easy_case, 90.0)
    r = run(p, image=image, hint_diameter_cm=43.4)
    assert r.method == "wheel_geometry"
    assert r.depth_cm == pytest.approx(base.depth_cm, abs=0.5)
    assert any("more than two sigma from the wheel" in x for x in r.reasons)
    assert r.confidence == pytest.approx(base.confidence * 0.5, abs=0.011)


def test_person_hint_uses_the_person(easy_case) -> None:  # type: ignore[no-untyped-def]
    p, image = side_by_side(easy_case, 60.0)
    r = run(p, image=image, hint_reference="person")
    assert r.method == "person_reference"


def test_person_measured_when_verified_water_stops_short(easy_case) -> None:  # type: ignore[no-untyped-def]
    """The verified water misses the strip at the legs: water is segmented below."""
    p, f, rgb, _ = person_pipeline(easy_case, 70.0)
    ys = np.nonzero(f.mask.any(axis=1))[0]
    short = f.water.copy()
    short[: int(ys.max()) + 30] = False  # verified water starts 30 rows below the legs
    p.masks.by_id[16] = short  # type: ignore[attr-defined]
    for i in range(3):
        p.masks.by_id[50 + i] = f.water  # type: ignore[attr-defined]
    r = run(p, image=image_from_rgb(rgb))
    assert r.method == "person_reference", r.reasons
    assert any(s.target == "water below people" for s in r.agent_trace)
    assert any("segmented locally" in x for x in r.reasons)


def test_scene_face_boxes_are_blurred(easy_case) -> None:  # type: ignore[no-untyped-def]
    """A face the scene model boxed on someone who is not measured is blurred too."""
    p, _, rgb, _ = person_pipeline(easy_case, 70.0)
    h, w = rgb.shape[:2]
    by = np.zeros((h, w), bool)
    by[60:80, 185:205] = True  # clear of the overlay's legend at the top left
    checker = (np.indices((h, w)).sum(axis=0) // 3) % 2 == 0
    rgb[by & checker] = 255
    rgb[by & ~checker] = 0
    p.scene.reading = replace(p.scene.reading, faces=((185, 60, 205, 80),))  # type: ignore[attr-defined]
    image = image_from_rgb(rgb)
    assert face_detail(image.rgb, by) > 0.6
    r = run(p, image=image)
    assert face_detail(decode(r.overlay_png_b64), by) < 0.05
    assert face_box_region((185, 60, 205, 80)) in r._faces
