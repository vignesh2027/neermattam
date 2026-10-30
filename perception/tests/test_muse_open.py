"""Muse Spark Open on Baseten: JSON extraction and repair, retries, parsing, cost.

Responses are recorded from the live API (tests/fixtures/baseten_*.json), so
the envelope (``content`` plus ``reasoning_content``, usage with reasoning
token details) is the real shape. No test touches the network.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import httpx
import numpy as np
import pytest

from neermattam_perception.adapters import ModelError
from neermattam_perception.media import image_from_rgb
from neermattam_perception.muse_open import (
    JsonExtractError,
    MuseSparkOpenAdapter,
    Usage,
    extract_json,
    parse_open_scene,
)
from neermattam_perception.references import RefKind

FIXTURES = Path(__file__).parent / "fixtures"
KEY = "test-secret-key-123"


def recorded(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def with_content(payload: dict, content: str) -> dict:
    p = json.loads(json.dumps(payload))
    p["choices"][0]["message"]["content"] = content
    return p


async def _no_sleep(_: float) -> None:
    return None


def adapter(handler, **kw) -> MuseSparkOpenAdapter:  # type: ignore[no-untyped-def]
    return MuseSparkOpenAdapter(
        api_key=KEY,
        base_url="https://inference.example/v1",
        model="meta-models/muse-spark-open",
        transport=httpx.MockTransport(handler),
        sleep=_no_sleep,
        **kw,
    )


IMAGE = image_from_rgb(np.full((675, 900, 3), 90, dtype=np.uint8))


# --------------------------------------------------------------------------- JSON


def test_extract_json_bare_fenced_and_prose() -> None:
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n{"a": 2}\n```') == {"a": 2}
    assert extract_json('Here is the answer:\n```\n{"a": 3}\n```\nDone.') == {"a": 3}
    assert extract_json('Sure. {"a": {"b": "x}y"}, "c": [1, 2]} hope that helps') == {
        "a": {"b": "x}y"},
        "c": [1, 2],
    }
    assert extract_json('{"a": [1, 2,], "b": 1,}') == {"a": [1, 2], "b": 1}


def test_extract_json_failures() -> None:
    for bad in ("", "   ", "no json here", "[1, 2]", '{"a": '):
        with pytest.raises(JsonExtractError):
            extract_json(bad)


# --------------------------------------------------------------------------- scene


def test_scene_from_recorded_response() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=recorded("baseten_scene_response.json"))

    reading = asyncio.run(adapter(handler).read_scene(IMAGE))
    body = json.loads(seen[0].content)
    assert seen[0].headers["authorization"] == f"Bearer {KEY}"
    assert seen[0].url.path.endswith("/chat/completions")
    assert body["model"] == "meta-models/muse-spark-open"
    assert body["reasoning_effort"] == "low"
    parts = body["messages"][0]["content"]
    assert parts[0]["type"] == "image_url"
    assert parts[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert "0-1000 grid" in parts[1]["text"]
    # The recorded answer: three people and one scooter wheel, 900 x 675 px.
    assert reading.water_visible
    kinds = [r.kind for r in reading.references]
    assert kinds == [RefKind.PERSON] * 3 + [RefKind.TWO_WHEELER_WHEEL]
    first = reading.references[0]
    assert first.box == (254, 548, 308, 627)  # [282,812,341,928] on the grid
    assert first.depth_cm == 105 and first.depth_reason
    assert first.visible_fraction == pytest.approx(0.42)
    assert reading.water_box == (342, 604, 424, 641)
    assert reading.overall_depth_cm == 100 and reading.overall_low_cm == 85
    # Cost from the recorded usage at Baseten's list price.
    u = Usage(1556, 4660, 4241, 128)
    assert reading.cost_usd == pytest.approx(u.cost_usd)
    assert u.cost_usd == pytest.approx((1428 * 1.25 + 128 * 0.15 + 4660 * 4.25) / 1e6)


def test_parse_scene_tolerates_bad_fields() -> None:
    data = {
        "water_visible": "yes",
        "water": {"box": [10, 10, 10, 10], "point": {"x": 500, "y": 900}},
        "references": [
            {"kind": "spaceship", "box": [0, 0, 10, 10]},
            {"kind": "car_wheel", "box": [600, 700, 400, 900], "point": None},
            {"kind": "car_wheel"},
            "junk",
        ],
        "image_quality": {"score": "bad"},
    }
    r = parse_open_scene(data, 1000, 500)
    assert r.water_visible is True
    assert r.water_box is None  # degenerate box dropped
    assert r.water_points == ((500, 450),)
    assert len(r.references) == 1
    ref = r.references[0]
    assert ref.box == (400, 350, 601, 451)  # corners sorted
    assert ref.point == (500, 400)  # box centre when the point is missing
    assert r.quality.score == 0.5
    assert r.overall_depth_cm is None
    assert r.faces == ()


def test_parse_scene_people_and_faces() -> None:
    data = {
        "water_visible": True,
        "water": {"point": [500, 900]},
        "references": [
            {
                "kind": "person",
                "box": [100, 200, 200, 800],
                "point": [150, 500],
                "head_top": [150, 205],
                "posture": "Wading",
                "adult": False,
                "landmark": "mid_thigh",
            }
        ],
        "faces": [[130, 200, 170, 260], "junk", [5, 5, 5, 5], {"box": [300, 200, 340, 260]}],
    }
    r = parse_open_scene(data, 1000, 500)
    p = r.references[0]
    assert p.head_top == (150, 102) and p.posture == "wading" and p.adult is False
    assert p.landmark == "mid_thigh"
    assert r.faces == ((130, 100, 171, 131), (300, 100, 341, 131))


# --------------------------------------------------------------------------- repair and retries


def test_json_repair_retry() -> None:
    rec = recorded("baseten_verify_response.json")
    replies = [with_content(rec, "The outline looks fine to me."), rec]
    bodies: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json=replies[len(bodies) - 1])

    r = asyncio.run(adapter(handler).ask_image(b"\xff\xd8jpeg", "check", what="verify"))
    assert r.repaired
    assert r.data["view"] == "head_on" and r.data["corrected_box"] == [484, 398, 537, 652]
    assert len(bodies) == 2
    msgs = bodies[1]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert "JSON object only" in msgs[2]["content"]
    assert bodies[0]["reasoning_effort"] == "low"
    one = Usage.from_payload(rec["usage"])
    assert r.usage.completion_tokens == 2 * one.completion_tokens


def test_json_repair_gives_up_after_one_retry() -> None:
    rec = with_content(recorded("baseten_verify_response.json"), "still prose")
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json=rec)

    with pytest.raises(ModelError, match="did not return JSON after one repair"):
        asyncio.run(adapter(handler).ask_image(b"x", "check", what="verify"))
    assert len(calls) == 2


def test_retries_on_429_and_5xx_with_backoff() -> None:
    rec = recorded("baseten_verify_response.json")
    statuses = [429, 503, 200]
    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)

    def handler(req: httpx.Request) -> httpx.Response:
        st = statuses.pop(0)
        if st == 200:
            return httpx.Response(200, json=rec)
        headers = {"retry-after": "3"} if st == 429 else {}
        return httpx.Response(st, json={"error": {"message": "busy"}}, headers=headers)

    a = adapter(handler, critical_retries=3)  # the backoff itself (default: one retry)
    a.sleep = sleep
    r = asyncio.run(a.ask_image(b"x", "check", what="verify"))
    assert r.data["is_target"] is False
    assert slept == [3.0, 4.0]  # Retry-After honoured, then exponential backoff (2 * 2^1)


def test_timeouts_retry_then_fail_cleanly() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=req)

    a = adapter(handler, max_retries=2)
    with pytest.raises(ModelError, match="ReadTimeout") as exc:
        asyncio.run(a.ask_image(b"x", "check", what="verify"))
    assert KEY not in str(exc.value)


def test_client_errors_are_not_retried_and_never_leak_the_key() -> None:
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    with pytest.raises(ModelError) as exc:
        asyncio.run(adapter(handler).read_scene(IMAGE))
    assert len(calls) == 1
    assert "401" in str(exc.value) and KEY not in str(exc.value)


def test_missing_key() -> None:
    a = MuseSparkOpenAdapter(api_key="")
    assert asyncio.run(a.ready()) == (False, "BASETEN_API_KEY is not set")
    with pytest.raises(ModelError, match="BASETEN_API_KEY"):
        asyncio.run(a.read_scene(IMAGE))


def test_from_env_reads_dotenv_without_printing(tmp_path: Path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    for k in ("BASETEN_API_KEY", "BASETEN_BASE_URL", "BASETEN_MODEL"):
        monkeypatch.delenv(k, raising=False)
    (tmp_path / ".env").write_text(
        'BASETEN_API_KEY="abc"\nBASETEN_BASE_URL=https://x/v1\nBASETEN_MODEL=m\nOTHER=1\n'
    )
    monkeypatch.chdir(tmp_path)
    a = MuseSparkOpenAdapter.from_env()
    assert (a.api_key, a.base_url, a.model) == ("abc", "https://x/v1", "m")
    assert "abc" not in capsys.readouterr().out
    assert "abc" not in repr(a)


# --------------------------------------------------------------------------- audio


def test_transcribe_sends_audio_and_widens_sigma() -> None:
    seen: list[dict] = []
    answer = {
        "text": "Velachery main road la mutti alavu thanni irukku",
        "language": "ta+en",
        "place_text": "Velachery main road",
        "depth_words": "mutti alavu (knee deep)",
        "depth_cm": 45,
        "depth_low_cm": 35,
        "depth_high_cm": 80,
    }
    rec = with_content(recorded("baseten_verify_response.json"), json.dumps(answer))

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return httpx.Response(200, json=rec)

    t = asyncio.run(adapter(handler).transcribe(b"RIFFfake", "audio/wav"))
    text, part = seen[0]["messages"][0]["content"]
    assert text["type"] == "text"  # text first: with the audio first the model often missed it
    assert part["type"] == "audio_url"
    assert (
        part["audio_url"]["url"]
        == "data:audio/wav;base64," + base64.b64encode(b"RIFFfake").decode()
    )
    assert t.place_text == "Velachery main road" and t.language == "ta+en"
    # "mutti" (Tamil, knee) is in the phrase table: the table's knee range wins
    # over the model's own 35 to 80 cm.
    assert t.depth_cm == 45.0 and t.depth_sigma_cm == 13.5
    assert (t.depth_low_cm, t.depth_high_cm) == (37.0, 53.0)
    assert t.depth_basis.startswith("phrase table: knee")


def test_transcribe_without_depth_words() -> None:
    rec = with_content(
        recorded("baseten_verify_response.json"),
        json.dumps(
            {
                "text": "hello",
                "language": "xx",
                "place_text": "",
                "depth_words": "",
                "depth_cm": None,
            }
        ),
    )
    t = asyncio.run(adapter(lambda r: httpx.Response(200, json=rec)).transcribe(b"a", "audio/ogg"))
    assert t.depth_cm is None and t.depth_sigma_cm is None
    assert t.language == "en"  # "xx" is not a supported code; the transcript is Latin script


def test_transcribe_asks_again_when_the_audio_was_not_heard() -> None:
    rec = recorded("baseten_verify_response.json")
    empty = with_content(
        rec, '{"text": "", "language": "", "place_text": "", "depth_words": "", "depth_cm": null}'
    )
    good = with_content(
        rec,
        json.dumps(
            {
                "text": "Adyar la ankle alavu",
                "language": "ta+en",
                "place_text": "Adyar",
                "depth_words": "ankle",
                "depth_cm": 10,
            }
        ),
    )
    replies = [empty, good]
    t = asyncio.run(
        adapter(lambda r: httpx.Response(200, json=replies.pop(0))).transcribe(b"a", "audio/wav")
    )
    assert t.text == "Adyar la ankle alavu" and t.depth_cm == 10.0 and t.depth_sigma_cm == 10.0
    assert t.usage.completion_tokens == 2 * Usage.from_payload(rec["usage"]).completion_tokens

    calls = []

    def never(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json=empty)

    with pytest.raises(ModelError, match="no transcript in 3 attempts"):
        asyncio.run(adapter(never).transcribe(b"a", "audio/wav"))
    assert len(calls) == 3


def test_every_call_has_a_total_time_limit() -> None:
    async def slow(req: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json=recorded("baseten_verify_response.json"))

    a = MuseSparkOpenAdapter(
        api_key=KEY,
        base_url="https://inference.example/v1",
        transport=httpx.MockTransport(slow),
        total_timeout_s=0.2,
    )
    with pytest.raises(ModelError, match="took longer than"):
        asyncio.run(a.ask_image(b"x", "check", what="verify"))


def test_a_stuck_check_is_cut_and_asked_once_more() -> None:
    """A call that hangs is abandoned after the per-attempt limit and retried once."""
    rec = recorded("baseten_verify_response.json")
    calls: list[int] = []

    async def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            await asyncio.sleep(5.0)  # the stuck first attempt
        return httpx.Response(200, json=rec)

    a = adapter(handler, verify_attempt_timeout_s=0.2)
    r = asyncio.run(asyncio.wait_for(a.ask_image(b"\xff\xd8jpeg", "check", what="verify"), 3.0))
    assert len(calls) == 2 and r.data


def test_a_check_that_never_answers_fails_within_its_limits() -> None:
    async def handler(req: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5.0)
        return httpx.Response(500)

    a = adapter(handler, verify_attempt_timeout_s=0.2)
    with pytest.raises(ModelError, match="no answer within"):
        asyncio.run(asyncio.wait_for(a.ask_image(b"x", "check", what="verify"), 3.0))
