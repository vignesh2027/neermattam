"""The wheel-shape judge, the multi-candidate step, the close look and the scene cache."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import cv2
import httpx
import numpy as np
import pytest

from neermattam_perception.adapters import SceneObject
from neermattam_perception.agent import VerifyRefineAgent, _chosen, _initial_points
from neermattam_perception.media import ImageInput
from neermattam_perception.muse_open import MuseSparkOpenAdapter, Usage, jpeg_for_model
from neermattam_perception.references import RefKind
from neermattam_perception.sam import PointPrompt
from neermattam_perception.sam_local import MaskCandidate
from neermattam_perception.wheel_shape import dedupe, solid_wheel, wheel_shape

from .conftest import case_image, reading_for
from .test_agent import GOOD, BoxSam, ScriptedVerifier
from .test_muse_open import IMAGE, KEY, recorded


def ring(shape: tuple[int, int], c: tuple[int, int], r: int, width: int, cut_y: int) -> np.ndarray:
    m = np.zeros(shape, np.uint8)
    cv2.circle(m, c, r, 1, -1)
    cv2.circle(m, c, r - width, 0, -1)
    m[cut_y:] = 0
    return m.astype(bool)


def test_solid_wheel_closes_a_tyre_arch_with_its_hull() -> None:
    arch = ring((300, 300), (150, 150), 100, 18, 210)  # tyre above water, rim left out
    solid, hull = solid_wheel(arch)
    assert hull and solid[150, 150]  # the rim area is inside
    full = np.zeros((300, 300), bool)
    cv2.circle(full := full.astype(np.uint8), (150, 150), 100, 1, -1)
    full = full.astype(bool)
    full[210:] = False
    assert solid.sum() == pytest.approx(full.sum(), rel=0.03)
    disc, used = solid_wheel(full)
    assert not used and np.array_equal(disc, full)


def test_wheel_shape_ranks_wheels_above_blobs_and_bulges(easy_case) -> None:  # type: ignore[no-untyped-def]
    wheel = wheel_shape(easy_case.wheel_mask)
    assert wheel.score > 0.5 and wheel.arc_deg > 150
    rect = np.zeros_like(easy_case.wheel_mask)
    rect[50:200, 100:400] = True
    assert wheel_shape(rect).score < 0.2
    bulge = easy_case.wheel_mask.copy()
    ys, xs = np.nonzero(bulge)
    yr = int(ys[np.argmax(xs)])  # the row where the wheel reaches furthest right
    bulge[yr - 25 : yr + 25, xs.max() - 10 : xs.max() + 90] = True  # an exhaust
    assert wheel_shape(bulge).score < wheel.score
    assert wheel_shape(np.zeros_like(rect)).score == 0.0
    assert "shape" in wheel.text()


def test_dedupe_drops_near_copies(easy_case) -> None:  # type: ignore[no-untyped-def]
    m = easy_case.wheel_mask
    shifted = np.roll(m, 1, axis=1)
    other = np.zeros_like(m)
    other[:20, :20] = True
    assert dedupe([m, shifted, other]) == [0, 2]


def test_initial_points_ring_with_the_hub_as_negative() -> None:
    obj = SceneObject(
        RefKind.TWO_WHEELER_WHEEL,
        (100, 60),
        (50, 40, 150, 140),
        rim_point=(100, 100),
        tyre_points=((55, 90), (100, 42), (145, 90), (900, 900)),
    )
    pts = _initial_points(obj, obj.box)
    assert [(p.x, p.y, p.positive) for p in pts] == [
        (55, 90, True),
        (100, 42, True),
        (145, 90, True),
        (100, 100, False),
    ]
    bare = SceneObject(RefKind.TWO_WHEELER_WHEEL, (100, 60), (50, 40, 150, 140))
    assert _initial_points(bare, bare.box) == (PointPrompt(100, 60),)


def test_choice_parsing() -> None:
    a, b = object(), object()
    shown = [a, b]
    assert _chosen({"choice": "B"}, shown) is b  # type: ignore[arg-type]
    assert _chosen({"choice": "a"}, shown) is a  # type: ignore[arg-type]
    assert _chosen({"choice": "none"}, shown) is None  # type: ignore[arg-type]
    assert _chosen({}, shown) is None  # type: ignore[arg-type]
    assert _chosen({}, [a]) is a  # type: ignore[arg-type]


@dataclass
class ManySam(BoxSam):
    """A fake SAM that also answers ``candidates``: a blob first, then the wheel."""

    asked: list[tuple[Any, Any]] = field(default_factory=list)

    async def candidates(self, image: ImageInput, box, points):  # type: ignore[no-untyped-def]
        self.asked.append((box, tuple(points)))
        wheel = self.objects[1]
        blob = np.zeros_like(wheel)
        blob[5:60, 5:200] = True
        return [
            MaskCandidate(blob, 0.95, "box", 0),
            MaskCandidate(wheel, 0.80, "box+points", 1),
            MaskCandidate(np.roll(wheel, 1, axis=0), 0.7, "points", 2),  # a near copy
        ]


def test_proposals_are_ranked_by_shape_and_the_verifier_picks(easy_case) -> None:  # type: ignore[no-untyped-def]
    sam = ManySam({1: easy_case.wheel_mask}, easy_case.wheel_mask.shape)
    ver = ScriptedVerifier(lambda what, n: {**GOOD, "choice": "A"})
    agent = VerifyRefineAgent(ver, sam)
    out = asyncio.run(
        agent.refine_reference(case_image(easy_case), reading_for(easy_case).references[0], 1, 1)
    )
    assert out.accepted and out.proposals_shown == 2
    assert out.mask is not None and out.shape is not None and out.shape.score > 0.5
    seg = out.trace[0]
    assert (
        seg.output.startswith("3 proposals, 2 distinct") and seg.decision == "verify the best two"
    )
    assert "chose A" in out.trace[1].output
    # The best shape was already wheel-like, so the first look was a close one.
    assert ver.calls == ["verify two wheeler wheel 1"]
    assert ver.images == [2]  # the two best proposals, as two images


def test_accept_on_a_wide_look_needs_a_close_look(easy_case) -> None:  # type: ignore[no-untyped-def]
    sam = BoxSam({}, easy_case.wheel_mask.shape)
    sam.objects[1] = easy_case.wheel_mask
    agent = VerifyRefineAgent(
        ScriptedVerifier(lambda what, n: GOOD), sam, accept_shape=2.0
    )  # nothing passes the shape gate, so the first look is wide
    obj = reading_for(easy_case).references[0]
    out = asyncio.run(agent.refine_reference(case_image(easy_case), obj, 1, 1))
    assert not out.accepted  # good by eye, but not wheel-shaped enough for this gate
    assert "does not fit a wheel" in out.trace[-1].decision

    agent2 = VerifyRefineAgent(ScriptedVerifier(lambda what, n: GOOD), sam, accept_shape=0.35)
    agent2.close_context = 0.5
    out2 = asyncio.run(agent2.refine_reference(case_image(easy_case), obj, 1, 1))
    assert out2.accepted


def test_wheel_rejected_by_shape_gate_even_if_called_good(easy_case) -> None:  # type: ignore[no-untyped-def]
    blob_sam = BoxSam({}, easy_case.wheel_mask.shape)  # returns the box itself: a rectangle
    ver = ScriptedVerifier(lambda what, n: GOOD)
    out = asyncio.run(
        VerifyRefineAgent(ver, blob_sam).refine_reference(
            case_image(easy_case), reading_for(easy_case).references[0], 1, 1
        )
    )
    assert not out.accepted and out.trace[-1].decision.startswith("give up: the outline does not")


# --------------------------------------------------------------------------- scene cache


def test_scene_reading_is_cached_in_memory_and_on_disk(tmp_path: Path) -> None:
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(json.loads(req.content))
        return httpx.Response(200, json=recorded("baseten_scene_response.json"))

    def make(**kw: Any) -> MuseSparkOpenAdapter:
        return MuseSparkOpenAdapter(
            api_key=KEY, transport=httpx.MockTransport(handler), scene_cache_dir=tmp_path, **kw
        )

    a = make()
    r1 = asyncio.run(a.read_scene(IMAGE))
    r2 = asyncio.run(a.read_scene(IMAGE))
    assert len(calls) == 1 and not r1.cached and r2.cached and r2.cost_usd == 0.0
    assert r2.references == r1.references
    r3 = asyncio.run(make().read_scene(IMAGE))  # a new adapter: from disk
    assert len(calls) == 1 and r3.cached
    asyncio.run(make(scene_effort="medium").read_scene(IMAGE))  # other settings: a new call
    assert len(calls) == 2 and calls[1]["reasoning_effort"] == "medium"
    assert Usage().cost_usd == 0.0


def test_model_images_are_compressed() -> None:
    noisy = np.random.default_rng(1).integers(0, 255, (600, 800, 3), dtype=np.uint8)
    img = replace(IMAGE, rgb=noisy, width=800, height=600)
    small = jpeg_for_model(img, 1280)
    big = jpeg_for_model(img, 1280, quality=95)
    assert len(small) < 0.6 * len(big)
    assert jpeg_for_model(img, 400)[:2] == b"\xff\xd8"


def test_two_proposals_go_as_two_images_on_one_grid() -> None:
    from neermattam_perception.agent import View, from_crop_grid, render_choice

    rgb = np.zeros((400, 400, 3), np.uint8)
    m = np.zeros((400, 400), bool)
    m[100:200, 100:200] = True
    images, both = render_choice(rgb, [m, m], None, (100, 100, 300, 300), out_side=200)
    assert len(images) == 2 and all(j[:2] == b"\xff\xd8" for j in images)
    assert both[:2] == b"\xff\xd8"
    assert from_crop_grid((500, 500), View((100, 100, 300, 300))) == (200, 200)


def test_water_keeps_regions_already_confirmed(easy_case) -> None:  # type: ignore[no-untyped-def]
    sam = BoxSam({}, easy_case.water_mask.shape)  # returns each prompt box as the mask
    h, w = easy_case.water_mask.shape

    def script(what: str, n: int) -> dict[str, Any]:
        if n == 0:
            return {"is_water": True, "coverage": "partial", "corrected_box": [500, 800, 900, 950]}
        return {"is_water": True, "coverage": "good"}

    agent = VerifyRefineAgent(ScriptedVerifier(script), sam)
    out = asyncio.run(agent.refine_water(case_image(easy_case), (0, 380, 200, 470), [], 16))
    assert out.accepted and out.mask is not None
    m = out.mask.mask
    assert m[400, 100] and m[int(0.85 * h), int(0.7 * w)]  # first region kept, second added
