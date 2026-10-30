"""The checks every model answer passes (validate.py), and what happens when one fails."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from typing import Any

import httpx
import pytest

from neermattam_perception.adapters import AnswerRejected
from neermattam_perception.agent import VerifyRefineAgent
from neermattam_perception.muse_open import (
    PROMPT_VERSIONS,
    SCENE_PROMPT_VERSION,
    VERIFY_WATER_VERSION,
    VERIFY_WHEEL_VERSION,
    Usage,
    prompt_digest,
    prompt_version,
    verify_prompt,
)
from neermattam_perception.pipeline import Pipeline
from neermattam_perception.validate import (
    check_scene,
    check_transcript,
    check_water_answer,
    check_wheel_answer,
    grid_box_problem,
    grid_point_problem,
    pixel_box_problem,
)

from .conftest import FakeMasks, FakeScene, case_image, reading_for
from .test_agent import GOOD, WATER_OK, BoxSam, ScriptedVerifier
from .test_muse_open import IMAGE, adapter, recorded, with_content
from .test_pipeline import FAST

SCENE_OK: dict[str, Any] = {
    "water_visible": True,
    "water": {"box": [100, 700, 400, 900], "point": [250, 800]},
    "references": [
        {
            "kind": "two_wheeler_wheel",
            "box": [400, 500, 500, 650],
            "point": [450, 520],
            "tyre_points": [[410, 560], [450, 505], [490, 560]],
            "vehicle": "unknown",
            "position": "front",
            "confidence": 0.8,
            "depth_cm": 20,
        }
    ],
    "vehicles": [{"kind": "two_wheeler", "box": [380, 300, 700, 700], "wheels_visible": True}],
    "overall": {"depth_cm": 25, "low_cm": 15, "high_cm": 35, "reason": "knee deep"},
    "faces": [],
    "image_quality": {"score": 0.8, "blurred": False, "too_dark": False, "issues": []},
}


# --------------------------------------------------------------------------- primitives


def test_grid_points_and_boxes() -> None:
    assert grid_point_problem([0, 1000]) is None
    assert grid_point_problem({"x": 5, "y": 6}) is None
    assert "outside the 0 to 1000 grid" in (grid_point_problem([1003, 5]) or "")
    assert "outside" in (grid_point_problem([-1, 5]) or "")
    assert "not a number" in (grid_point_problem([True, 5]) or "")
    assert "not a point" in (grid_point_problem([1, 2, 3]) or "")
    assert grid_box_problem([0, 0, 1000, 1000]) is None
    assert "x_min < x_max" in (grid_box_problem([600, 700, 400, 900]) or "")  # corners swapped
    assert "x_min < x_max" in (grid_box_problem([10, 10, 10, 20]) or "")  # no width
    assert "outside" in (grid_box_problem([0, 0, 1001, 10]) or "")
    assert "not a number" in (grid_box_problem([0, 0, float("nan"), 10]) or "")
    assert pixel_box_problem((0, 0, 640, 480), 640, 480) is None
    assert "not inside" in (pixel_box_problem((0, 0, 641, 480), 640, 480) or "")


# --------------------------------------------------------------------------- scene


def test_a_good_scene_answer_passes_untouched() -> None:
    c = check_scene(SCENE_OK, 900, 675)
    assert c.ok, c.problems
    assert c.data["references"][0]["kind"] == "two_wheeler_wheel"


def test_scene_rejections_are_named_never_silent() -> None:
    bad = json.loads(json.dumps(SCENE_OK))
    bad["references"] += [
        {"kind": "boat", "box": [0, 0, 10, 10]},  # not an allowed kind
        {"kind": "car_wheel", "box": [600, 700, 400, 900]},  # corners swapped, no point
        {"kind": "person", "box": [100, 100, 200, 600], "posture": "dancing", "adult": "no"},
    ]
    bad["references"][0]["tyre_points"].append([1200, 40])  # off the grid
    bad["references"][0]["confidence"] = 1.7
    bad["vehicles"].append({"kind": "spaceship", "box": [0, 0, 100, 100]})
    bad["overall"]["low_cm"] = 50  # above the high bound
    bad["faces"] = [[130, 200, 170, 260], [5, 5, 5, 5]]
    c = check_scene(bad, 900, 675)
    assert c.fatal is None
    text = "\n".join(c.problems)
    assert "references[1]: kind 'boat' is not an allowed reference kind; dropped" in text
    assert "references[2].box" in text and "references[2]: no valid box or point" in text
    assert "references[3].posture" in text and "references[3].adult" in text
    assert "references[0].tyre_points[3]" in text and "references[0].confidence" in text
    assert "vehicles[1].kind" in text
    assert "the low bound is above the high bound" in text
    assert "faces[1]" in text
    kinds = [r["kind"] for r in c.data["references"]]
    assert kinds == ["two_wheeler_wheel", "person"]
    assert len(c.data["references"][0]["tyre_points"]) == 3
    assert "posture" not in c.data["references"][1] and "adult" not in c.data["references"][1]
    assert len(c.data["vehicles"]) == 1 and len(c.data["faces"]) == 1
    assert "low_cm" not in c.data["overall"] and c.data["overall"]["depth_cm"] == 25


def test_scene_without_a_boolean_water_visible_is_fatal() -> None:
    for v in ("yes", None, 1):
        c = check_scene({**SCENE_OK, "water_visible": v}, 900, 675)
        assert c.fatal is not None and c.fatal.startswith("water_visible")
    assert (
        check_scene(["not", "an", "object"], 900, 675).fatal
        == "the scene answer is not a JSON object"
    )


# --------------------------------------------------------------------------- checks of masks


def test_wheel_answer_decision_fields_are_required() -> None:
    assert check_wheel_answer({**GOOD, "choice": "A"}, 2).ok
    assert check_wheel_answer(GOOD, 1).ok  # one panel: no choice needed
    assert (check_wheel_answer(GOOD, 2).fatal or "").startswith("choice: missing")
    assert (check_wheel_answer({**GOOD, "choice": "B"}, 1).fatal or "").startswith("choice")
    assert (check_wheel_answer({**GOOD, "is_target": "true"}, 1).fatal or "").startswith(
        "is_target"
    )
    assert (check_wheel_answer({**GOOD, "outline_quality": "fine"}, 1).fatal or "").startswith(
        "outline_quality"
    )
    c = check_wheel_answer({**GOOD, "choice": "none"}, 2)
    assert c.ok and c.data["choice"] == "none"


def test_wheel_answer_bad_optional_fields_are_dropped() -> None:
    c = check_wheel_answer(
        {
            **GOOD,
            "view": "upside_down",
            "tyre_edge_visible": 2,
            "tyre_box": [500, 100, 300, 400],
            "waterline": [[0, 0]],
            "exclude_points": [[10, 10], [2000, 5]],
        },
        1,
    )
    assert c.fatal is None
    assert {p.split(":")[0] for p in c.problems} == {
        "view",
        "tyre_edge_visible",
        "tyre_box",
        "waterline",
        "exclude_points[1]",
    }
    assert c.data["exclude_points"] == [[10, 10]]
    assert "tyre_box" not in c.data and "view" not in c.data


def test_water_answer() -> None:
    assert check_water_answer(WATER_OK).ok
    assert (check_water_answer({"is_water": True}).fatal or "").startswith("coverage")
    c = check_water_answer({**WATER_OK, "corrected_point": [500, -3]})
    assert c.fatal is None and c.problems[0].startswith("corrected_point")


def test_transcript_codes_and_ranges() -> None:
    c = check_transcript(
        {
            "text": "mutti alavu",
            "language": "ta+xx+EN+ta-IN",
            "depth_cm": 45,
            "depth_low_cm": 60,
            "depth_high_cm": 30,
        }
    )
    assert c.fatal is None
    assert c.data["language"] == "ta+en"
    assert any("'xx' is not a supported language code" in p for p in c.problems)
    assert any("the low bound is above the high bound" in p for p in c.problems)
    assert "depth_low_cm" not in c.data and c.data["depth_cm"] == 45
    assert (check_transcript({"text": 5}).fatal or "").startswith("text")
    assert check_transcript({"text": "x", "depth_cm": -4}).problems


# --------------------------------------------------------------------------- the agent


def test_a_failed_check_is_asked_again_and_logged(easy_case) -> None:  # type: ignore[no-untyped-def]
    img = case_image(easy_case)
    sam = BoxSam({1: easy_case.wheel_mask}, easy_case.wheel_mask.shape)

    def script(what: str, n: int) -> dict[str, Any]:
        if n == 0:
            return {**GOOD, "is_target": "yes"}  # not a boolean
        return GOOD

    ver = ScriptedVerifier(script)
    out = asyncio.run(
        VerifyRefineAgent(ver, sam).refine_reference(
            img, reading_for(easy_case).references[0], 1, 1
        )
    )
    assert out.accepted
    steps = [(t.step, t.decision) for t in out.trace]
    assert steps == [
        ("segment", "verify the best one"),
        ("validate", "retry"),
        ("verify", "accept"),
    ]
    assert out.trace[1].output.startswith("is_target") and out.trace[1].model == "validator"
    assert len(ver.calls) == 2
    # Both answers are paid for.
    assert out.cost_usd == pytest.approx(2 * Usage(1000, 500).cost_usd)


def test_two_failed_checks_give_up(easy_case) -> None:  # type: ignore[no-untyped-def]
    img = case_image(easy_case)
    sam = BoxSam({1: easy_case.wheel_mask}, easy_case.wheel_mask.shape)
    ver = ScriptedVerifier(lambda what, n: {"is_target": True, "outline_quality": "great"})
    out = asyncio.run(
        VerifyRefineAgent(ver, sam).refine_reference(
            img, reading_for(easy_case).references[0], 1, 1
        )
    )
    assert not out.accepted and out.mask is None
    assert [(t.step, t.decision) for t in out.trace][-2:] == [
        ("validate", "retry"),
        ("validate", "give up"),
    ]
    assert out.verdict == "the scene model's answers failed their checks"
    assert out.cost_usd > 0


def test_dropped_fields_are_logged_and_the_rest_used(easy_case) -> None:  # type: ignore[no-untyped-def]
    img = case_image(easy_case)
    sam = BoxSam({1: easy_case.wheel_mask}, easy_case.wheel_mask.shape)
    ver = ScriptedVerifier(lambda what, n: {**GOOD, "view": "sideways"})
    out = asyncio.run(
        VerifyRefineAgent(ver, sam).refine_reference(
            img, reading_for(easy_case).references[0], 1, 1
        )
    )
    assert out.accepted
    drop = [t for t in out.trace if t.step == "validate"]
    assert len(drop) == 1 and drop[0].decision.startswith("drop 1 field")
    assert "view: 'sideways'" in drop[0].output


def test_water_check_failure_gives_up(easy_case) -> None:  # type: ignore[no-untyped-def]
    img = case_image(easy_case)
    sam = BoxSam({16: easy_case.water_mask}, easy_case.water_mask.shape)
    ver = ScriptedVerifier(lambda what, n: {"is_water": "true", "coverage": "good"})
    out = asyncio.run(VerifyRefineAgent(ver, sam).refine_water(img, None, [(10, 470)], 16))
    assert not out.accepted
    assert [t.decision for t in out.trace if t.step == "validate"] == ["retry", "give up"]


# --------------------------------------------------------------------------- the scene adapter


def test_scene_answer_failing_its_check_is_asked_again_without_the_cache() -> None:
    answers = [{**SCENE_OK, "water_visible": "yes"}, SCENE_OK]
    calls: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        body = answers[min(len(calls), len(answers)) - 1]
        return httpx.Response(
            200, json=with_content(recorded("baseten_scene_response.json"), json.dumps(body))
        )

    a = adapter(handler)
    reading = asyncio.run(a.read_scene(IMAGE))
    assert len(calls) == 2
    assert reading.retried_because is not None and reading.retried_because.startswith(
        "water_visible"
    )
    assert reading.prompt_version == SCENE_PROMPT_VERSION and reading.dropped == ()
    assert reading.water_visible
    # The good answer was cached, the bad one never was.
    again = asyncio.run(a.read_scene(IMAGE))
    assert len(calls) == 2 and again.cached and again.retried_because is None


def test_scene_answer_failing_twice_is_rejected() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        body = {**SCENE_OK, "water_visible": None}
        return httpx.Response(
            200, json=with_content(recorded("baseten_scene_response.json"), json.dumps(body))
        )

    with pytest.raises(AnswerRejected) as exc:
        asyncio.run(adapter(handler).read_scene(IMAGE))
    assert len(exc.value.attempts) == 2 and exc.value.cost_usd > 0


def test_pipeline_declines_a_scene_that_failed_twice(easy_case) -> None:  # type: ignore[no-untyped-def]
    class Rejecting(FakeScene):
        async def read_scene(self, image):  # type: ignore[no-untyped-def]
            raise AnswerRejected(
                "the scene model's answer", ["water_visible: a", "water_visible: b"]
            )

    p = Pipeline(Rejecting(reading_for(easy_case)), FakeMasks({}), FAST)
    r = asyncio.run(p.measure(image=case_image(easy_case)))
    assert not r.usable and r.depth_cm is None
    assert r.reasons == ["the scene model's answer failed its checks twice (water_visible: b)"]
    assert [(s.step, s.decision) for s in r.agent_trace] == [
        ("scene", "stop: no checked scene reading"),
        ("validate", "retry"),
        ("validate", "give up"),
    ]


def test_dropped_scene_fields_become_a_validate_step(easy_case) -> None:  # type: ignore[no-untyped-def]
    reading = replace(
        reading_for(easy_case),
        prompt_version=SCENE_PROMPT_VERSION,
        dropped=("references[1]: kind 'boat' is not an allowed reference kind; dropped",),
    )
    masks = FakeMasks({1: easy_case.wheel_mask, 2: easy_case.rim_mask, 16: easy_case.water_mask})
    r = asyncio.run(Pipeline(FakeScene(reading), masks, FAST).measure(image=case_image(easy_case)))
    assert r.usable
    assert (
        r.agent_trace[0].step == "scene" and r.agent_trace[0].prompt_version == SCENE_PROMPT_VERSION
    )
    v = r.agent_trace[1]
    assert v.step == "validate" and "boat" in v.output and v.prompt_version is None


# --------------------------------------------------------------------------- prompt versions


def test_prompt_versions_are_stable_ids() -> None:
    assert set(PROMPT_VERSIONS) == {"scene", "verify-wheel", "verify-water", "transcribe"}
    for name, v in PROMPT_VERSIONS.items():
        assert re.fullmatch(rf"{name}@[0-9a-f]{{12}}", v)
        digest = prompt_digest(v)
        assert digest is not None and digest.startswith(v.split("@")[1])
    assert prompt_digest("scene@000000000000") is None
    # Any change to the text changes the version.
    assert prompt_version("x", "a b") != prompt_version("x", "a  b")
    assert prompt_version("x", "ab", "c") != prompt_version("x", "a", "bc")
    # One version per template, whatever the kind of wheel filled in.
    assert verify_prompt("car wheel and tyre", 2) != verify_prompt("bus wheel and tyre", 2)


def test_verify_steps_carry_their_prompt_version(easy_case) -> None:  # type: ignore[no-untyped-def]
    img = case_image(easy_case)
    sam = BoxSam({1: easy_case.wheel_mask, 16: easy_case.water_mask}, easy_case.wheel_mask.shape)
    agent = VerifyRefineAgent(
        ScriptedVerifier(lambda w, n: WATER_OK if "water" in w else GOOD), sam
    )
    out = asyncio.run(agent.refine_reference(img, reading_for(easy_case).references[0], 1, 1))
    water = asyncio.run(agent.refine_water(img, None, [(10, 470)], 16))
    assert {t.prompt_version for t in out.trace if t.step == "verify"} == {VERIFY_WHEEL_VERSION}
    assert {t.prompt_version for t in water.trace if t.step == "verify"} == {VERIFY_WATER_VERSION}
    assert all(t.prompt_version is None for t in out.trace + water.trace if t.step == "segment")
