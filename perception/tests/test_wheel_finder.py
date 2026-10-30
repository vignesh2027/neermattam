"""Finding a wheel inside a vehicle box: grid prompts, dark blobs, wheel score, agent path."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from neermattam_perception.adapters import VehicleObject
from neermattam_perception.agent import VerifyRefineAgent
from neermattam_perception.media import image_from_rgb
from neermattam_perception.muse_open import parse_open_scene
from neermattam_perception.references import RefKind
from neermattam_perception.sam_local import MaskCandidate
from neermattam_perception.wheel_finder import (
    dark_ellipse_boxes,
    grid_points,
    score_wheel,
    size_score,
    wheel_region,
)

from .test_agent import GOOD, ScriptedVerifier


def car_scene() -> tuple[np.ndarray, np.ndarray, tuple[int, int, int, int]]:
    """A light grey car body with one dark tyre (rim inside) cut by water at the bottom."""
    rgb = np.full((480, 640, 3), 170, np.uint8)
    cv2.rectangle(rgb, (100, 150), (540, 330), (200, 200, 205), -1)  # body
    cv2.circle(rgb, (200, 330), 60, (25, 25, 25), -1)  # tyre
    cv2.circle(rgb, (200, 330), 35, (150, 150, 150), -1)  # rim
    rgb[360:, :] = (90, 110, 120)  # water
    wheel = np.zeros((480, 640), bool)
    yy, xx = np.mgrid[0:480, 0:640]
    wheel[((xx - 200) ** 2 + (yy - 330) ** 2) <= 60**2] = True
    wheel[360:] = False
    return rgb, wheel, (100, 150, 540, 360)


def test_region_grid_and_dark_blob_find_the_tyre() -> None:
    rgb, _, vbox = car_scene()
    region = wheel_region(vbox, 640, 480)
    assert region[1] > vbox[1] and region[3] > vbox[3]  # lower part, a little below
    pts = grid_points(region)
    assert len(pts) == 24 and all(region[0] < x < region[2] for x, _ in pts)
    boxes = dark_ellipse_boxes(rgb, region, vbox[2] - vbox[0])
    assert boxes, "the dark tyre is found"
    b = boxes[0]
    assert b[0] <= 145 and b[2] >= 255  # around the tyre (x 140 to 260)


def test_wheel_score_prefers_the_tyre_to_a_body_patch() -> None:
    rgb, wheel, vbox = car_scene()
    region = wheel_region(vbox, 640, 480)
    body = np.zeros_like(wheel)
    body[160:250, 300:520] = True
    tyre = score_wheel(rgb, wheel, region, vbox[2] - vbox[0])
    patch = score_wheel(rgb, body, region, vbox[2] - vbox[0])
    assert tyre.score > 0.4 and tyre.darkness > 0.5
    assert patch.score < tyre.score
    assert "dark" in tyre.text()
    tiny = np.zeros_like(wheel)
    tiny[300:305, 300:305] = True
    assert size_score(tiny, 440) == 0.0


@dataclass
class RegionSam:
    """Answers region_candidates with the true wheel among distractors."""

    wheel: np.ndarray
    mask_model_name: str = "fake-sam"
    name: str = "fake-sam"
    regions: list[Any] = field(default_factory=list)

    async def region_candidates(self, image, region, prompts):  # type: ignore[no-untyped-def]
        self.regions.append((region, len(prompts)))
        body = np.zeros_like(self.wheel)
        body[160:250, 300:520] = True
        return [
            MaskCandidate(body, 0.95, "grid", 0),
            MaskCandidate(self.wheel, 0.9, "dark blob", 1),
        ]

    async def segment(self, image, objects):  # type: ignore[no-untyped-def]
        raise AssertionError("not used when the first proposal is accepted")

    async def ready(self):  # type: ignore[no-untyped-def]
        return True, "ok"


def test_agent_finds_and_accepts_a_vehicle_wheel() -> None:
    rgb, wheel, vbox = car_scene()
    sam = RegionSam(wheel)
    ver = ScriptedVerifier(lambda what, n: {**GOOD, "choice": "A"})
    agent = VerifyRefineAgent(ver, sam)
    vehicle = VehicleObject("car", vbox, "hyundai_creta")
    out = asyncio.run(
        agent.find_vehicle_wheel(image_from_rgb(rgb), vehicle, RefKind.CAR_WHEEL, 1, 1)
    )
    assert sam.regions and sam.regions[0][1] >= 24  # grid points plus the dark blob
    assert out.accepted and out.mask is not None
    assert out.obj.kind == RefKind.CAR_WHEEL and out.obj.vehicle == "hyundai_creta"
    assert out.trace[0].step == "segment" and "24 grid points" in out.trace[0].output
    assert out.trace[0].target == "car 1 wheel"
    inter = np.logical_and(out.mask.mask, wheel).sum() / wheel.sum()
    assert inter > 0.9  # the tyre, not the body patch, was shown first and accepted


def test_vehicles_are_parsed_from_the_scene_answer() -> None:
    data = {
        "water_visible": True,
        "vehicles": [
            {
                "kind": "Car",
                "box": [100, 300, 700, 800],
                "vehicle": "hyundai_creta",
                "wheels_visible": True,
            },
            {"kind": "truck", "box": [5, 5, 5, 9]},  # degenerate box: dropped
            {
                "kind": "bus",
                "box": [10, 10, 200, 300],
                "vehicle": "no_such",
                "wheels_visible": False,
            },
        ],
    }
    r = parse_open_scene(data, 1000, 500)
    assert [v.kind for v in r.vehicles] == ["car", "bus"]
    assert r.vehicles[0].box == (100, 150, 701, 401) and r.vehicles[0].vehicle == "hyundai_creta"
    assert r.vehicles[1].vehicle is None and not r.vehicles[1].wheels_visible


def test_unsplash_photo_metadata() -> None:
    root = Path(__file__).resolve().parents[2] / "eval"
    meta = json.loads((root / "unsplash_photos.json").read_text())["photos"]
    assert len(meta) == 5 and {m["set"] for m in meta} == {"close"}
    for m in meta:
        assert m["page"].startswith("https://unsplash.com/photos/")
        assert m["licence"] == "Unsplash License" and m["author"]


def test_light_truck_on_car_wheels_is_searched_and_other_trucks_are_not(easy_case) -> None:  # type: ignore[no-untyped-def]
    from dataclasses import replace

    from .conftest import case_image
    from .test_agent import agent_pipeline, open_reading

    base = open_reading(easy_case)
    wref = base.references[0]
    ys, xs = np.nonzero(easy_case.wheel_mask)
    truck_box = (int(xs.min()) - 40, int(ys.min()) - 120, int(xs.max()) + 200, int(ys.max()) + 5)
    truck = VehicleObject("truck", truck_box)
    pickup = replace(base, references=(replace(wref, kind=RefKind.CAR_WHEEL),), vehicles=(truck,))
    p, _, _ = agent_pipeline(easy_case, pickup)
    r = asyncio.run(p.measure(image=case_image(easy_case)))
    assert any("light truck on car-size wheels" in x for x in r.reasons)
    assert any(t.target == "truck 1 wheel" for t in r.agent_trace)

    lorry = replace(base, references=(replace(wref, kind=RefKind.BUS_WHEEL),), vehicles=(truck,))
    p2, _, _ = agent_pipeline(easy_case, lorry)
    r2 = asyncio.run(p2.measure(image=case_image(easy_case)))
    assert "no wheel size is known for a truck" in r2.reasons
    assert not any(t.target == "truck 1 wheel" for t in r2.agent_trace)


def test_an_outline_on_the_rim_is_darker_outside_than_inside() -> None:
    from neermattam_perception.wheel_finder import INSIDE_TYRE_RATIO, outside_inside_ratio

    rgb, wheel, _ = car_scene()
    yy, xx = np.mgrid[0:480, 0:640]
    rim = ((xx - 200) ** 2 + (yy - 330) ** 2) <= 33**2
    rim[360:] = False
    assert outside_inside_ratio(rgb, wheel) > 1.5  # rubber inside, body and water outside
    assert outside_inside_ratio(rgb, rim) < INSIDE_TYRE_RATIO  # rubber outside the rim


def test_tyre_failure_tells_tyre_from_rim_cut_and_wrong_size() -> None:
    from neermattam_perception.wheel_finder import tyre_evidence, tyre_failure

    rgb, wheel, vbox = car_scene()
    yy, xx = np.mgrid[0:480, 0:640]
    vw = vbox[2] - vbox[0]
    ev = tyre_evidence(rgb, wheel)
    assert ev is not None and ev.dark_inside > ev.dark_outside
    assert tyre_failure(ev, "car", vw) is None  # 120 px on a 440 px car: 27%

    rim = ((xx - 200) ** 2 + (yy - 330) ** 2) <= 33**2
    rim[360:] = False
    why = tyre_failure(tyre_evidence(rgb, rim), "car", vw)
    assert why is not None and "inside the tyre" in why

    # Half the tyre closed by a chord through the wheel: a tilted oval (the x1 failure).
    cut = wheel & ((xx - 200) + (yy - 330) < 0)
    why = tyre_failure(tyre_evidence(rgb, cut))
    assert why is not None and ("tilted" in why or "inside the tyre" in why)

    why = tyre_failure(ev, "car", 2000)  # a 120 px wheel on a 2000 px car box
    assert why is not None and "of the vehicle's width" in why


def test_tyre_points_outside_the_outline_are_counted() -> None:
    from neermattam_perception.wheel_finder import points_outside

    _, wheel, _ = car_scene()
    on_rubber = [(145, 320), (200, 275), (255, 320)]  # radius about 55 of 60
    assert points_outside(wheel, on_rubber) == 0
    yy, xx = np.mgrid[0:480, 0:640]
    rim = ((xx - 200) ** 2 + (yy - 330) ** 2) <= 33**2
    assert points_outside(rim, on_rubber) == 3
