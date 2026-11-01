"""Machine-readable codes for the English reasons and step decisions.

Every reason in a response (``reasons``) and every step decision in its trace is
an English sentence made from one of a small set of templates. A client that
shows them in another language needs the template and its values, not the
sentence: :func:`reason_code` and :func:`decision_code` turn a sentence back into
``{"code": ..., "args": {...}}`` with the catalogue below. The catalogue is the
contract (docs/CONTRACTS.md, section 1.3): codes are stable, and a new template
gets a new code. Text written by a model (a verifier's verdict, an image issue,
the estimate's basis) is never matched; it arrives as an argument (``detail`` or
``basis``) for the client to show as it is. A sentence that matches nothing gets
``{"code": "other", "args": {"text": ...}}``, which the tests forbid for every
sentence the pipeline is known to produce.
"""

from __future__ import annotations

import re
from typing import Any

from .geometry import FAILURE_TEXT as WHEEL_FAILURE_TEXT
from .person import FAILURE_TEXT as PERSON_FAILURE_TEXT
from .velocity import (
    REJECT_DIRECTION,
    REJECT_NO_FIT,
    REJECT_SHORT,
    REJECT_SPEED,
    REJECT_STILL,
)

Code = dict[str, Any]

_NUM = r"-?\d+(?:\.\d+)?"
_KIND = r"two-wheeler wheel|car wheel|auto-rickshaw wheel|bus wheel|kerb|person"
_KIND_CODE = {
    "two-wheeler wheel": "two_wheeler_wheel",
    "car wheel": "car_wheel",
    "auto-rickshaw wheel": "auto_wheel",
    "bus wheel": "bus_wheel",
    "kerb": "kerb",
    "person": "person",
}

# Sub-codes for texts that other modules own: a wheel's geometry failure, a
# person's decline, a velocity failure, a track rejection.
WHEEL_FAILURES = {text: reason.value for reason, text in WHEEL_FAILURE_TEXT.items()}
PERSON_FAILURES = {text: reason.value for reason, text in PERSON_FAILURE_TEXT.items()}
VELOCITY_FAILURES = {
    "frame rate unknown": "no_frame_rate",
    "no floating object was tracked for long enough": "tracks_too_short",
    "no tracked object moved measurably": "nothing_moved",
    "tracks move in no common direction": "no_common_direction",
    "tracks disagree on the flow": "tracks_disagree",
}
TRACK_REJECTIONS = {
    REJECT_SHORT: "too_short",
    REJECT_NO_FIT: "no_fit",
    REJECT_STILL: "not_moving",
    REJECT_DIRECTION: "against_flow",
    REJECT_SPEED: "speed_outlier",
}


def _sub(table: dict[str, str], text: str) -> tuple[str, str]:
    """(sub-code, detail) for ``text`` that starts with one of ``table``'s texts."""
    for known, code in table.items():
        if text == known:
            return code, ""
        if text.startswith(known + " (") and text.endswith(")"):
            return code, text[len(known) + 2 : -1]
    return "other", text


# (code, pattern). Named groups become args; numbers become numbers. Order
# matters: the first full match wins, so specific templates come first.
REASONS: list[tuple[str, str]] = [
    ("water_visible", r"standing water visible"),
    ("no_water", r"no standing water visible"),
    ("image_unusable", r"image is too blurred or dark to measure"),
    ("image_issue", r"image issue: (?P<detail>.+)"),
    (
        "scene_answer_rejected",
        r"the scene model's answer failed its checks twice \((?P<detail>.+)\)",
    ),
    (
        "reference_not_supported",
        rf"depth from a (?P<kind>{_KIND}) is not supported yet; photograph a vehicle wheel",
    ),
    ("no_reference", r"no wheel or person found to measure against"),
    ("water_not_located", r"could not locate the water surface"),
    ("light_truck_car_wheels", r"a light truck on car-size wheels: generic car wheel size used"),
    ("no_wheel_size_for_vehicle", r"no wheel size is known for a (?P<vehicle>.+)"),
    (
        "budget_ran_out",
        rf"the time budget of (?P<budget_s>{_NUM}) s ran out before every check was done; "
        r"this is the best answer reached in time",
    ),
    ("people_not_measured_budget", r"people were not measured: the time budget ran out"),
    ("water_below_people_budget", r"water below people not segmented: the time budget ran out"),
    ("velocity_budget", r"velocity not measured: the time budget ran out"),
    (
        "water_partial_budget",
        r"water: only the part confirmed before the time budget ran out is used",
    ),
    ("water_not_confirmed", r"water mask not confirmed \((?P<detail>.*)\)"),
    ("water_surface_unverified", r"the water surface as a whole could not be verified"),
    ("water_not_segmented", r"water could not be segmented"),
    (
        "wheel_not_confirmed",
        r"(?P<target>.+?): the scene model did not confirm the mask \((?P<detail>.*)\)",
    ),
    (
        "wheel_not_segmented",
        rf"(?P<kind>{_KIND}) could not be segmented \(it may be fully submerged\)",
    ),
    ("reference_found", rf"(?P<kind>{_KIND}) found"),
    ("wheel_size_catalogue", r"wheel size from catalogue: (?P<label>.+)"),
    ("wheel_size_generic", r"wheel size unknown, generic prior used"),
    (
        "tyre_squash",
        rf"tyre squash of (?P<squash_cm>{_NUM}) cm subtracted \(sigma (?P<sigma_cm>{_NUM}) cm\)",
    ),
    ("waterline_agrees", rf"scene model waterline agrees within (?P<cm>{_NUM}) cm"),
    (
        "estimate_disagrees",
        rf"the scene model's own estimate \((?P<estimate_cm>{_NUM}) cm\) disagrees with the "
        r"measurement by more than two sigma; confidence lowered",
    ),
    (
        "person_agrees_with_wheel",
        rf"a person in the water gives (?P<depth_cm>{_NUM}) cm \(sigma (?P<sigma_cm>{_NUM}) cm\), "
        r"consistent with the wheel",
    ),
    (
        "person_disagrees_with_wheel",
        rf"a person in the water gives (?P<depth_cm>{_NUM}) cm \(sigma (?P<sigma_cm>{_NUM}) cm\), "
        r"more than two sigma from the wheel; confidence lowered",
    ),
    (
        "shallow_not_trusted",
        rf"the (?P<kind>{_KIND}) measured (?P<depth_cm>{_NUM}) cm \(sigma (?P<sigma_cm>{_NUM}) "
        rf"cm\), but the scene model estimates (?P<estimate_cm>{_NUM}) cm "
        r"(?P<where>at that wheel|for the street), above the measurement's two-sigma bound; a "
        r"shallow reading that disagrees is not trusted, so the scene model's estimate is "
        r"reported",
    ),
    (
        "estimate_only",
        r"no wheel could be measured; depth is the scene model's visual estimate, not a "
        r"measurement",
    ),
    ("estimate_basis", r"estimate based on: (?P<basis>.*)"),
    (
        "person_measured",
        rf"a person standing in the water was measured against the adult stature prior "
        rf"\((?P<height_cm>{_NUM}) cm, sigma (?P<sigma_cm>{_NUM}) cm\)",
    ),
    ("person_coarse", r"a person reference is coarse: expect about 10 to 15 cm of error"),
    (
        "person_landmark",
        rf"the scene model puts the water at the (?P<landmark>[a-z ]+), about "
        rf"(?P<depth_cm>{_NUM}) cm \((?P<apart_cm>{_NUM}) cm apart\)",
    ),
    (
        "person_contradicted",
        rf"person (?P<person>\d+): measured (?P<depth_cm>{_NUM}) cm \(sigma "
        rf"(?P<sigma_cm>{_NUM}) cm\), but the scene model sees about (?P<scene_cm>{_NUM}) cm "
        r"there; not used",
    ),
    (
        "person_local_water",
        r"person (?P<person>\d+): the water just below was segmented locally, not checked by "
        r"the scene model",
    ),
    (
        "persons_combined",
        rf"people (?P<people>\d+(?: and \d+)+) agree: (?P<depth_cm>{_NUM}) cm \(sigma "
        rf"(?P<sigma_cm>{_NUM}) cm\), their readings combined",
    ),
    ("person_declined", r"person (?P<person>\d+): (?P<failure>.+)"),
    (
        "velocity_measured",
        rf"surface velocity (?P<surface_mps>{_NUM}) m/s \(sigma (?P<surface_sigma_mps>{_NUM})\) "
        rf"from (?P<used>\d+) tracked objects, (?P<rejected>\d+) rejected; depth-averaged "
        rf"(?P<mean_mps>{_NUM}) m/s \(sigma (?P<mean_sigma_mps>{_NUM})\)",
    ),
    ("velocity_measured_tracks", r"surface velocity from (?P<used>\d+) tracked objects"),
    ("velocity_no_scale", r"velocity unavailable: the person gives no scale"),
    ("velocity_unavailable", r"velocity unavailable: (?P<failure>.+)"),
    ("flow_no_frame_rate", r"the video has no frame rate"),
    ("flow_locator_failed", r"the debris locator failed: (?P<detail>.+)"),
    ("flow_nothing_floating", r"the scene model found nothing floating to track"),
    (
        "flow_nothing_distinct",
        r"no box from the scene model held an object distinct from the water",
    ),
    ("flow_tracking_failed", r"tracking failed: (?P<detail>.+)"),
    # Last: "<wheel kind>: <geometry failure>", e.g. "car wheel: water does not meet the wheel".
    ("wheel_failed", rf"(?P<kind>{_KIND}): (?P<failure>.+)"),
]

DECISIONS: list[tuple[str, str]] = [
    ("accept", r"accept"),
    ("accept_partial", r"accept \(coverage partial\)"),
    # "[x0,y0,x1,y1] px", then the points, such as " + points [1,2], [3,4]".
    ("refine", r"refine with box (?P<box>\[[-\d, ]+\]) px(?: \+ (?P<points>.+))?"),
    ("give_up", r"give up"),
    ("give_up_reason", r"give up: (?P<detail>.+)"),
    ("verify", r"verify"),
    ("verify_best", r"verify the best (?P<n>one|two)"),
    ("measure", r"measure"),
    ("candidate", r"candidate"),
    ("declined", r"declined"),
    ("rejected", r"rejected"),
    ("retry", r"retry"),
    ("drop", r"drop"),
    ("drop_fields", r"drop (?P<n>\d+) fields? that failed, use the rest"),
    ("snap", r"snap"),
    ("scene_stop_no_water", r"stop: no water"),
    ("scene_stop_no_wheel", r"stop: no wheel"),
    ("scene_stop_unchecked", r"stop: no checked scene reading"),
    ("scene_report_estimate", r"no wheel: report own estimate"),
    (
        "scene_measure_persons",
        r"no wheel: measure (?P<n>\d+) persons? against the stature prior and segment the water",
    ),
    (
        "scene_verify_wheels",
        r"segment and verify (?P<n>\d+) wheels? and the water",
    ),
    (
        "scene_search_vehicles",
        r"search (?P<vehicles>\d+) vehicle box(?:es)? for wheels"
        r"(?:, check (?P<wheels>\d+) wheels?)? and segment the water",
    ),
    ("join_water", r"join to the verified water"),
    ("check_water_below", r"check that each outline ends at water"),
    ("measure_people", r"measure each against the stature prior"),
    ("use_measurement", r"use the measurement"),
    ("use_wheel", r"use the wheel"),
    ("use_person", r"use the person"),
    ("use_person_n", r"use person (?P<person>\d+)"),
    ("combine_people", r"combine the people who agree"),
    ("report_estimate", r"report model estimate"),
    ("shallow_not_trusted", r"shallow reading not trusted: report the estimate"),
    ("person_not_published", r"do not publish a person measurement"),
    ("budget_answer", r"answer with what was verified in time"),
    ("budget_skip_people", r"skip the people"),
    ("budget_keep_water", r"keep the verified water as it is"),
    ("budget_verified_water_only", r"use the verified water only"),
    ("track", r"track"),
    ("flow_stop_nothing_floating", r"stop: nothing floating to track"),
    ("check_tracks", r"check the tracks"),
    ("subtract_camera_motion", r"subtract its motion from every track"),
    ("camera_motion_kept", r"camera motion not removed"),
    ("report_velocity", r"report velocity"),
    ("no_speed", r"no speed"),
    ("no_speed_rejected", r"no speed \((?P<rejected>\d+) tracks rejected\)"),
]

_REASONS = [(c, re.compile(p)) for c, p in REASONS]
_DECISIONS = [(c, re.compile(p)) for c, p in DECISIONS]


def _value(v: str) -> Any:
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    if re.fullmatch(_NUM, v):
        return float(v)
    return v


def _encode(text: str, table: list[tuple[str, re.Pattern[str]]]) -> Code:
    for code, pat in table:
        m = pat.fullmatch(text)
        if m is None:
            continue
        args = {k: _value(v) for k, v in m.groupdict().items() if v is not None and v != ""}
        if "kind" in args:
            args["kind"] = _KIND_CODE.get(str(args["kind"]), args["kind"])
        return {"code": code, "args": args}
    return {"code": "other", "args": {"text": text}}


def reason_code(text: str) -> Code:
    """The code and args of one reason sentence."""
    c = _encode(text, _REASONS)
    args = c["args"]
    if c["code"] == "wheel_failed":
        sub, detail = _sub(WHEEL_FAILURES, str(args.pop("failure")))
        if sub == "other":  # "<kind>: <text>" that is not a geometry failure
            return {"code": "other", "args": {"text": text}}
        args["failure"] = sub
        if detail:
            args["detail"] = detail
    elif c["code"] == "person_declined":
        sub, detail = _sub(PERSON_FAILURES, str(args.pop("failure")))
        if sub == "other":
            return {"code": "other", "args": {"text": text}}
        args["failure"] = sub
        if detail:
            args["detail"] = detail
    elif c["code"] == "velocity_unavailable":
        failure = str(args.pop("failure"))
        sub, _ = _sub(VELOCITY_FAILURES, failure)
        args["failure"] = sub
        if sub == "other":
            args["detail"] = failure
    return c


def decision_code(text: str) -> Code:
    """The code and args of one step decision."""
    return _encode(text, _DECISIONS)
