"""Shared test helpers: SAM wire encoding, SSE framing, fake models, synthetic scenes.

No test touches the network. SAM output is built in the documented wire
grammar and encoded with the official one_bit codec from ``meta_sam_parser``,
so the parser path under test is the real one.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pytest
from meta_sam_parser._mask_codec import _encode_raster  # official encoder, test use only

from neermattam_perception import sam
from neermattam_perception.adapters import (
    FRESH_SCENE,
    ImageQuality,
    MaskResult,
    SceneObject,
    SceneReading,
    TrackResult,
)
from neermattam_perception.media import ImageInput, VideoInput, image_from_rgb
from neermattam_perception.references import RefKind
from neermattam_perception.synth import SynthCase, SynthParams, render

FIXTURES = Path(__file__).parent / "fixtures"


def docs_fixture_line() -> str:
    lines = (FIXTURES / "sam_docs_video_frame0.txt").read_text().splitlines()
    return next(line for line in lines if not line.startswith("#"))


# --------------------------------------------------------------------------- wire encoding


def encode_record(
    object_id: int, mask: np.ndarray, frame_w: int, frame_h: int, confidence: float | None = None
) -> str:
    """One ``id<|box..|><|mask..|>`` record for a full-frame boolean mask."""
    ys, xs = np.nonzero(mask)
    x1, x2, y1, y2 = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
    crop = mask[y1 : y2 + 1, x1 : x2 + 1].astype(np.uint8)
    h, w = crop.shape
    payload = _encode_raster(crop.ravel().tolist(), w, h)
    c = f";c={confidence}" if confidence is not None else ""
    box = f"<|box;x1={x1};y1={y1};x2={x2};y2={y2};w={frame_w};h={frame_h}{c}|>"
    msk = f"<|mask;x=0;y=0{c};data={h},{w},{payload}|>"
    return f"{object_id}{box}{msk}"


def encode_line(
    frame: int,
    objects: Sequence[tuple[int, np.ndarray]],
    frame_w: int,
    frame_h: int,
    confidence: float | None = None,
) -> str:
    recs = [encode_record(i, m, frame_w, frame_h, confidence) for i, m in objects if m.any()]
    return f"<{frame}f>" + ",".join(recs)


def sse_stream(lines: Sequence[str], *, finalize: str = "content_part") -> str:
    """Server-sent events for a SAM response whose output text is ``lines``.

    One ``response.output_text.delta`` per line, as the docs describe, then the
    lane is closed by ``response.content_part.done`` (what the API sends) and
    the stream ends with ``response.completed``.
    """
    lane = {"item_id": "msg_1", "output_index": 0, "content_index": 0}
    events: list[dict[str, object]] = [
        {"type": "response.created", "response": {"id": "resp_1", "status": "in_progress"}},
    ]
    text = ""
    for line in lines:
        delta = line + "\n"
        text += delta
        events.append({"type": "response.output_text.delta", **lane, "delta": delta})
    if finalize == "content_part":
        events.append(
            {
                "type": "response.content_part.done",
                **lane,
                "part": {"type": "output_text", "text": text},
            }
        )
    else:
        events.append({"type": "response.output_text.done", **lane, "text": text})
    events.append({"type": "response.completed", "response": {"id": "resp_1"}})
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)


# --------------------------------------------------------------------------- fakes


@dataclass
class FakeScene:
    reading: SceneReading
    scene_model_name: str = "muse-spark-1.3"
    name: str = "fake-scene"
    is_ready: bool = True
    calls: int = 0
    # Whether each call was asked for a fresh reading (adapters.FRESH_SCENE).
    fresh_seen: list[bool] = field(default_factory=list)

    async def read_scene(self, image: ImageInput) -> SceneReading:
        self.calls += 1
        self.fresh_seen.append(FRESH_SCENE.get())
        return self.reading

    async def ready(self) -> tuple[bool, str]:
        return (True, "ok") if self.is_ready else (False, "fake not ready")


@dataclass
class FakeMasks:
    """Answers each prompted id with a stored mask, round-tripped through the wire format."""

    by_id: dict[int, np.ndarray]
    tracks: dict[str, list[tuple[int, float, float]]] = field(default_factory=dict)
    mask_model_name: str = "sam-3.1"
    name: str = "fake-masks"
    last_prompt: str = ""

    async def segment(self, image: ImageInput, objects: Sequence[sam.ObjectPrompt]) -> MaskResult:
        self.last_prompt = sam.build_prompt((image.width, image.height), objects)
        wanted = [
            (o.object_id, self.by_id[o.object_id]) for o in objects if o.object_id in self.by_id
        ]
        line = encode_line(0, wanted, image.width, image.height, confidence=0.9)
        result = sam.parse_text(line, "image")
        masks = sam.collect_masks(result, image.width, image.height)
        return MaskResult({int(m.object_id): m for m in masks}, sam.segmentation_cost_usd("image"))

    async def track(self, video: VideoInput, concept: str) -> TrackResult:
        return TrackResult(self.tracks, max(video.n_frames, 50), 0.01)

    async def ready(self) -> tuple[bool, str]:
        return True, "ok"


def reading_for(
    case: SynthCase,
    *,
    water: bool = True,
    kind: RefKind = RefKind.TWO_WHEELER_WHEEL,
    waterline: bool = True,
) -> SceneReading:
    """What a perfect scene model would say about a synthetic case."""
    ys, xs = np.nonzero(case.wheel_mask)
    # A tyre point: just inside the top of the outline.
    top = int(ys.min())
    cols = xs[ys == top]
    point = (int(np.median(cols)), top + 3)
    rys, rxs = np.nonzero(case.rim_mask)
    rim_point = (int(np.median(rxs)), int(np.median(rys))) if rxs.size else None
    wy, wx = np.nonzero(case.water_mask)
    order = np.argsort(wy)[::-1]
    water_pts = tuple((int(wx[i]), int(wy[i])) for i in order[:: max(1, len(order) // 3)][:3])
    wl: tuple[tuple[int, int], ...] = ()
    if waterline:
        p = np.array(case.waterline_point_px)
        d = np.array(case.waterline_dir)
        half = 0.3 * float(xs.max() - xs.min())
        wl = (tuple(np.rint(p - half * d).astype(int)), tuple(np.rint(p + half * d).astype(int)))
    return SceneReading(
        water_visible=water,
        water_points=water_pts,
        references=(
            SceneObject(
                kind,
                point,
                (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1),
                rim_point,
                None,
                None,
                0.9,
            ),
        ),
        waterline_points=wl,  # type: ignore[arg-type]
        floating_debris=None,
        quality=ImageQuality(0.9),
    )


@pytest.fixture(scope="session")
def easy_case() -> SynthCase:
    """A moderate, clean-ish scene: scooter wheel, 35 percent submerged, side-on."""
    p = SynthParams(
        diameter_cm=43.4,
        depth_fraction=0.35,
        yaw_deg=15.0,
        pitch_deg=10.0,
        roll_deg=2.0,
        wheel_px=220.0,
        tyre_width_frac=0.1,
        mask_noise_px=0.4,
        ripple_px=0.5,
        width=640,
        height=480,
    )
    return render(p, np.random.default_rng(11))


def case_image(case: SynthCase) -> ImageInput:
    return image_from_rgb(case.image)


@pytest.fixture(autouse=True)
def _every_text_has_a_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every reason and step decision any test produces must map to a catalogued
    code (codes.py): a new English template without a code fails the suite."""
    from neermattam_perception import codes, pipeline

    def strict(fn):  # type: ignore[no-untyped-def]
        def wrapped(text: str):  # type: ignore[no-untyped-def]
            out = fn(text)
            assert out["code"] != "other", f"no code for: {text!r}"
            return out

        return wrapped

    monkeypatch.setattr(pipeline, "reason_code", strict(codes.reason_code))
    monkeypatch.setattr(pipeline, "decision_code", strict(codes.decision_code))
