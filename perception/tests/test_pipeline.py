from __future__ import annotations

import asyncio
import base64
import json
import re
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
import pytest

from neermattam_perception.geometry import GeometryConfig
from neermattam_perception.media import VideoInput
from neermattam_perception.models import MeasureResponse
from neermattam_perception.pipeline import Pipeline, PipelineConfig
from neermattam_perception.references import RefKind

from .conftest import FakeMasks, FakeScene, case_image, reading_for

FAST = PipelineConfig(geometry=GeometryConfig(mc_draws=10))


def run(p: Pipeline, **kw: object) -> MeasureResponse:
    return asyncio.run(p.measure(**kw))  # type: ignore[arg-type]


def make_pipeline(case, **reading_kw) -> tuple[Pipeline, FakeScene, FakeMasks]:  # type: ignore[no-untyped-def]
    scene = FakeScene(reading_for(case, **reading_kw))
    masks = FakeMasks({1: case.wheel_mask, 2: case.rim_mask, 16: case.water_mask})
    return Pipeline(scene, masks, FAST), scene, masks


def test_end_to_end_on_synthetic_scene(easy_case) -> None:  # type: ignore[no-untyped-def]
    p, _, masks = make_pipeline(easy_case)
    r = run(p, image=case_image(easy_case), hint_diameter_cm=43.4)
    assert r.usable, r.reasons
    assert r.depth_cm == pytest.approx(easy_case.depth_cm, abs=2.0)
    assert r.depth_sigma_cm and r.depth_sigma_cm > 0
    assert r.reference is not None and r.reference.source == "hint"
    assert r.waterline is not None and r.waterline.method == "sam_mask_intersection"
    assert r.waterline.agreement_cm is not None and r.waterline.agreement_cm < 2.0
    assert 0 < r.confidence <= 1
    assert r.models.scene == "muse-spark-1.3" and r.models.masks == "sam-3.1"
    assert set(r.timings_ms) >= {"scene", "masks", "geometry"}
    assert r.cost_usd == pytest.approx(0.0025)
    assert "standing water visible" in r.reasons
    # One SAM request carried the wheel, its rim and the water.
    assert re.match(r"<0f>1<\|box;.*\|>.*,2<\|point;.*,16<\|point;", masks.last_prompt)
    png = base64.b64decode(r.overlay_png_b64 or "")
    img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    assert img is not None and img.shape[0] > 100


def test_catalogue_reference_used_without_hint(easy_case) -> None:  # type: ignore[no-untyped-def]
    p, scene, _ = make_pipeline(easy_case)
    ref = replace(scene.reading.references[0], vehicle="honda_activa_110", position="rear")
    scene.reading = replace(scene.reading, references=(ref,))
    r = run(p, image=case_image(easy_case))
    assert r.usable and r.reference is not None
    assert r.reference.source == "catalogue:90/100-10" and r.reference.diameter_cm == 43.4


def test_response_matches_contract_keys(easy_case) -> None:  # type: ignore[no-untyped-def]
    contracts = Path(__file__).resolve().parents[2] / "docs" / "CONTRACTS.md"
    section = (
        contracts.read_text().split("### POST /v1/measure")[1].split("### POST /v1/transcribe")[0]
    )
    example = json.loads(re.search(r"```json\n(.*?)```", section, re.S).group(1))  # type: ignore[union-attr]
    p, _, _ = make_pipeline(easy_case)
    r = run(p, image=case_image(easy_case)).model_dump()
    assert set(r) == set(example)
    assert set(r["reference"]) == set(example["reference"])
    assert set(r["waterline"]) == set(example["waterline"])
    assert set(r["models"]) == set(example["models"])
    assert r["measurement_id"].startswith("m_") and len(r["measurement_id"]) == 28


def test_no_water(easy_case) -> None:  # type: ignore[no-untyped-def]
    p, _, _ = make_pipeline(easy_case, water=False)
    r = run(p, image=case_image(easy_case))
    assert not r.usable and r.depth_cm is None
    assert r.reasons == ["no standing water visible"]


def test_no_reference(easy_case) -> None:  # type: ignore[no-untyped-def]
    p, scene, _ = make_pipeline(easy_case)
    scene.reading = replace(scene.reading, references=())
    r = run(p, image=case_image(easy_case))
    assert not r.usable and "no wheel or person found to measure against" in r.reasons


def test_kerb_hint_is_declined_honestly(easy_case) -> None:  # type: ignore[no-untyped-def]
    p, _, _ = make_pipeline(easy_case)
    r = run(p, image=case_image(easy_case), hint_reference="kerb")
    assert not r.usable and any("not supported yet" in x for x in r.reasons)


def test_blurred_image(easy_case) -> None:  # type: ignore[no-untyped-def]
    p, scene, _ = make_pipeline(easy_case)
    scene.reading = replace(
        scene.reading,
        quality=replace(scene.reading.quality, score=0.1, blurred=True, issues=("motion blur",)),
    )
    r = run(p, image=case_image(easy_case))
    assert not r.usable and "image issue: motion blur" in r.reasons
    assert {"code": "image_issue", "args": {"detail": "motion blur"}} in [
        c.model_dump() for c in r.reason_codes
    ]


def test_wheel_without_mask_reports_submerged(easy_case) -> None:  # type: ignore[no-untyped-def]
    p, _, masks = make_pipeline(easy_case)
    del masks.by_id[1]
    r = run(p, image=case_image(easy_case))
    assert not r.usable and any("fully submerged" in x for x in r.reasons)


def test_geometry_failure_reason_surfaces(easy_case) -> None:  # type: ignore[no-untyped-def]
    p, _, masks = make_pipeline(easy_case)
    masks.by_id[16] = np.zeros_like(easy_case.water_mask)
    masks.by_id[16][0:5, 0:5] = True  # water far from the wheel
    r = run(p, image=case_image(easy_case))
    assert not r.usable and any("water does not meet the wheel" in x for x in r.reasons)


def test_prefers_hinted_kind(easy_case) -> None:  # type: ignore[no-untyped-def]
    p, scene, masks = make_pipeline(easy_case)
    car = replace(scene.reading.references[0], kind=RefKind.CAR_WHEEL)
    scene.reading = replace(scene.reading, references=(car, scene.reading.references[0]))
    masks.by_id[3] = easy_case.wheel_mask
    r = run(p, image=case_image(easy_case), hint_reference="two_wheeler_wheel")
    assert r.usable and r.reference is not None and r.reference.kind == "two_wheeler_wheel"


def test_video_velocity(easy_case) -> None:  # type: ignore[no-untyped-def]
    p, _, masks = make_pipeline(easy_case)
    key = case_image(easy_case)
    # Two debris tracks moving 30 px per frame at 30 fps in the key-frame scale.
    masks.tracks = {
        "1": [(f, 50.0 + 30.0 * f, 400.0) for f in range(20)],
        "2": [(f, 60.0 + 31.0 * f, 420.0) for f in range(20)],
    }
    video = VideoInput(b"", "video/mp4", 30.0, 60, key.width, key.height, 0, key)
    r = run(p, video=video)
    assert r.usable, r.reasons
    assert r.velocity_mps is not None and r.velocity_sigma_mps is not None
    m_cm_per_px = 43.4 / 2.0  # order of magnitude only: cm per px is about D / (2a)
    assert 0 < r.velocity_mps < m_cm_per_px
    assert "velocity" in r.timings_ms
    assert any("surface velocity" in x for x in r.reasons)
