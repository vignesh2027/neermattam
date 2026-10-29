"""The Modal SAM adapter (wire format, zoomed crops, errors) and the local fallback."""

from __future__ import annotations

import asyncio
import base64
import io
import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import numpy as np
import pytest
from PIL import Image

from neermattam_perception.adapters import MaskResult, ModelError, SceneObject
from neermattam_perception.agent import _initial_points
from neermattam_perception.media import image_from_rgb
from neermattam_perception.modal_sam import FallbackMasks, ModalSamAdapter, rle_decode
from neermattam_perception.references import RefKind
from neermattam_perception.sam import ObjectMask, ObjectPrompt, PointPrompt
from neermattam_perception.sam_local import MaskCandidate


def rle_encode(mask: np.ndarray) -> dict[str, Any]:
    """The server's encoding: column-major runs, starting with zeros."""
    flat = mask.flatten(order="F").astype(np.uint8)
    counts, current, run = [], 0, 0
    for v in flat:
        if v == current:
            run += 1
        else:
            counts.append(run)
            current, run = int(v), 1
    counts.append(run)
    return {"size": list(mask.shape), "counts": counts}


def test_rle_round_trip() -> None:
    m = np.zeros((7, 5), bool)
    m[1:4, 2:5] = True
    m[0, 0] = True  # starts with a run of ones: a leading zero-length run
    assert np.array_equal(rle_decode(rle_encode(m)), m)
    with pytest.raises(ValueError):
        rle_decode({"size": [2, 2], "counts": [1, 1]})


PHOTO = np.zeros((600, 800, 3), np.uint8)
IMG = image_from_rgb(PHOTO)


@dataclass
class FakeService:
    """Answers /segment with a disc where the box or points are, in the frame sent."""

    seen: list[dict[str, Any]] = field(default_factory=list)
    status: int = 200

    def __call__(self, req: httpx.Request) -> httpx.Response:
        assert req.headers["authorization"] == "Bearer tok"
        if req.url.path.endswith("/health"):
            return httpx.Response(200, json={"status": "ok"})
        if self.status != 200:
            return httpx.Response(self.status, json={"detail": "busy"})
        body = json.loads(req.content)
        self.seen.append(body)
        im = Image.open(io.BytesIO(base64.b64decode(body["image_b64"])))
        w, h = im.size
        results = []
        for p in body["prompts"]:
            if "box" in p:
                x0, y0, x1, y1 = p["box"]
            else:
                xs = [q[0] for q in p["positive_points"]]
                ys = [q[1] for q in p["positive_points"]]
                x0, y0, x1, y1 = min(xs) - 10, min(ys) - 10, max(xs) + 10, max(ys) + 10
            yy, xx = np.mgrid[0:h, 0:w]
            cx, cy, r = (x0 + x1) / 2, (y0 + y1) / 2, min(x1 - x0, y1 - y0) / 2
            disc = (xx - cx) ** 2 + (yy - cy) ** 2 <= r * r
            results.append(
                {
                    "id": p["id"],
                    "candidates": [
                        {
                            "index": 0,
                            "score": 0.9,
                            "bbox": None,
                            "area": int(disc.sum()),
                            "rle": rle_encode(disc),
                        }
                    ],
                }
            )
        return httpx.Response(
            200,
            json={"results": results, "image_size": [w, h], "timings_ms": {"embed": 9000.0}},
        )


def adapter(svc: FakeService, **kw: Any) -> ModalSamAdapter:
    return ModalSamAdapter(
        url="https://modal.example", token="tok", transport=httpx.MockTransport(svc), **kw
    )


def test_small_objects_are_sent_zoomed_and_placed_back() -> None:
    svc = FakeService()
    a = adapter(svc)
    box = (400, 300, 460, 360)  # small: segmented in a crop around it
    pts = (PointPrompt(430, 305), PointPrompt(430, 330, positive=False))
    cands = asyncio.run(a.candidates(IMG, box, pts))
    sent = svc.seen[0]
    assert sent["model"] == "large" and sent["max_masks"] == 3
    labels = [p["id"] for p in sent["prompts"]]
    assert labels == ["box+points", "points"]  # never a box without the points
    first = sent["prompts"][0]
    assert first["negative_points"] and first["positive_points"]
    sent_img = Image.open(io.BytesIO(base64.b64decode(sent["image_b64"])))
    assert sent_img.size[0] < 800  # a crop, not the photo
    disc = cands[0].mask
    ys, xs = np.nonzero(disc)
    assert disc.shape == (600, 800)
    assert abs(xs.mean() - 430) < 3 and abs(ys.mean() - 330) < 3  # back in photo pixels
    assert a.last_timings_ms["embed"] == 9000.0


def test_large_regions_go_whole_and_shrunk_and_segment_groups_one_frame() -> None:
    svc = FakeService()
    a = adapter(svc, max_side=400)
    water = ObjectPrompt(16, (PointPrompt(400, 500),), (0, 300, 800, 600))
    big = ObjectPrompt(17, (), (100, 100, 700, 500))
    res = asyncio.run(a.segment(IMG, [water, big]))
    assert len(svc.seen) == 1  # both in one request: one embedding
    sent_img = Image.open(io.BytesIO(base64.b64decode(svc.seen[0]["image_b64"])))
    assert sent_img.size == (400, 300)
    assert res.masks[16].mask.shape == (600, 800) and res.masks[16].area > 0
    cy = np.nonzero(res.masks[17].mask)[0].mean()
    assert abs(cy - 300) < 3


def test_service_failures_are_model_errors_without_the_token() -> None:
    a = adapter(FakeService(status=503))
    with pytest.raises(ModelError, match="503") as exc:
        asyncio.run(a.candidates(IMG, (10, 10, 50, 50), (PointPrompt(20, 20),)))
    assert "tok" not in str(exc.value)
    assert "tok" not in repr(a)
    assert asyncio.run(ModalSamAdapter().ready())[0] is False


@dataclass
class LocalStub:
    mask_model_name: str = "sam-2.1-hiera-small"
    loaded: bool = False
    loads: int = 0
    calls: int = 0

    def load(self) -> None:
        self.loads += 1
        self.loaded = True

    async def segment(self, image, objects):  # type: ignore[no-untyped-def]
        self.calls += 1
        m = np.zeros((600, 800), bool)
        m[:10, :10] = True
        return MaskResult(
            {o.object_id: ObjectMask(str(o.object_id), 0, m, (0, 0, 10, 10), 0.5) for o in objects}
        )

    async def candidates(self, image, box, points):  # type: ignore[no-untyped-def]
        self.calls += 1
        return [MaskCandidate(np.ones((600, 800), bool), 0.5, "box", 0)]

    async def ready(self):  # type: ignore[no-untyped-def]
        return True, "ok"


def test_fallback_to_local_when_modal_fails_then_retries_later() -> None:
    svc = FakeService(status=503)
    local = LocalStub()
    fb = FallbackMasks(adapter(svc), local, cooldown_s=0.0)
    out = asyncio.run(fb.candidates(IMG, (10, 10, 50, 50), (PointPrompt(20, 20),)))
    assert local.loads == 1 and local.calls == 1 and out[0].variant == "box"
    assert "fallback" in fb.mask_model_name and "503" in fb.failures[0]
    svc.status = 200  # Modal is back; the cooldown has passed
    out2 = asyncio.run(fb.candidates(IMG, (100, 100, 160, 160), (PointPrompt(130, 130),)))
    assert out2[0].variant == "box+points" and fb.mask_model_name.endswith("(Modal)")
    res = asyncio.run(fb.segment(IMG, [ObjectPrompt(1, (), (100, 100, 160, 160))]))
    assert 1 in res.masks
    assert asyncio.run(fb.ready()) == (True, "ok")


def test_fallback_stays_local_during_the_cooldown() -> None:
    svc = FakeService(status=503)
    local = LocalStub()
    fb = FallbackMasks(adapter(svc), local, cooldown_s=60.0)
    asyncio.run(fb.segment(IMG, [ObjectPrompt(1, (), (10, 10, 50, 50))]))
    svc.status = 200
    asyncio.run(fb.segment(IMG, [ObjectPrompt(1, (), (10, 10, 50, 50))]))
    assert local.calls == 2 and len(svc.seen) == 0


def test_initial_points_send_the_scene_models_exclusions() -> None:
    obj = SceneObject(
        RefKind.TWO_WHEELER_WHEEL,
        (100, 60),
        (50, 40, 150, 140),
        rim_point=(100, 100),
        tyre_points=((55, 90), (100, 42)),
        exclude_points=((160, 80), (100, 100), (900, 900)),
    )
    pts = _initial_points(obj, obj.box)
    assert [(p.x, p.y, p.positive) for p in pts] == [
        (55, 90, True),
        (100, 42, True),
        (100, 100, False),  # the hub, once
        (160, 80, False),  # the exhaust next to the tyre
    ]


def test_modal_calls_have_a_time_limit() -> None:
    async def slow(req: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json={})

    a = ModalSamAdapter(
        url="https://modal.example",
        token="tok",
        transport=httpx.MockTransport(slow),
        timeout_s=-14.8,
    )
    with pytest.raises(ModelError, match="took longer than"):
        asyncio.run(a.candidates(IMG, (10, 10, 50, 50), (PointPrompt(20, 20),)))
    assert asyncio.run(a.ready()) == (False, "Modal SAM did not answer in time")
