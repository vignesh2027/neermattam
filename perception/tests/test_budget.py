"""The time budget and the choice between people: nothing starts after the deadline,
and a person reading the scene model clearly contradicts is not published."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np
import pytest

from neermattam_perception import sam
from neermattam_perception.adapters import MaskResult, SceneObject
from neermattam_perception.media import ImageInput, image_from_rgb
from neermattam_perception.person import PersonMeasurement
from neermattam_perception.pipeline import (
    PERSON_ID_BASE,
    ModelEstimate,
    Pipeline,
    PipelineConfig,
    choose_person,
    scene_conflict,
)
from neermattam_perception.references import RefKind

from .test_person import person_pipeline
from .test_pipeline import FAST


@dataclass
class FakeClock:
    """The budget's clock: ``now`` moves only when a test moves it."""

    now: float = 1000.0

    def __call__(self) -> float:
        return self.now


@dataclass
class ClockedMasks:
    """Wraps a fake mask model; records the clock time of every segment call and can
    move the clock forward by ``cost_s`` per call, as slow SAM requests would."""

    inner: Any
    clock: FakeClock
    cost_s: float = 0.0
    calls: list[tuple[float, list[int]]] = field(default_factory=list)
    mask_model_name: str = "sam-2.1-hiera-small"
    name: str = "clocked"

    async def segment(self, image: ImageInput, objects: Sequence[sam.ObjectPrompt]) -> MaskResult:
        self.calls.append((self.clock.now, [o.object_id for o in objects]))
        self.clock.now += self.cost_s
        res: MaskResult = await self.inner.segment(image, objects)
        return res

    async def track(self, video: Any, concept: str) -> Any:
        return await self.inner.track(video, concept)

    async def ready(self) -> tuple[bool, str]:
        return True, "ok"


def test_nothing_starts_after_the_deadline(easy_case) -> None:  # type: ignore[no-untyped-def]
    # One slow SAM request (60 s on the fake clock) uses the whole budget: the
    # people must then not be segmented at all, and the answer says why.
    p0, _f, rgb, _ = person_pipeline(easy_case, 70.0)
    clock = FakeClock()
    masks = ClockedMasks(p0.masks, clock, cost_s=60.0)
    p = Pipeline(p0.scene, masks, FAST, clock=clock)
    r = asyncio.run(p.measure(image=image_from_rgb(rgb)))
    deadline = 1000.0 + 55.0 - 5.0
    assert all(t < deadline for t, _ in masks.calls), masks.calls
    started_people = [ids for _, ids in masks.calls if any(i >= PERSON_ID_BASE for i in ids)]
    assert started_people == []
    assert "people were not measured: the time budget ran out" in r.reasons
    assert any(s.step == "budget" and s.target == "people" for s in r.agent_trace)
    assert r.method != "person_reference"


def test_people_are_measured_within_the_budget(easy_case) -> None:  # type: ignore[no-untyped-def]
    p0, _f, rgb, _ = person_pipeline(easy_case, 70.0)
    clock = FakeClock()
    masks = ClockedMasks(p0.masks, clock, cost_s=2.0)
    r = asyncio.run(Pipeline(p0.scene, masks, FAST, clock=clock).measure(image=image_from_rgb(rgb)))
    assert r.method == "person_reference", r.reasons


def test_water_below_people_waits_for_the_budget(easy_case) -> None:  # type: ignore[no-untyped-def]
    # The people's segmentation starts in time but ends after the deadline: the
    # second request (water just below them) is not started.
    p0, _f, rgb, _ = person_pipeline(easy_case, 70.0)
    by_id = {k: v for k, v in p0.masks.by_id.items() if k != 16}  # type: ignore[attr-defined]
    p0.masks.by_id = by_id  # type: ignore[attr-defined]
    clock = FakeClock()
    masks = ClockedMasks(p0.masks, clock, cost_s=26.0)
    r = asyncio.run(Pipeline(p0.scene, masks, FAST, clock=clock).measure(image=image_from_rgb(rgb)))
    assert all(t < 1050.0 for t, _ in masks.calls)
    assert not any(s.target == "water below people" for s in r.agent_trace)
    assert r.method != "person_reference"


@dataclass
class DeadlineAgent:
    """Records the deadline the pipeline gives the checks, then gives up on everything."""

    seen: list[float | None] = field(default_factory=list)

    async def run(self, key: Any, refs: Any, *args: Any, deadline: Any = None, **kw: Any) -> Any:
        from neermattam_perception.agent import WaterOutcome

        self.seen.append(deadline)
        return WaterOutcome(False, None, 0, "time budget"), []


def test_people_are_segmented_during_the_checks(easy_case) -> None:  # type: ignore[no-untyped-def]
    # The checks get the whole budget; the people's SAM requests were started
    # before them, so they are done when the checks end.
    p0, _f, rgb, _ = person_pipeline(easy_case, 70.0)
    clock = FakeClock()
    agent = DeadlineAgent()
    masks = ClockedMasks(p0.masks, clock)
    p = Pipeline(p0.scene, masks, FAST, agent=agent, clock=clock)  # type: ignore[arg-type]
    r = asyncio.run(p.measure(image=image_from_rgb(rgb)))
    assert agent.seen == [1000.0 + 55.0 - 5.0]
    people = [ids for _, ids in masks.calls if any(i >= PERSON_ID_BASE for i in ids)]
    assert people, masks.calls
    assert any("(segmented during the checks)" in s.input for s in r.agent_trace)


# --------------------------------------------------------------------------- choice


def reading(depth: float, sigma: float, landmark_depth: float | None = None) -> PersonMeasurement:
    return PersonMeasurement(
        depth_cm=depth, depth_sigma_cm=sigma, visible_share=0.7, top_y=10.0, shoulder_y=40.0,
        waterline_y=150.0, x_centre=50.0, height_cm=159.0, height_sigma_cm=9.0,
        landmark="shin" if landmark_depth is not None else None,
        landmark_depth_cm=landmark_depth,
    )  # fmt: skip


def person(depth_cm: float | None = None) -> SceneObject:
    return SceneObject(RefKind.PERSON, (50, 80), (30, 5, 70, 160), depth_cm=depth_cm)


MASK = np.zeros((4, 4), dtype=bool)


def test_scene_conflict_is_two_sided() -> None:
    assert scene_conflict(70.0, 13.0, 25.0, 20.0)  # deep reading, shallow scene
    assert scene_conflict(10.0, 3.0, 40.0, 20.0)  # shallow reading, deep scene
    assert not scene_conflict(28.7, 20.3, 25.0, 20.0)
    assert not scene_conflict(30.0, 3.0, 36.0, 20.0)  # within half the estimate's sigma


def test_the_reading_the_scene_contradicts_is_not_chosen() -> None:
    # The live c1 run: person 1 at 70 +- 13 cm, person 2 at 28.7 +- 20.3 cm, and
    # the scene model saw the water at the shin (about 25 cm). Smallest sigma
    # alone chose person 1.
    found = [
        (reading(70.0, 13.0, landmark_depth=25.0), person(), MASK, "person 1"),
        (reading(28.7, 20.3, landmark_depth=25.0), person(), MASK, "person 2"),
    ]
    chosen, notes = choose_person(found, None)
    assert chosen is not None and chosen[3] == "person 2"
    assert len(notes) == 1 and notes[0].startswith("person 1: measured 70 cm")


def test_every_reading_contradicted_gives_none() -> None:
    est = ModelEstimate(25.0, 20.0, "water at the shins")
    found = [(reading(70.0, 13.0), person(), MASK, "person 1")]
    chosen, notes = choose_person(found, est)
    assert chosen is None and len(notes) == 1


def test_scene_value_preference_and_agreeing_readings() -> None:
    est = ModelEstimate(25.0, 20.0, "street")
    # The scene model's own estimate at the person comes before the street estimate.
    found = [(reading(70.0, 12.0), person(depth_cm=68.0), MASK, "person 1")]
    assert choose_person(found, est)[0] is not None
    # Two readings that agree with each other are combined (1/sigma^2 weights); the
    # sigma stays at the smallest single sigma, since they share the stature prior.
    found = [
        (reading(30.0, 12.0), person(), MASK, "person 1"),
        (reading(26.0, 9.0), person(), MASK, "person 2"),
    ]
    chosen, notes = choose_person(found, est)
    assert chosen is not None and chosen[3] == "person 2"
    w1, w2 = 1 / 12.0**2, 1 / 9.0**2
    assert chosen[0].depth_cm == pytest.approx((30.0 * w1 + 26.0 * w2) / (w1 + w2))
    assert chosen[0].depth_sigma_cm == 9.0
    assert notes == ["people 1 and 2 agree: 27 cm (sigma 9 cm), their readings combined"]
    assert choose_person([], est) == (None, [])


def test_pipeline_falls_back_to_the_estimate(easy_case) -> None:  # type: ignore[no-untyped-def]
    # The person measures about 70 cm; the scene model says 20 cm at that person.
    p0, _f, rgb, _ = person_pipeline(easy_case, 70.0)
    scene = p0.scene
    ref = replace(scene.reading.references[0], depth_cm=20.0)  # type: ignore[attr-defined]
    scene.reading = replace(  # type: ignore[attr-defined]
        scene.reading,  # type: ignore[attr-defined]
        references=(ref,),
        overall_depth_cm=20.0,
        overall_reason="water at the ankles",
    )
    r = asyncio.run(Pipeline(scene, p0.masks, FAST).measure(image=image_from_rgb(rgb)))
    assert r.method == "model_estimate", r.reasons
    assert any("but the scene model sees about 20 cm there; not used" in x for x in r.reasons)
    assert any(s.decision == "do not publish a person measurement" for s in r.agent_trace)


def test_video_is_not_tracked_after_the_budget() -> None:
    cfg = PipelineConfig()
    assert cfg.video_beyond_budget is False


@dataclass
class SlowPeopleMasks:
    """A mask model whose person request takes ``delay_s`` of real time."""

    inner: Any
    delay_s: float
    mask_model_name: str = "sam-2.1-hiera-small"
    name: str = "slow"

    async def segment(self, image: ImageInput, objects: Sequence[sam.ObjectPrompt]) -> MaskResult:
        if any(PERSON_ID_BASE <= o.object_id < PERSON_ID_BASE + 10 for o in objects):
            await asyncio.sleep(self.delay_s)
        res: MaskResult = await self.inner.segment(image, objects)
        return res

    async def ready(self) -> tuple[bool, str]:
        return True, "ok"


def test_a_request_still_running_at_the_deadline_is_abandoned(easy_case) -> None:  # type: ignore[no-untyped-def]
    import time

    p0, _f, rgb, _ = person_pipeline(easy_case, 70.0)
    cfg = replace(FAST, budget_s=5.0 + 0.3)  # 0.3 s before the deadline
    p = Pipeline(p0.scene, SlowPeopleMasks(p0.masks, delay_s=5.0), cfg)
    t0 = time.perf_counter()
    r = asyncio.run(p.measure(image=image_from_rgb(rgb)))
    assert time.perf_counter() - t0 < 3.0
    assert "people were not measured: the time budget ran out" in r.reasons
    assert any(s.step == "budget" and s.target == "people" for s in r.agent_trace)


def test_readings_that_disagree_give_way_to_the_scene() -> None:
    # Both pass the guard against wide scene values, but disagree with each other:
    # the one the scene model supports best is used, not the smallest sigma.
    found = [
        (reading(80.0, 6.0), person(depth_cm=70.0), MASK, "person 1"),
        (reading(30.0, 8.0), person(depth_cm=32.0), MASK, "person 2"),
    ]
    chosen, notes = choose_person(found, None)
    assert chosen is not None and chosen[3] == "person 2" and notes == []


@dataclass
class StuckScene:
    inner: Any
    name: str = "stuck"
    scene_model_name: str = "muse-spark-open"

    async def read_scene(self, image: ImageInput) -> Any:
        await asyncio.sleep(10)
        return await self.inner.read_scene(image)

    async def ready(self) -> tuple[bool, str]:
        return True, "ok"


def test_a_stuck_scene_reading_is_cut_at_the_deadline(easy_case) -> None:  # type: ignore[no-untyped-def]
    import time

    from neermattam_perception.adapters import ModelError

    p0, _f, rgb, _ = person_pipeline(easy_case, 70.0)
    p = Pipeline(StuckScene(p0.scene), p0.masks, replace(FAST, budget_s=5.2))
    t0 = time.perf_counter()
    with pytest.raises(ModelError, match="within the time budget"):
        asyncio.run(p.measure(image=image_from_rgb(rgb)))
    assert time.perf_counter() - t0 < 2.0


def test_either_scene_statement_can_contradict() -> None:
    # The scene model's estimate at the person agrees, but the landmark it named at
    # the waterline (shin, about 25 cm) contradicts the 52 cm reading: not used.
    found = [(reading(52.2, 12.5, landmark_depth=25.0), person(depth_cm=45.0), MASK, "person 1")]
    chosen, notes = choose_person(found, None)
    assert chosen is None
    assert notes == [
        "person 1: measured 52 cm (sigma 12 cm), but the scene model sees about 25 cm there; "
        "not used"
    ]


def test_early_bands_reach_over_the_lower_body() -> None:
    from neermattam_perception.pipeline import EARLY_BAND_REACH_UP, _below_jobs

    mask = np.zeros((200, 100), dtype=bool)
    mask[20:120, 40:60] = True  # a person 100 px tall, feet at row 119
    (late,) = _below_jobs([mask], None)
    (early,) = _below_jobs([mask], None, reach_up=EARLY_BAND_REACH_UP)
    assert early[1][1] == late[1][1] - 25 and early[1][3] == late[1][3]
    # The positive points stay beside and below the feet.
    assert [p.y for p in early[2].points if p.positive] == [
        p.y for p in late[2].points if p.positive
    ]
