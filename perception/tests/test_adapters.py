from __future__ import annotations

import asyncio
import json

import httpx
import numpy as np
import pytest

from neermattam_perception import adapters as ad
from neermattam_perception import sam
from neermattam_perception.media import image_from_rgb
from neermattam_perception.references import RefKind

from .conftest import encode_line, sse_stream

IMG = image_from_rgb(np.full((200, 400, 3), 128, np.uint8))  # width 400, height 200

SCENE_JSON = {
    "water_visible": True,
    "water_points": [{"x": 500, "y": 900}],
    "references": [
        {
            "kind": "two_wheeler_wheel",
            "point": {"x": 250, "y": 400},
            "rim_point": {"x": 250, "y": 500},
            "box": {"x_min": 200, "y_min": 300, "x_max": 300, "y_max": 700},
            "vehicle": "honda_activa_110",
            "position": "rear",
            "confidence": 0.8,
        },
        {
            "kind": "spaceship",  # not in the enum: dropped, not fatal
            "point": {"x": 1, "y": 1},
            "rim_point": {"x": 1, "y": 1},
            "box": {"x_min": 0, "y_min": 0, "x_max": 1, "y_max": 1},
            "vehicle": "unknown",
            "position": "unknown",
            "confidence": 0.1,
        },
    ],
    "waterline_points": [{"x": 200, "y": 650}, {"x": 300, "y": 650}],
    "floating_debris": "plastic bottle",
    "image_quality": {"score": 0.7, "blurred": False, "too_dark": False, "issues": []},
}


def test_parse_scene_converts_normalised_coordinates() -> None:
    r = ad.parse_scene_json(SCENE_JSON, 400, 200)
    assert r.water_visible and r.water_points == ((200, 180),)
    (ref,) = r.references
    assert ref.kind is RefKind.TWO_WHEELER_WHEEL
    assert ref.point == (100, 80) and ref.rim_point == (100, 100)
    assert ref.box == (80, 60, 121, 141)
    assert (ref.vehicle, ref.position) == ("honda_activa_110", "rear")
    assert r.waterline_points == ((80, 130), (120, 130))
    assert r.floating_debris == "plastic bottle" and r.quality.score == pytest.approx(0.7)


def test_parse_scene_clamps_edge_values() -> None:
    data = dict(SCENE_JSON, water_points=[{"x": 1001, "y": -2}], references=[])
    assert ad.parse_scene_json(data, 400, 200).water_points == ((399, 0),)


def test_schema_is_strict_and_lists_catalogue() -> None:
    s = ad.scene_schema()
    assert s["additionalProperties"] is False
    ref = s["properties"]["references"]["items"]
    assert "honda_activa_110" in ref["properties"]["vehicle"]["enum"]
    assert set(ref["required"]) == set(ref["properties"])


def make_handler(seen: list[httpx.Request], *, status: int = 200):  # type: ignore[no-untyped-def]
    mask = np.zeros((200, 400), bool)
    mask[60:140, 80:120] = True
    water = np.zeros((200, 400), bool)
    water[150:, :] = True

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "bad key"}})
        if req.url.path == "/v1/chat/completions":
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": json.dumps(SCENE_JSON)}}],
                    "usage": {"prompt_tokens": 1000, "completion_tokens": 200},
                },
            )
        if req.url.path == "/v1/responses":
            text = sse_stream([encode_line(0, [(1, mask), (16, water)], 400, 200, 0.9)])
            return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})
        if req.url.path == "/v1/models":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(404)

    return handler


def test_meta_adapter_scene_request_and_cost() -> None:
    seen: list[httpx.Request] = []
    a = ad.MetaApiAdapter("test-key", transport=httpx.MockTransport(make_handler(seen)))
    reading = asyncio.run(a.read_scene(IMG))
    assert reading.references[0].vehicle == "honda_activa_110"
    assert reading.cost_usd == pytest.approx((1000 * 1.25 + 200 * 4.25) / 1e6)
    req = seen[0]
    assert req.headers["authorization"] == "Bearer test-key"
    body = json.loads(req.content)
    assert body["model"] == "muse-spark-1.3"
    assert body["response_format"]["type"] == "json_schema"
    parts = body["messages"][0]["content"]
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_meta_adapter_segments_all_objects_in_one_request() -> None:
    seen: list[httpx.Request] = []
    a = ad.MetaApiAdapter("k", transport=httpx.MockTransport(make_handler(seen)))
    prompts = [
        sam.ObjectPrompt(1, (sam.PointPrompt(100, 100),), (80, 60, 120, 140)),
        sam.ObjectPrompt(16, (sam.PointPrompt(200, 180),)),
    ]
    res = asyncio.run(a.segment(IMG, prompts))
    assert len(seen) == 1
    body = json.loads(seen[0].content)
    assert body["model"] == "sam-3.1" and body["stream"] is True
    assert body["input"][0]["content"][0]["text"].startswith("<0f>1<|box;x1=80;y1=60;x2=119;y2=139")
    assert set(res.masks) == {1, 16}
    assert res.masks[1].area == 80 * 40
    assert res.masks[1].confidence == pytest.approx(0.9)
    assert res.cost_usd == pytest.approx(0.0025)


def test_meta_adapter_errors_never_leak_the_key() -> None:
    seen: list[httpx.Request] = []
    a = ad.MetaApiAdapter(
        "secret-123", transport=httpx.MockTransport(make_handler(seen, status=401))
    )
    with pytest.raises(ad.ModelError) as err:
        asyncio.run(a.read_scene(IMG))
    assert "401" in str(err.value) and "secret-123" not in str(err.value)
    with pytest.raises(ad.ModelError):
        asyncio.run(a.segment(IMG, [sam.ObjectPrompt(1, (sam.PointPrompt(1, 1),))]))
    assert asyncio.run(a.ready()) == (False, "Meta Model API returned HTTP 401")


def test_meta_adapter_without_key() -> None:
    a = ad.MetaApiAdapter("")
    assert asyncio.run(a.ready()) == (False, "MODEL_API_KEY is not set")
    with pytest.raises(ad.ModelError, match="MODEL_API_KEY"):
        asyncio.run(a.read_scene(IMG))


def test_meta_adapter_ready() -> None:
    a = ad.MetaApiAdapter("k", transport=httpx.MockTransport(make_handler([])))
    assert asyncio.run(a.ready()) == (True, "ok")


def test_open_weights_stub_is_explicit() -> None:
    o = ad.OpenWeightsAdapter()
    ok, why = asyncio.run(o.ready())
    assert not ok and "not implemented" in why
    with pytest.raises(NotImplementedError, match="TODO"):
        asyncio.run(o.read_scene(IMG))
    with pytest.raises(NotImplementedError):
        asyncio.run(o.segment(IMG, []))
