from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from neermattam_perception import flow as fl
from neermattam_perception import velocity as v
from neermattam_perception.adapters import ModelError
from neermattam_perception.media import ImageInput, VideoInput, image_from_rgb
from neermattam_perception.muse_open import (
    DEBRIS_VERSION,
    PROMPT_VERSIONS,
    JsonReply,
    Usage,
    prompt_digest,
)
from neermattam_perception.person import HEAD_TO_SHOULDER, PersonMeasurement
from neermattam_perception.pipeline import WATER_BELOW_ID_BASE, Pipeline, _box_tracker
from neermattam_perception.sam_local import BoxTracks, ObjectTrack, _forget_old_frames

from .conftest import FakeMasks, FakeScene, case_image
from .test_person import person_pipeline
from .test_pipeline import FAST, make_pipeline

# --------------------------------------------------------------------------- fakes


def grid(b: tuple[int, int, int, int], w: int, h: int) -> list[float]:
    return [1000 * b[0] / w, 1000 * b[1] / h, 1000 * b[2] / w, 1000 * b[3] / h]


@dataclass
class FakeLocator:
    data: dict[str, Any]
    fail: bool = False
    calls: int = 0

    async def locate_debris_json(self, image: ImageInput) -> JsonReply:
        self.calls += 1
        if self.fail:
            raise ModelError("debris locator gave no answer within 45 s")
        return JsonReply(self.data, Usage(1000, 200), 5, "")


@dataclass
class FakeTracker:
    """Moves each box's centre by a fixed number of video pixels per frame."""

    motions: list[tuple[float, float]]
    lost: dict[int, set[int]] = field(default_factory=dict)  # object index: lost frame numbers
    area_jumps: dict[int, set[int]] = field(default_factory=dict)
    mask_model_name: str = "sam-2.1-hiera-small"
    calls: list[tuple[int, list[tuple[int, int, int, int]], int, int]] = field(default_factory=list)

    async def track_boxes(
        self,
        video: VideoInput,
        start: int,
        boxes: Any,
        *,
        points: Any = None,
        stride: int = 1,
        max_frames: int = 150,
    ) -> BoxTracks:
        self.calls.append((start, list(boxes), stride, max_frames))
        self.points = points
        frames = list(range(start, video.n_frames, stride))[:max_frames]
        out = BoxTracks(start=start, stride=stride, frames=frames)
        for i, b in enumerate(boxes):
            ot = ObjectTrack(str(i + 1), b)
            cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
            vx, vy = self.motions[i]
            for f in frames:
                if f in self.lost.get(i, set()):
                    ot.lost.append(f)
                    continue
                x, y = cx + vx * (f - start), cy + vy * (f - start)
                ot.points.append((f, x, y))
                ot.areas.append(900 if f in self.area_jumps.get(i, set()) else 25)
                ot.boxes.append((int(x) - 2, int(y) - 2, int(x) + 3, int(y) + 3))
            out.objects[str(i + 1)] = ot
        return out


W, H = 640, 480  # video
KW, KH = 320, 240  # key frame, half size: every pixel length halves


def with_blobs(rgb: np.ndarray, centres: list[tuple[int, int]]) -> np.ndarray:
    """Dark 5x5 blobs: the floating objects the debris boxes are snapped onto."""
    out = rgb.copy()
    for x, y in centres:
        out[y - 2 : y + 3, x - 2 : x + 3] = 20
    return out


BLOBS = [(46 + 50 * i, 105) for i in range(8)] + [(20, 20)]


def make_video(fps: float = 25.0, n: int = 100, key_index: int = 0) -> VideoInput:
    grey = np.full((KH, KW, 3), 128, dtype=np.uint8)
    key = image_from_rgb(with_blobs(grey, BLOBS))
    return VideoInput(b"", "video/mp4", fps, n, W, H, key_index, key)


def debris_answer(n: int, anchor: bool = True) -> dict[str, Any]:
    items = [
        {"box": grid((40 + 50 * i, 100, 52 + 50 * i, 110), KW, KH), "point": None, "what": "leaf"}
        for i in range(n)
    ]
    out: dict[str, Any] = {"debris": items, "flow": "left to right"}
    out["anchor"] = (
        {"box": grid((10, 10, 30, 30), KW, KH), "point": [62, 83], "what": "sign"}
        if anchor
        else None
    )
    return out


ONE_CM = fl.FlowScale(1.0, 0.05, "test scale")  # 1 cm per key-frame pixel


def measure(locator: Any, tracker: Any, scale: fl.FlowScale | None = ONE_CM, **kw: Any):  # type: ignore[no-untyped-def]
    return asyncio.run(fl.measure_flow(make_video(**kw), locator, tracker, scale))


# --------------------------------------------------------------------------- parsing


def test_parse_debris_checks_every_box() -> None:
    w, h = 1000, 500
    data = {
        "debris": [
            {"box": [100, 100, 120, 120], "point": [110, 110], "what": "bottle"},
            {"box": [101, 101, 121, 121], "what": "bottle"},  # duplicate
            {"box": [0, 0, 600, 600], "what": "car"},  # far too large
            {"box": [500, 500, 501, 501]},  # too small to track
            {"box": "nonsense"},
            "not an object",
            {"box": [300, 300, 320, 330], "point": [900, 900], "what": ""},  # point outside box
        ],
        "anchor": {"box": [10, 10, 30, 30], "what": "pole"},
        "flow": "towards the camera",
    }
    r = fl.parse_debris(data, w, h)
    assert [p.what for p in r.debris] == ["bottle", "object"]
    assert r.debris[0].box == (100, 50, 121, 61) and r.debris[0].point == (110, 55)
    assert r.debris[1].point is None
    assert r.anchor is not None and r.anchor.what == "pole"
    assert r.flow == "towards the camera"
    assert len(r.dropped) == 5
    assert any("duplicate" in d for d in r.dropped)
    assert any("too large" in d for d in r.dropped)
    assert any("too small" in d for d in r.dropped)


def test_parse_debris_caps_and_bad_shapes() -> None:
    many = [{"box": [10 + 60 * i, 10, 30 + 60 * i, 30]} for i in range(12)]
    r = fl.parse_debris({"debris": many, "anchor": {"box": "x"}}, 1000, 1000)
    assert len(r.debris) == fl.MAX_DEBRIS
    assert r.anchor is None and any(d.startswith("anchor") for d in r.dropped)
    r = fl.parse_debris({"debris": {"box": [1, 2, 3, 4]}}, 100, 100)
    assert r.debris == () and r.dropped == ("debris: not a list",)


def test_debris_prompt_is_versioned() -> None:
    assert PROMPT_VERSIONS["locate-debris"] == DEBRIS_VERSION
    assert DEBRIS_VERSION.startswith("locate-debris@")
    assert prompt_digest(DEBRIS_VERSION) is not None


# --------------------------------------------------------------------------- scales


def test_person_scale_from_head_to_shoulder() -> None:
    pm = PersonMeasurement(
        depth_cm=60.0, depth_sigma_cm=12.0, visible_share=0.6, top_y=100.0, shoulder_y=130.0,
        waterline_y=200.0, x_centre=50.0, height_cm=160.0, height_sigma_cm=9.0,
    )  # fmt: skip
    s = fl.person_scale(pm)
    assert s is not None
    assert s.cm_per_px == pytest.approx(HEAD_TO_SHOULDER * 160.0 / 30.0)
    assert 0.08 < s.rel_sigma < 0.12  # stature, proportion and posture together
    assert s.at_xy == (50.0, 200.0)
    assert fl.person_scale(replace(pm, shoulder_y=100.0)) is None


def test_wheel_scale_uses_the_measurement() -> None:
    ref = SimpleNamespace(
        kind=SimpleNamespace(value="two_wheeler_wheel"),
        diameter_cm=43.4,
        diameter_sigma_cm=2.0,
        source="catalogue:90/100-10",
    )
    m = SimpleNamespace(cm_per_px=0.25, ellipse=SimpleNamespace(cx=10.0, cy=20.0))
    s = fl.wheel_scale(ref, m)  # type: ignore[arg-type]
    assert s.cm_per_px == 0.25
    assert s.rel_sigma == pytest.approx(math.hypot(2.0 / 43.4, 0.03))
    assert "two wheeler wheel" in s.source and s.at_xy == (10.0, 20.0)


# --------------------------------------------------------------------------- track checks


def test_object_track_records_lost_frames() -> None:
    ot = ObjectTrack("1", (0, 0, 4, 4))
    m = np.zeros((10, 10), dtype=bool)
    m[2:4, 6:9] = True
    ot.add(0, m, 2.0)
    ot.add(1, m, -0.5)  # SAM says the object is gone
    ot.add(2, np.zeros((10, 10), dtype=bool), 3.0)  # empty mask
    assert ot.points == [(0, 7.0, 2.5)] and ot.areas == [6] and ot.boxes == [(6, 2, 9, 4)]
    assert ot.lost == [1, 2]


def test_check_track_rules() -> None:
    ot = ObjectTrack("1", (0, 0, 4, 4))
    assert fl.check_track(ot, 10)[1] == "never found by the tracker"
    ot.points = [(f, float(f), 0.0) for f in range(4)]
    ot.areas = [20, 20, 20, 20]
    ot.lost = list(range(4, 10))
    assert "lost on 6 of 10" in str(fl.check_track(ot, 10)[1])
    ot.lost = []
    ot.points = [(f, float(f), 0.0) for f in range(10)]
    ot.areas = [20] * 9 + [200]  # one frame slid onto something larger: drop that frame
    pts, why = fl.check_track(ot, 10)
    assert why is None and len(pts) == 9 and pts[-1][0] == 8
    ot.areas = [20] * 6 + [200] * 4  # too often: the whole track goes
    assert "mask size changed" in str(fl.check_track(ot, 10)[1])


def test_track_region_covers_the_prompts_with_room() -> None:
    from neermattam_perception.sam_local import track_region

    assert track_region([(100, 200, 120, 220), (500, 300, 520, 330)], 1280, 720) == (
        0, 8, 712, 522,
    )  # fmt: skip
    assert track_region([(10, 10, 20, 20)], 200, 100) == (0, 0, 84, 84)  # 64 px at least


def test_forget_old_frames_keeps_the_memory_window() -> None:
    session = SimpleNamespace(
        processed_frames={i: f"px{i}" for i in range(30)},
        output_dict_per_obj={
            0: {
                "cond_frame_outputs": {0: "c"},
                "non_cond_frame_outputs": {i: i for i in range(1, 30)},
            }
        },
    )
    _forget_old_frames(session, 29, keep=16)
    assert 29 not in session.processed_frames
    non = session.output_dict_per_obj[0]["non_cond_frame_outputs"]
    assert 13 not in non and 14 in non and 28 in non
    assert session.output_dict_per_obj[0]["cond_frame_outputs"] == {0: "c"}  # the prompt stays


# --------------------------------------------------------------------------- measure_flow


def test_known_speed_through_the_whole_flow() -> None:
    # 4 video px per frame is 2 key px per frame; at 25 fps and 1 cm per key px, 0.5 m/s.
    tracker = FakeTracker([(4.0, 0.0)] * 3 + [(0.0, 0.0)])
    loc = FakeLocator(debris_answer(3))
    r = measure(loc, tracker)
    assert r.estimate is not None, r.reason
    assert r.estimate.surface_mps == pytest.approx(0.5, rel=1e-6)
    assert r.estimate.n_used == 3
    assert r.estimate.mean_mps == pytest.approx(v.SURFACE_TO_MEAN * 0.5)
    start, boxes, stride, max_frames = tracker.calls[0]
    assert start == 0 and stride == 2 and max_frames == int(4.0 * 25 / 2)
    assert tracker.points[3] == (40, 40)  # the anchor point (20, 20) in key px, doubled
    assert tracker.points[0] == (92, 210)  # the blob's centroid
    # The box was snapped onto the blob (44..48, 103..107, padded by 2), then doubled.
    assert boxes[0] == (84, 202, 102, 220)
    assert len(boxes) == 4  # three debris and the anchor
    assert r.anchor_used and r.anchor_shift_px == 0.0
    assert [s.step for s in r.steps] == ["debris", "track", "velocity", "velocity"]
    assert r.steps[0].prompt_version == DEBRIS_VERSION
    assert r.cost_usd == pytest.approx(Usage(1000, 200).cost_usd)
    assert all(t.kept for t in r.verdicts) and len(r.used_tracks) == 3
    assert r.verdicts[0].px_per_s == pytest.approx(50.0)  # key px per second


def test_anchor_removes_a_pan() -> None:
    # The camera pans: everything shifts -3 px per frame, the debris drift +4 on top.
    pan = FakeTracker([(1.0, 0.0)] * 3 + [(-3.0, 0.0)])
    r = measure(FakeLocator(debris_answer(3)), pan)
    assert r.estimate is not None and r.estimate.surface_mps == pytest.approx(0.5, rel=1e-6)
    assert r.anchor_used and r.anchor_shift_px and r.anchor_shift_px > 10
    no_anchor = measure(FakeLocator(debris_answer(3, anchor=False)), FakeTracker([(1.0, 0.0)] * 3))
    assert no_anchor.estimate is not None
    assert no_anchor.estimate.surface_mps == pytest.approx(0.125, rel=1e-6)  # pan not removed
    assert any("camera motion not removed" in s.decision for s in no_anchor.steps)


def test_lost_anchor_is_not_used() -> None:
    tracker = FakeTracker([(4.0, 0.0)] * 2 + [(0.0, 0.0)], lost={2: set(range(0, 100, 4))})
    r = measure(FakeLocator(debris_answer(2)), tracker)
    assert not r.anchor_used and r.estimate is not None
    assert any("anchor not usable" in s.output for s in r.steps)


def test_bad_tracks_are_rejected_with_reasons() -> None:
    tracker = FakeTracker(
        [(4.0, 0.0), (4.2, 0.2), (3.8, -0.2), (0.0, 0.0), (4.0, 0.0), (-4.0, 0.0)],
        lost={4: set(range(0, 60))},
    )
    loc = FakeLocator(debris_answer(6, anchor=False))
    r = measure(loc, tracker)
    assert r.estimate is not None, r.reason
    reasons = {t.track_id: t.reason for t in r.verdicts}
    assert reasons["4"] == v.REJECT_STILL
    assert "lost on" in str(reasons["5"])
    assert reasons["6"] == v.REJECT_DIRECTION
    assert {t.track_id for t in r.verdicts if t.kept} == {"1", "2", "3"}
    assert r.estimate.n_used == 3
    assert r.estimate.surface_mps == pytest.approx(0.5, rel=0.02)


def test_without_a_scale_motion_is_reported_in_pixels() -> None:
    r = measure(FakeLocator(debris_answer(2)), FakeTracker([(4.0, 0.0)] * 2 + [(0, 0)]), None)
    assert r.estimate is None
    assert "cannot be scaled" in r.reason and "50 px/s" in r.reason
    assert all(not t.kept and t.reason == "no scale (passed every check)" for t in r.verdicts)
    assert r.verdicts[0].px_per_s == pytest.approx(50.0)
    assert r.pixel_estimate is not None and r.pixel_estimate.surface_mps == pytest.approx(50.0)
    # A still object is named as such even without a scale.
    r = measure(
        FakeLocator(debris_answer(3)), FakeTracker([(4.0, 0), (4.0, 0), (0, 0), (0, 0)]), None
    )
    assert r.verdicts[2].reason == v.REJECT_STILL


def test_boxes_on_plain_water_are_snapped_or_dropped() -> None:
    grey = np.full((100, 200), 128, dtype=np.uint8)
    grey[50:55, 120:125] = 30  # a dark object 10 px right of the box below
    p = fl.Prompted((100, 45, 115, 60), (107, 52), "leaf")
    q, note = fl.snap_to_object(grey, p)
    assert q is not None and q.box == (118, 48, 127, 57) and q.point == (122, 52)
    assert note is not None and note.startswith("snapped") and "15 px" in note
    on = fl.Prompted((115, 45, 130, 60), None, "leaf")
    q, note = fl.snap_to_object(grey, on)
    assert q is not None and note is None  # already on it: nothing to say
    far = fl.Prompted((10, 10, 25, 25), None, "leaf")
    q, note = fl.snap_to_object(grey, far)
    assert q is None and note is not None and "nothing distinct" in note
    noise = np.random.default_rng(0).normal(128, 3, (100, 200)).clip(0, 255).astype(np.uint8)
    assert fl.snap_to_object(noise, far)[0] is None  # ripples of a few grey levels are not objects


def test_plain_water_boxes_never_reach_the_tracker() -> None:
    tracker = FakeTracker([(4.0, 0.0)] * 3)
    video = make_video()
    blank = replace(video, key_frame=image_from_rgb(np.full((KH, KW, 3), 128, dtype=np.uint8)))
    r = asyncio.run(fl.measure_flow(blank, FakeLocator(debris_answer(3)), tracker, ONE_CM))
    assert r.estimate is None and "distinct from the water" in r.reason
    assert not tracker.calls
    assert [s.decision for s in r.steps].count("drop") == 4  # three debris and the anchor


def test_nothing_floating_or_locator_down() -> None:
    tracker = FakeTracker([])
    r = measure(FakeLocator({"debris": [], "anchor": None, "flow": "not visible"}), tracker)
    assert r.estimate is None and "nothing floating" in r.reason and not tracker.calls
    r = measure(FakeLocator({}, fail=True), tracker)
    assert r.estimate is None and "locator failed" in r.reason
    assert r.steps[0].decision == "give up"


# --------------------------------------------------------------------------- pipeline


@dataclass
class LocatingScene(FakeScene):
    locator: FakeLocator | None = None

    async def locate_debris_json(self, image: ImageInput) -> JsonReply:
        assert self.locator is not None
        return await self.locator.locate_debris_json(image)


@dataclass
class TrackingMasks(FakeMasks):
    tracker: FakeTracker | None = None
    mask_model_name: str = "sam-2.1-hiera-small"

    async def track_boxes(self, video: VideoInput, start: int, boxes: Any, **kw: Any) -> BoxTracks:
        assert self.tracker is not None
        return await self.tracker.track_boxes(video, start, boxes, **kw)


def test_box_tracker_is_found_behind_a_fallback() -> None:
    t = FakeTracker([])
    assert _box_tracker(t) is t
    wrapper = SimpleNamespace(fallback=t)
    assert _box_tracker(wrapper) is t
    assert _box_tracker(SimpleNamespace()) is None


def video_for(key: ImageInput, n: int = 100, fps: float = 25.0, blobs: int = 0) -> VideoInput:
    if blobs:
        h = key.height
        key = image_from_rgb(with_blobs(key.rgb, [(25 + 30 * i, h - 35) for i in range(blobs)]))
    return VideoInput(b"", "video/mp4", fps, n, key.width, key.height, 0, key)


def debris_on(key: ImageInput, n: int) -> dict[str, Any]:
    w, h = key.width, key.height
    return {
        "debris": [
            {"box": grid((20 + 30 * i, h - 40, 30 + 30 * i, h - 30), w, h), "what": "leaf"}
            for i in range(n)
        ],
        "anchor": None,
        "flow": "left to right",
    }


def test_pipeline_uses_sam21_tracking_with_a_wheel(easy_case) -> None:  # type: ignore[no-untyped-def]
    p0, scene0, masks0 = make_pipeline(easy_case)
    key = case_image(easy_case)
    scene = LocatingScene(scene0.reading, locator=FakeLocator(debris_on(key, 3)))
    masks = TrackingMasks(masks0.by_id, tracker=FakeTracker([(3.0, 0.0)] * 3))
    got: list[fl.FlowResult] = []
    p = Pipeline(scene, masks, p0.config)
    r = asyncio.run(p.measure(video=video_for(key, blobs=3), on_flow=got.append))
    assert r.usable and r.method == "wheel_geometry", r.reasons
    assert r.velocity_mps is not None and r.velocity_sigma_mps is not None
    assert len(got) == 1 and got[0].estimate is not None and got[0].scale is not None
    s = got[0]
    expected = 3.0 * 25.0 * s.scale.cm_per_px / 100.0  # type: ignore[union-attr]
    assert s.estimate.surface_mps == pytest.approx(expected, rel=1e-6)  # type: ignore[union-attr]
    assert r.velocity_mps == pytest.approx(round(v.SURFACE_TO_MEAN * expected, 2), abs=0.011)
    steps = [t.step for t in r.agent_trace]
    assert {"debris", "track", "velocity"} <= set(steps)
    assert next(t for t in r.agent_trace if t.step == "debris").prompt_version == DEBRIS_VERSION
    assert any("surface velocity" in x for x in r.reasons)
    assert r.cost_usd > 0.0025  # the locator's call is counted


def test_pipeline_noun_phrase_path_is_unchanged(easy_case) -> None:  # type: ignore[no-untyped-def]
    # Without track_boxes (SAM 3.1), the mask model tracks the noun phrase as before.
    p, _, masks = make_pipeline(easy_case)
    key = case_image(easy_case)
    masks.tracks = {"1": [(f, 50.0 + 30.0 * f, 400.0) for f in range(20)]}
    r = asyncio.run(p.measure(video=video_for(key, 60, 30.0)))
    assert r.velocity_mps is not None
    assert not any(t.step == "debris" for t in r.agent_trace)


def test_pipeline_velocity_with_a_person_reference(easy_case) -> None:  # type: ignore[no-untyped-def]
    p0, _f, rgb, _ = person_pipeline(easy_case, 70.0)
    key = image_from_rgb(rgb)
    scene = LocatingScene(p0.scene.reading, locator=FakeLocator(debris_on(key, 2)))
    masks = TrackingMasks(p0.masks.by_id, tracker=FakeTracker([(2.0, 1.0)] * 2))
    got: list[fl.FlowResult] = []
    video = video_for(key, blobs=2)
    r = asyncio.run(Pipeline(scene, masks, FAST).measure(video=video, on_flow=got.append))
    assert r.method == "person_reference", r.reasons
    assert r.velocity_mps is not None and r.velocity_sigma_mps is not None
    s = got[0].scale
    assert s is not None and s.source.startswith("person")
    assert r.velocity_sigma_mps >= 0.08 * r.velocity_mps  # the person's scale is coarse
    assert "velocity" in r.timings_ms


def test_pipeline_velocity_when_the_water_was_not_verified(easy_case) -> None:  # type: ignore[no-untyped-def]
    # The second person path (no verified water surface) measured velocity too:
    # before, it returned without calling the flow step at all.
    p0, f, rgb, _ = person_pipeline(easy_case, 70.0)
    key = image_from_rgb(rgb)
    # No water surface; only the water just below the person, segmented locally.
    by_id = {k: m for k, m in p0.masks.by_id.items() if k != 16}  # type: ignore[attr-defined]
    by_id[WATER_BELOW_ID_BASE] = f.water
    scene = LocatingScene(p0.scene.reading, locator=FakeLocator(debris_on(key, 2)))  # type: ignore[attr-defined]
    masks = TrackingMasks(by_id, tracker=FakeTracker([(2.0, 1.0)] * 2))
    got: list[fl.FlowResult] = []
    video = video_for(key, blobs=2)
    r = asyncio.run(Pipeline(scene, masks, FAST).measure(video=video, on_flow=got.append))
    assert r.method == "person_reference", r.reasons
    assert "the water surface as a whole could not be verified" in r.reasons
    assert len(got) == 1 and got[0].scale is not None
    assert r.velocity_mps is not None
