"""The verify-and-refine agent and the open-weights pipeline.

A fake SAM and a scripted verifier stand in for the models.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np
import pytest

from neermattam_perception.adapters import MaskResult, ModelError, SceneObject, SceneReading
from neermattam_perception.agent import VerifyRefineAgent, crop_rect
from neermattam_perception.geometry import GeometryConfig
from neermattam_perception.media import ImageInput
from neermattam_perception.models import MeasureResponse
from neermattam_perception.muse_open import JsonReply, Usage
from neermattam_perception.pipeline import (
    ESTIMATE_MIN_SIGMA_CM,
    Pipeline,
    PipelineConfig,
    estimate_sigma,
)
from neermattam_perception.references import RefKind
from neermattam_perception.sam import ObjectMask, ObjectPrompt
from neermattam_perception.sam_local import mask_box

from .conftest import FakeScene, case_image, reading_for

FAST = PipelineConfig(geometry=GeometryConfig(mc_draws=10))
Box = tuple[int, int, int, int]


def run(p: Pipeline, **kw: object) -> MeasureResponse:
    return asyncio.run(p.measure(**kw))  # type: ignore[arg-type]


def box_of(mask: np.ndarray) -> Box:
    b = mask_box(mask)
    assert b is not None
    return b


def rect_mask(shape: tuple[int, int], box: Box) -> np.ndarray:
    m = np.zeros(shape, dtype=bool)
    m[box[1] : box[3], box[0] : box[2]] = True
    return m


@dataclass
class BoxSam:
    """Fake SAM: a prompt box overlapping a known object returns that object's mask;
    any other box returns the box itself (a mask on the wrong thing)."""

    objects: dict[int, np.ndarray]
    shape: tuple[int, int]
    mask_model_name: str = "fake-sam"
    name: str = "fake-sam"
    prompts: list[ObjectPrompt] = field(default_factory=list)
    last: dict[int, tuple[Box | None, Box | None]] = field(default_factory=dict)

    async def segment(self, image: ImageInput, objects: Sequence[ObjectPrompt]) -> MaskResult:
        out = {}
        for o in objects:
            self.prompts.append(o)
            truth = self.objects.get(o.object_id)
            m = None
            if truth is not None:
                tb = box_of(truth)
                if o.box is None or (
                    min(o.box[2], tb[2]) > max(o.box[0], tb[0])
                    and min(o.box[3], tb[3]) > max(o.box[1], tb[1])
                ):
                    m = truth
            if m is None and o.box is not None:
                m = rect_mask(self.shape, o.box)
            if m is not None:
                out[o.object_id] = ObjectMask(str(o.object_id), 0, m, box_of(m), 0.9)
                self.last[o.object_id] = (o.box, box_of(m))
        return MaskResult(out)

    async def track(self, video, concept):  # type: ignore[no-untyped-def]
        raise ModelError("no tracking")

    async def ready(self) -> tuple[bool, str]:
        return True, "ok"


Script = Callable[[str, int], dict[str, Any]]


@dataclass
class ScriptedVerifier:
    """Answers each check from ``script(what, n)``, where ``n`` counts calls per target."""

    script: Script
    scene_model_name: str = "muse-spark-open"
    calls: list[str] = field(default_factory=list)
    images: list[int] = field(default_factory=list)

    async def ask_image(self, jpeg: bytes, prompt: str, *, what: str) -> JsonReply:
        images = [jpeg] if isinstance(jpeg, bytes) else list(jpeg)
        assert images and all(j[:2] == b"\xff\xd8" for j in images)  # real JPEGs
        self.images.append(len(images))
        n = sum(1 for c in self.calls if c == what)
        self.calls.append(what)
        return JsonReply(self.script(what, n), Usage(1000, 500), 1234, "reasoning")


GOOD = {"is_target": True, "outline_quality": "good", "view": "side", "tyre_edge_visible": 0.6}
WATER_OK = {"is_water": True, "coverage": "good"}


def grid_in_rect(box: Box, rect: Box) -> list[int]:
    """A pixel box as the verifier would give it: on the 0 to 1000 grid of the crop."""
    left, top, right, bottom = rect
    w, h = right - left, bottom - top
    return [
        round((box[0] - left) * 1000 / w),
        round((box[1] - top) * 1000 / h),
        round((box[2] - left) * 1000 / w),
        round((box[3] - top) * 1000 / h),
    ]


# --------------------------------------------------------------------------- agent loop


def test_accepts_a_good_mask_first_time(easy_case) -> None:  # type: ignore[no-untyped-def]
    img = case_image(easy_case)
    sam = BoxSam({1: easy_case.wheel_mask}, easy_case.wheel_mask.shape)
    ver = ScriptedVerifier(lambda what, n: GOOD)
    agent = VerifyRefineAgent(ver, sam)
    obj = reading_for(easy_case).references[0]
    out = asyncio.run(agent.refine_reference(img, obj, 1, 1))
    assert out.accepted and out.refinements == 0
    assert out.mask is not None and np.array_equal(out.mask.mask, easy_case.wheel_mask)
    assert [t.step for t in out.trace] == ["segment", "verify"]
    assert out.trace[-1].decision == "accept"
    assert out.cost_usd == pytest.approx(Usage(1000, 500).cost_usd)


def test_refines_a_wrong_box_then_accepts(easy_case) -> None:  # type: ignore[no-untyped-def]
    """The probe's failure: the box landed next to the wheel. The verifier corrects it."""
    img = case_image(easy_case)
    shape = easy_case.wheel_mask.shape
    sam = BoxSam({1: easy_case.wheel_mask}, shape)
    truth = box_of(easy_case.wheel_mask)
    width = truth[2] - truth[0]
    wrong = (truth[2] + 10, truth[1], truth[2] + 10 + width, truth[3])  # a "water tank" beside it

    def script(what: str, n: int) -> dict[str, Any]:
        if n == 0:
            asked, got = sam.last[1]
            rect = crop_rect(img.width, img.height, asked, got)
            return {
                "is_target": False,
                "outline_quality": "wrong",
                "corrected_box": grid_in_rect(truth, rect),
                "reason": "outline is on the blue tank next to the wheel",
            }
        return {**GOOD, "waterline": [[100, 600], [900, 610]]}

    agent = VerifyRefineAgent(ScriptedVerifier(script), sam)
    obj = replace(
        reading_for(easy_case).references[0], box=wrong, point=(wrong[0] + 5, wrong[1] + 5)
    )
    out = asyncio.run(agent.refine_reference(img, obj, 1, 1))
    assert out.accepted and out.refinements == 1
    assert [t.decision.split(" ")[0] for t in out.trace] == ["verify", "refine", "verify", "accept"]
    second = sam.prompts[1]
    assert second.box is not None and second.points == ()  # the suspect point is dropped
    assert all(abs(a - b) <= 3 for a, b in zip(second.box, truth, strict=True)), (second.box, truth)
    assert np.array_equal(out.mask.mask, easy_case.wheel_mask)  # type: ignore[union-attr]
    assert len(out.waterline_points) == 2


def test_gives_up_after_two_refinements(easy_case) -> None:  # type: ignore[no-untyped-def]
    img = case_image(easy_case)
    sam = BoxSam({}, easy_case.wheel_mask.shape)  # never finds the wheel

    def script(what: str, n: int) -> dict[str, Any]:
        return {
            "is_target": False,
            "outline_quality": "wrong",
            "corrected_box": [100 + 50 * n, 100, 300 + 50 * n, 300],
        }

    agent = VerifyRefineAgent(ScriptedVerifier(script), sam)
    out = asyncio.run(agent.refine_reference(img, reading_for(easy_case).references[0], 1, 1))
    assert not out.accepted and out.mask is None
    assert [t.step for t in out.trace] == ["segment", "verify"] * 3
    assert out.trace[-1].decision == "give up"
    assert sum(1 for t in out.trace if t.decision.startswith("refine")) == 2


def test_gives_up_on_a_head_on_tyre(easy_case) -> None:  # type: ignore[no-untyped-def]
    img = case_image(easy_case)
    sam = BoxSam({1: easy_case.wheel_mask}, easy_case.wheel_mask.shape)
    ver = ScriptedVerifier(lambda what, n: {**GOOD, "view": "head_on"})
    out = asyncio.run(
        VerifyRefineAgent(ver, sam).refine_reference(
            img, reading_for(easy_case).references[0], 1, 1
        )
    )
    assert not out.accepted and len(ver.calls) == 1
    assert out.trace[-1].decision.startswith("give up: tyre seen head-on")


def test_water_is_refined_with_a_corrected_box(easy_case) -> None:  # type: ignore[no-untyped-def]
    img = case_image(easy_case)
    shape = easy_case.water_mask.shape
    sam = BoxSam({16: easy_case.water_mask}, shape)
    wb = box_of(easy_case.water_mask)
    h, w = shape

    def script(what: str, n: int) -> dict[str, Any]:
        if n == 0:
            grid = [
                round(wb[0] * 1000 / w),
                round(wb[1] * 1000 / h),
                round(wb[2] * 1000 / w),
                round(wb[3] * 1000 / h),
            ]
            return {"is_water": False, "coverage": "wrong", "corrected_box": grid}
        return WATER_OK

    agent = VerifyRefineAgent(ScriptedVerifier(script), sam)
    out = asyncio.run(agent.refine_water(img, (0, 0, 30, 30), [], 16))  # starts on the sky
    assert out.accepted and out.refinements == 1
    assert np.array_equal(out.mask.mask, easy_case.water_mask)  # type: ignore[union-attr]
    assert [t.decision.split(" ")[0] for t in out.trace] == ["verify", "refine", "verify", "accept"]


def test_verifier_failure_is_a_traced_give_up(easy_case) -> None:  # type: ignore[no-untyped-def]
    @dataclass
    class Broken:
        scene_model_name: str = "muse-spark-open"

        async def ask_image(self, jpeg: bytes, prompt: str, *, what: str) -> JsonReply:
            raise ModelError("scene model failed with HTTP 503")

    sam = BoxSam({1: easy_case.wheel_mask}, easy_case.wheel_mask.shape)
    out = asyncio.run(
        VerifyRefineAgent(Broken(), sam).refine_reference(
            case_image(easy_case), reading_for(easy_case).references[0], 1, 1
        )
    )
    assert not out.accepted and "HTTP 503" in out.trace[-1].output


# --------------------------------------------------------------------------- pipeline


def open_reading(case, **kw) -> SceneReading:  # type: ignore[no-untyped-def]
    r = reading_for(case, **kw)
    return replace(
        r,
        overall_depth_cm=20.0,
        overall_low_cm=10.0,
        overall_high_cm=30.0,
        overall_reason="water at the lower part of the scooter wheel",
        water_box=box_of(case.water_mask),
    )


def agent_pipeline(case, reading: SceneReading, script: Script | None = None):  # type: ignore[no-untyped-def]
    sam = BoxSam({1: case.wheel_mask, 2: case.rim_mask, 16: case.water_mask}, case.wheel_mask.shape)

    def default(what: str, n: int) -> dict[str, Any]:
        return WATER_OK if what == "verify water" else GOOD

    ver = ScriptedVerifier(script or default)
    scene = FakeScene(reading, scene_model_name="muse-spark-open")
    return Pipeline(scene, sam, FAST, agent=VerifyRefineAgent(ver, sam)), sam, ver


def test_open_pipeline_measures_and_traces(easy_case) -> None:  # type: ignore[no-untyped-def]
    p, sam, _ = agent_pipeline(easy_case, open_reading(easy_case))
    r = run(p, image=case_image(easy_case), hint_diameter_cm=43.4)
    assert r.usable and r.method == "wheel_geometry", r.reasons
    assert r.depth_cm == pytest.approx(easy_case.depth_cm, abs=2.0)
    steps = [(t.step, t.target) for t in r.agent_trace]
    assert steps[0] == ("scene", "photo")
    assert ("verify", "water") in steps and ("verify", "two wheeler wheel 1") in steps
    assert ("geometry", "two wheeler wheel 1") in steps
    assert (
        r.agent_trace[-1].step == "estimate" and r.agent_trace[-1].decision == "use the measurement"
    )
    assert r.model_estimate is not None and r.model_estimate.depth_cm == 20.0
    assert r.model_estimate.depth_sigma_cm == ESTIMATE_MIN_SIGMA_CM
    assert "agent" in r.timings_ms and r.cost_usd == pytest.approx(2 * Usage(1000, 500).cost_usd)
    # The accepted wheel is a solid shape; no separate rim mask on this path.
    assert not any(o.object_id == 2 for o in sam.prompts)
    assert r.captured_at is None


def test_rejected_wheels_fall_back_to_the_model_estimate(easy_case) -> None:  # type: ignore[no-untyped-def]
    def script(what: str, n: int) -> dict[str, Any]:
        if what == "verify water":
            return WATER_OK
        return {"is_target": False, "outline_quality": "wrong", "reason": "this is a shadow"}

    p, _, _ = agent_pipeline(easy_case, open_reading(easy_case), script)
    r = run(p, image=case_image(easy_case))
    assert r.usable and r.method == "model_estimate"
    assert r.depth_cm == 20.0 and r.depth_sigma_cm == 20.0
    assert r.reference is None and r.waterline is None
    assert any("did not confirm" in x for x in r.reasons)
    assert any("not a measurement" in x for x in r.reasons)
    assert r.confidence <= 0.25
    assert r.overlay_png_b64


def test_people_only_falls_back_to_a_labelled_estimate(easy_case) -> None:  # type: ignore[no-untyped-def]
    base = open_reading(easy_case)
    person = SceneObject(
        RefKind.PERSON, (50, 50), (40, 20, 80, 120), depth_cm=100.0, depth_reason="chest deep"
    )
    reading = replace(
        base,
        references=(person,),
        overall_depth_cm=100.0,
        overall_low_cm=None,
        overall_high_cm=None,
    )
    p, sam, _ = agent_pipeline(easy_case, reading)
    r = run(p, image=case_image(easy_case))
    assert r.usable and r.method == "model_estimate"
    assert r.depth_cm == 100.0 and r.depth_sigma_cm == 30.0  # 30 percent beats 20 cm here
    # The person is segmented (the fake SAM gives a box, which has no shoulders),
    # the measurement declines, and the scene model's estimate is reported.
    assert any(o.object_id == 30 for o in sam.prompts)
    assert any("person 1" in x for x in r.reasons)
    assert r.agent_trace[0].decision.startswith("no wheel: measure 1 person")


def test_no_estimate_and_no_wheel_is_unusable(easy_case) -> None:  # type: ignore[no-untyped-def]
    reading = replace(reading_for(easy_case), references=())
    p, _, _ = agent_pipeline(easy_case, reading)
    r = run(p, image=case_image(easy_case))
    assert not r.usable and r.method is None and r.depth_cm is None


def test_estimate_sigma_rule() -> None:
    assert estimate_sigma(10.0) == 20.0
    assert estimate_sigma(100.0) == pytest.approx(30.0)
    assert estimate_sigma(50.0, 20.0, 120.0) == 50.0


def test_disagreement_with_the_estimate_lowers_confidence(easy_case) -> None:  # type: ignore[no-untyped-def]
    agree, _, _ = agent_pipeline(easy_case, open_reading(easy_case))
    far = replace(open_reading(easy_case), overall_depth_cm=150.0)
    disagree, _, _ = agent_pipeline(easy_case, far)
    a = run(agree, image=case_image(easy_case), hint_diameter_cm=43.4)
    d = run(disagree, image=case_image(easy_case), hint_diameter_cm=43.4)
    assert a.method == d.method == "wheel_geometry"
    assert d.depth_cm == a.depth_cm  # the map uses the geometry
    assert d.confidence < a.confidence
    assert any("more than two sigma" in x for x in d.reasons)
    assert not any("more than two sigma" in x for x in a.reasons)


def test_waterline_disagreement_widens_sigma(easy_case) -> None:  # type: ignore[no-untyped-def]
    base = open_reading(easy_case)
    good, _, _ = agent_pipeline(easy_case, base)
    shifted = tuple((x, y - 25) for x, y in base.waterline_points)
    bad_ref = replace(base.references[0], waterline_points=shifted)
    bad, _, _ = agent_pipeline(
        easy_case, replace(base, references=(bad_ref,), waterline_points=shifted)
    )
    g = run(good, image=case_image(easy_case), hint_diameter_cm=43.4)
    b = run(bad, image=case_image(easy_case), hint_diameter_cm=43.4)
    assert (
        g.waterline
        and b.waterline
        and b.waterline.agreement_cm
        and g.waterline.agreement_cm is not None
    )
    assert b.waterline.agreement_cm > 5.0
    assert b.depth_sigma_cm >= b.waterline.agreement_cm - 0.05  # type: ignore[operator]
    assert b.depth_sigma_cm == pytest.approx(  # added in quadrature
        math.hypot(g.depth_sigma_cm or 0.0, b.waterline.agreement_cm), abs=0.3
    )


def test_squash_correction_adds_its_own_sigma(easy_case) -> None:  # type: ignore[no-untyped-def]
    reading = open_reading(easy_case)
    auto = replace(reading.references[0], kind=RefKind.AUTO_WHEEL, vehicle="bajaj_re_auto")
    no_wl = replace(reading, references=(replace(auto, waterline_points=()),), waterline_points=())
    p, _, _ = agent_pipeline(easy_case, no_wl)
    r = run(p, image=case_image(easy_case))
    assert r.usable and r.reference is not None and r.reference.kind == "auto_wheel"
    assert f"tyre squash of 1.9 cm subtracted (sigma {0.5 * 1.9:.1f} cm)" in r.reasons
    # The same scene with the squash uncertainty removed gives a smaller sigma.
    import neermattam_perception.references as refs

    old = refs.SQUASH_REL_SIGMA
    try:
        refs.SQUASH_REL_SIGMA = 0.0
        r0 = run(agent_pipeline(easy_case, no_wl)[0], image=case_image(easy_case))
    finally:
        refs.SQUASH_REL_SIGMA = old
    assert r.depth_sigma_cm == pytest.approx(
        math.hypot(r0.depth_sigma_cm or 0.0, 0.5 * 1.9),
        abs=0.1,  # both are rounded to 0.1
    )
