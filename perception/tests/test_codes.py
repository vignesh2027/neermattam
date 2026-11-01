"""The reason and decision codes (codes.py) and their contract (CONTRACTS.md 1.3)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from neermattam_perception import codes
from neermattam_perception.codes import decision_code, reason_code

CONTRACTS = Path(__file__).resolve().parents[2] / "docs" / "CONTRACTS.md"


def section_codes() -> set[str]:
    text = CONTRACTS.read_text(encoding="utf-8")
    sec = text.split("### 1.3 Reason and decision codes")[1].split("## 2. Engine")[0]
    return set(re.findall(r"^\| `([a-z_0-9]+)` \|", sec, re.M))


def test_contract_lists_exactly_the_catalogue() -> None:
    catalogue = {c for c, _ in codes.REASONS} | {c for c, _ in codes.DECISIONS}
    catalogue |= set(codes.WHEEL_FAILURES.values()) | set(codes.PERSON_FAILURES.values())
    catalogue |= set(codes.VELOCITY_FAILURES.values())
    assert section_codes() == catalogue


@pytest.mark.parametrize(
    ("text", "code", "args"),
    [
        ("standing water visible", "water_visible", {}),
        ("image issue: motion blur", "image_issue", {"detail": "motion blur"}),
        ("two-wheeler wheel found", "reference_found", {"kind": "two_wheeler_wheel"}),
        (
            "the time budget of 55 s ran out before every check was done; this is the best "
            "answer reached in time",
            "budget_ran_out",
            {"budget_s": 55},
        ),
        (
            "a person reference is coarse: expect about 10 to 15 cm of error",
            "person_coarse",
            {},
        ),
        (
            "two-wheeler wheel 1: the scene model did not confirm the mask (this is a shadow)",
            "wheel_not_confirmed",
            {"target": "two-wheeler wheel 1", "detail": "this is a shadow"},
        ),
        (
            "car wheel: water does not meet the wheel",
            "wheel_failed",
            {"kind": "car_wheel", "failure": "no_water_contact"},
        ),
        (
            "person 2: the person is not standing upright (sitting)",
            "person_declined",
            {"person": 2, "failure": "posture", "detail": "sitting"},
        ),
        (
            "person 1: measured 70 cm (sigma 13 cm), but the scene model sees about 25 cm there; "
            "not used",
            "person_contradicted",
            {"person": 1, "depth_cm": 70, "sigma_cm": 13, "scene_cm": 25},
        ),
        (
            "tyre squash of 1.9 cm subtracted (sigma 0.9 cm)",
            "tyre_squash",
            {"squash_cm": 1.9, "sigma_cm": 0.9},
        ),
        (
            "velocity unavailable: tracks move in no common direction",
            "velocity_unavailable",
            {"failure": "no_common_direction"},
        ),
        (
            "velocity unavailable: video tracking by noun phrase needs SAM 3.1",
            "velocity_unavailable",
            {"failure": "other", "detail": "video tracking by noun phrase needs SAM 3.1"},
        ),
        (
            "estimate based on: water at the ankles",
            "estimate_basis",
            {"basis": "water at the ankles"},
        ),
    ],
)
def test_reasons(text: str, code: str, args: dict[str, object]) -> None:
    assert reason_code(text) == {"code": code, "args": args}


@pytest.mark.parametrize(
    ("text", "code", "args"),
    [
        ("report model estimate", "report_estimate", {}),
        ("measure each against the stature prior", "measure_people", {}),
        ("refine with box [600,570,670,640] px", "refine", {"box": "[600,570,670,640]"}),
        (
            "refine with box [1,2,3,4] px + points [5,6], [7,8]",
            "refine",
            {"box": "[1,2,3,4]", "points": "points [5,6], [7,8]"},
        ),
        ("give up: tyre seen head-on", "give_up_reason", {"detail": "tyre seen head-on"}),
        ("verify the best two", "verify_best", {"n": "two"}),
        (
            "search 2 vehicle boxes for wheels, check 1 wheel and segment the water",
            "scene_search_vehicles",
            {"vehicles": 2, "wheels": 1},
        ),
        (
            "search 1 vehicle box for wheels and segment the water",
            "scene_search_vehicles",
            {"vehicles": 1},
        ),
        ("drop 2 fields that failed, use the rest", "drop_fields", {"n": 2}),
        ("no speed (3 tracks rejected)", "no_speed_rejected", {"rejected": 3}),
        ("use person 2", "use_person_n", {"person": 2}),
    ],
)
def test_decisions(text: str, code: str, args: dict[str, object]) -> None:
    assert decision_code(text) == {"code": code, "args": args}


def test_unknown_text_is_other() -> None:
    assert reason_code("something new") == {"code": "other", "args": {"text": "something new"}}
    assert reason_code("car wheel: not a geometry failure")["code"] == "other"
    assert decision_code("dance") == {"code": "other", "args": {"text": "dance"}}
