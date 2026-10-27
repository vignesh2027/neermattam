"""Open-weights pieces that need no network and no torch: mask choice, adapters by
config, the confirmation step, the water band, the service's audio endpoint and
``captured_at``, and the batched sigma propagation."""

from __future__ import annotations

import asyncio
import io
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from neermattam_perception import geometry as g
from neermattam_perception.adapters import MetaApiAdapter, ModelError
from neermattam_perception.agent import VerifyRefineAgent
from neermattam_perception.muse_open import JsonReply, TranscriptResult, Usage
from neermattam_perception.pipeline import Pipeline, PipelineConfig
from neermattam_perception.references import ReferenceChoice, RefKind
from neermattam_perception.sam_local import (
    Sam21LocalAdapter,
    Sam31ApiAdapter,
    Sam31LocalAdapter,
    box_iou,
    choose_mask,
    mask_model_from_config,
)
from neermattam_perception.service import create_app

from .conftest import FakeMasks, FakeScene, case_image, reading_for
from .test_agent import GOOD, WATER_OK, BoxSam, ScriptedVerifier, agent_pipeline, open_reading

# --------------------------------------------------------------------------- SAM 2.1 helpers


def test_choose_mask_prefers_agreement_with_the_box() -> None:
    shape = (100, 100)
    box = (20, 20, 60, 60)
    leak = np.zeros(shape, bool)
    leak[10:90, 10:90] = True  # spills into the neighbour, high predicted IoU
    fit = np.zeros(shape, bool)
    fit[21:59, 21:59] = True  # fills the box
    sliver = np.zeros(shape, bool)
    sliver[30:35, 30:35] = True
    best, cands = choose_mask([leak, fit, sliver], [0.95, 0.80, 0.90], box)
    assert best == 1
    assert cands[1].box_agreement == pytest.approx(box_iou((21, 21, 59, 59), box))
    # Without a box, SAM's own score decides.
    assert choose_mask([leak, fit, sliver], [0.95, 0.80, 0.90], None)[0] == 0


def test_adapters_by_config_and_the_sam31_slots() -> None:
    assert isinstance(mask_model_from_config("sam2.1-local"), Sam21LocalAdapter)
    api = mask_model_from_config("sam3.1-api", api=MetaApiAdapter(""))
    assert isinstance(api, Sam31ApiAdapter) and api.mask_model_name == "sam-3.1"
    local = mask_model_from_config("sam3.1-local")
    assert isinstance(local, Sam31LocalAdapter)
    assert asyncio.run(local.ready())[0] is False
    with pytest.raises(ModelError, match="gated"):
        local.load()
    with pytest.raises(ValueError):
        mask_model_from_config("sam9")


def test_sam21_is_not_ready_before_load_and_has_no_noun_tracking() -> None:
    a = Sam21LocalAdapter()
    assert asyncio.run(a.ready()) == (False, "SAM 2.1 is not loaded")
    with pytest.raises(ModelError, match=r"SAM 3\.1"):
        asyncio.run(a.track(None, "plastic bottle"))  # type: ignore[arg-type]


# --------------------------------------------------------------------------- agent extras


@dataclass
class ConfirmingVerifier(ScriptedVerifier):
    """A quick check that always says yes, and a full check scripted separately."""

    confirm_script: Any = None
    confirms: list[str] = field(default_factory=list)

    async def confirm_image(self, jpeg: bytes, prompt: str, *, what: str) -> JsonReply:
        self.confirms.append(what)
        return JsonReply(self.confirm_script(what), Usage(2000, 3000), 20000, "careful")


def test_full_reasoning_check_can_overrule_a_quick_accept(easy_case) -> None:  # type: ignore[no-untyped-def]
    sam = BoxSam({1: easy_case.wheel_mask}, easy_case.wheel_mask.shape)
    ver = ConfirmingVerifier(
        lambda what, n: GOOD,
        confirm_script=lambda what: {
            "is_target": True,
            "outline_quality": "partial",
            "reason": "the outline takes in the mudguard",
        },
    )
    out = asyncio.run(
        VerifyRefineAgent(ver, sam).refine_reference(
            case_image(easy_case), reading_for(easy_case).references[0], 1, 1
        )
    )
    assert not out.accepted
    assert [t.step for t in out.trace] == ["segment", "verify", "confirm"]
    assert out.trace[1].decision == "accept"
    assert out.trace[2].decision == "give up: outline only partly right"
    assert out.cost_usd == pytest.approx(Usage(1000, 500).cost_usd + Usage(2000, 3000).cost_usd)


def test_confirmed_accept(easy_case) -> None:  # type: ignore[no-untyped-def]
    sam = BoxSam({1: easy_case.wheel_mask}, easy_case.wheel_mask.shape)
    ver = ConfirmingVerifier(lambda what, n: GOOD, confirm_script=lambda what: GOOD)
    out = asyncio.run(
        VerifyRefineAgent(ver, sam).refine_reference(
            case_image(easy_case), reading_for(easy_case).references[0], 1, 1
        )
    )
    assert out.accepted and ver.confirms == ["confirm two wheeler wheel 1"]


def test_water_band_is_added_only_where_the_waterline_was_seen(easy_case) -> None:  # type: ignore[no-untyped-def]
    # Water that stops 6 px short of the tyre: no contact for the geometry.
    gap = easy_case.water_mask.copy()
    ys, xs = np.nonzero(easy_case.wheel_mask)
    near = np.zeros_like(gap)
    near[ys.max() - 40 : ys.max() + 6, xs.min() - 5 : xs.max() + 5] = True
    gap &= ~near

    def script(what: str, n: int) -> dict[str, Any]:
        return WATER_OK if what == "verify water" else {**GOOD, "waterline_visible": True}

    p, sam, _ = agent_pipeline(easy_case, open_reading(easy_case), script)
    sam.objects[16] = gap
    sam.objects[40] = easy_case.water_mask  # what SAM finds in the band below the wheel
    r = asyncio.run(p.measure(image=case_image(easy_case), hint_diameter_cm=43.4))
    assert r.usable and r.method == "wheel_geometry", r.reasons
    band = [t for t in r.agent_trace if t.target == "water at wheels"]
    assert len(band) == 1 and band[0].decision == "join to the verified water"
    assert any(o.object_id == 40 for o in sam.prompts)

    p2, sam2, _ = agent_pipeline(easy_case, open_reading(easy_case))  # waterline not reported
    sam2.objects[16] = gap
    r2 = asyncio.run(p2.measure(image=case_image(easy_case), hint_diameter_cm=43.4))
    assert not any(t.target == "water at wheels" for t in r2.agent_trace)
    assert r2.method == "model_estimate"  # no contact, so no measurement


# --------------------------------------------------------------------------- squash


def test_squash_sigma_is_half_the_correction() -> None:
    ref = ReferenceChoice(RefKind.AUTO_WHEEL, 42.6, 2.0, "catalogue:4.00-8", "auto", 1.9)
    assert ref.squash_sigma_cm == pytest.approx(0.95)
    assert ReferenceChoice(RefKind.CAR_WHEEL, 60.0, 1.5, "x").squash_sigma_cm == 0.0


# --------------------------------------------------------------------------- service


@dataclass
class FakeTranscriber:
    fail: bool = False
    seen: list[tuple[bytes, str]] = field(default_factory=list)

    async def transcribe(self, audio: bytes, media_type: str) -> TranscriptResult:
        self.seen.append((audio, media_type))
        if self.fail:
            raise ModelError("transcription failed with HTTP 400: audio not supported")
        return TranscriptResult(
            "Velachery la mutti alavu thanni",
            "ta+en",
            "Velachery",
            "mutti alavu",
            45.0,
            13.5,
            Usage(900, 200),
            4000,
        )


def _client(easy_case, transcriber=None) -> TestClient:  # type: ignore[no-untyped-def]
    scene = FakeScene(reading_for(easy_case))
    masks = FakeMasks({1: easy_case.wheel_mask, 2: easy_case.rim_mask, 16: easy_case.water_mask})
    pipe = Pipeline(scene, masks, PipelineConfig(geometry=g.GeometryConfig(mc_draws=5)))
    return TestClient(create_app(pipe, None, transcriber))  # type: ignore[arg-type]


def test_transcribe_endpoint(easy_case) -> None:  # type: ignore[no-untyped-def]
    t = FakeTranscriber()
    with _client(easy_case, t) as c:
        r = c.post("/v1/transcribe", files={"audio": ("v.m4a", b"\x00\x01audio", "audio/x-m4a")})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["place_text"] == "Velachery" and body["depth_cm"] == 45.0
        assert body["depth_sigma_cm"] == 13.5 and body["model"] == "muse-spark-open"
        assert t.seen == [(b"\x00\x01audio", "audio/mp4")]
        assert (
            c.post("/v1/transcribe", files={"audio": ("v.txt", b"x", "text/plain")}).status_code
            == 415
        )
        assert (
            c.post("/v1/transcribe", files={"audio": ("v.wav", b"", "audio/wav")}).status_code
            == 422
        )


def test_transcribe_failures_are_503(easy_case) -> None:  # type: ignore[no-untyped-def]
    with _client(easy_case, None) as c:
        r = c.post("/v1/transcribe", files={"audio": ("v.wav", b"RIFF", "audio/wav")})
        assert r.status_code == 503 and "no audio model" in r.json()["detail"]
    with _client(easy_case, FakeTranscriber(fail=True)) as c:
        r = c.post("/v1/transcribe", files={"audio": ("v.wav", b"RIFF", "audio/wav")})
        assert r.status_code == 503 and "audio model unavailable" in r.json()["detail"]


def test_measure_body_carries_captured_at(easy_case) -> None:  # type: ignore[no-untyped-def]
    buf = io.BytesIO()
    im = Image.fromarray(easy_case.image)
    exif = im.getexif()
    exif.get_ifd(0x8769)[36867] = "2015:12:02 07:56:12"
    im.save(buf, format="JPEG", exif=exif)
    with _client(easy_case) as c:
        r = c.post("/v1/measure", files={"image": ("p.jpg", buf.getvalue(), "image/jpeg")})
    assert r.status_code == 200
    assert r.json()["captured_at"] == "2015-12-02T07:56:12"
    assert r.headers["x-captured-at"] == "2015-12-02T07:56:12"
    assert r.json()["agent_trace"][0]["step"] == "scene"


def test_service_loads_local_masks_once_at_start(easy_case) -> None:  # type: ignore[no-untyped-def]
    @dataclass
    class Loadable(FakeMasks):
        loads: int = 0

        def load(self) -> None:
            self.loads += 1

    masks = Loadable({1: easy_case.wheel_mask, 16: easy_case.water_mask})
    pipe = Pipeline(FakeScene(reading_for(easy_case)), masks)
    with TestClient(create_app(pipe)) as c:
        assert c.get("/healthz").status_code == 200
        c.get("/readyz")
    assert masks.loads == 1


# --------------------------------------------------------------------------- sigma propagation


def test_batched_propagation_tracks_the_monte_carlo_and_is_faster(easy_case) -> None:  # type: ignore[no-untyped-def]
    args = (easy_case.wheel_mask, easy_case.water_mask, 43.4, 0.0)
    kw = {"rim_mask": easy_case.rim_mask}
    base = {"model_sigma_table": (), "model_bias_table": (), "mc_draws": 60, "seed": 3}
    t0 = time.perf_counter()
    mc = g.measure_wheel_depth(*args, g.GeometryConfig(uncertainty="monte_carlo", **base), **kw)
    t_mc = time.perf_counter() - t0
    t0 = time.perf_counter()
    lin = g.measure_wheel_depth(*args, g.GeometryConfig(uncertainty="linear", **base), **kw)
    t_lin = time.perf_counter() - t0
    assert isinstance(mc, g.DepthMeasurement) and isinstance(lin, g.DepthMeasurement)
    assert lin.depth_cm == pytest.approx(mc.depth_cm)  # only the sigma differs
    assert 0.5 < lin.depth_sigma_cm / mc.depth_sigma_cm < 2.0
    assert t_lin < t_mc
    again = g.measure_wheel_depth(*args, g.GeometryConfig(uncertainty="linear", **base), **kw)
    assert isinstance(again, g.DepthMeasurement) and again.depth_sigma_cm == lin.depth_sigma_cm
