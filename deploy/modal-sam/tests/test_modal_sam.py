"""Tests for the SAM service's pure logic and the client, with no network and no model."""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import ClassVar

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import client
import sam_core

# --------------------------------------------------------------------------- RLE


@pytest.mark.parametrize("seed", range(5))
def test_rle_round_trip_random(seed: int) -> None:
    rng = np.random.default_rng(seed)
    m = rng.random((37, 53)) > 0.6
    rle = sam_core.rle_encode(m)
    assert rle["size"] == [37, 53]
    assert sum(rle["counts"]) == 37 * 53
    assert np.array_equal(client.rle_decode(rle), m)


def test_rle_starts_with_zeros_run() -> None:
    m = np.zeros((4, 5), dtype=bool)
    m[0, 0] = True  # first pixel set: the zeros run is empty
    rle = sam_core.rle_encode(m)
    assert rle["counts"][0] == 0
    assert np.array_equal(client.rle_decode(rle), m)


def test_rle_is_column_major_like_coco() -> None:
    m = np.array([[0, 1], [0, 1]], dtype=bool)  # right column set
    assert sam_core.rle_encode(m)["counts"] == [2, 2]
    m = np.array([[0, 0], [1, 1]], dtype=bool)  # bottom row set
    assert sam_core.rle_encode(m)["counts"] == [1, 1, 1, 1]


def test_rle_empty_and_full() -> None:
    empty = np.zeros((3, 3), dtype=bool)
    full = np.ones((3, 3), dtype=bool)
    assert sam_core.rle_encode(empty)["counts"] == [9]
    assert sam_core.rle_encode(full)["counts"] == [0, 9]
    assert not client.rle_decode(sam_core.rle_encode(empty)).any()
    assert client.rle_decode(sam_core.rle_encode(full)).all()


def test_rle_decode_rejects_bad_counts() -> None:
    with pytest.raises(ValueError):
        client.rle_decode({"size": [2, 2], "counts": [1, 1]})


def test_mask_bbox_exclusive() -> None:
    m = np.zeros((10, 10), dtype=bool)
    m[2:5, 3:7] = True
    assert sam_core.mask_bbox(m) == [3, 2, 7, 5]
    assert sam_core.mask_bbox(np.zeros((4, 4), dtype=bool)) is None


# --------------------------------------------------------------------------- prompts


def test_parse_prompts_clamps_and_orders() -> None:
    (p,) = sam_core.parse_prompts(
        [{"id": "a", "box": [120, 90, -5, 10], "positive_points": [[200, 5]]}], 100, 80
    )
    assert p.id == "a"
    assert p.box == (0.0, 10.0, 100.0, 80.0)
    assert p.positive_points == ((99.0, 5.0),)


def test_parse_prompts_default_ids_and_negative_points() -> None:
    ps = sam_core.parse_prompts(
        [
            {"positive_points": [[1, 1]], "negative_points": [[2, 2]]},
            {"box": [0, 0, 5, 5]},
        ],
        10,
        10,
    )
    assert [p.id for p in ps] == [0, 1]
    assert ps[0].negative_points == ((2.0, 2.0),)


@pytest.mark.parametrize(
    "prompts",
    [
        [],
        "x",
        [{"id": 1}],  # nothing to segment
        [{"id": 1, "negative_points": [[1, 1]]}],  # only negatives
        [{"id": 1, "box": [1, 2, 3]}],
        [{"id": 1, "box": [5, 5, 5, 9]}],  # zero width
        [{"id": 1, "box": [0, 0, 4, 4]}, {"id": 1, "box": [0, 0, 4, 4]}],
        [{"id": True, "box": [0, 0, 4, 4]}],
        [{"id": 1, "positive_points": [[1, "a"]]}],
        [{"id": i, "box": [0, 0, 4, 4]} for i in range(sam_core.MAX_PROMPTS + 1)],
    ],
)
def test_parse_prompts_rejects(prompts: object) -> None:
    with pytest.raises(sam_core.PromptError):
        sam_core.parse_prompts(prompts, 10, 10)


def test_unknown_and_gated_models() -> None:
    with pytest.raises(sam_core.ModelUnavailable):
        sam_core.SamRunner(key="sam-9")
    r = sam_core.SamRunner(key="sam3.1", token=None)
    pytest.importorskip("torch")
    pytest.importorskip("transformers")
    with pytest.raises(sam_core.ModelUnavailable, match="gated"):
        r.load()


def test_client_prompt_json() -> None:
    p = client.SamPrompt(id=3, box=(1, 2, 3, 4), negative_points=((5, 6),))
    assert p.to_json() == {"id": 3, "box": [1.0, 2.0, 3.0, 4.0], "negative_points": [[5.0, 6.0]]}


def test_client_needs_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODAL_SAM_URL", raising=False)
    monkeypatch.delenv("MODAL_SAM_TOKEN", raising=False)
    with pytest.raises(client.SamServiceError):
        client.segment(b"x", [client.SamPrompt(1, box=(0, 0, 1, 1))])


# --------------------------------------------------------------------------- client over HTTP


class _Fake(BaseHTTPRequestHandler):
    seen: ClassVar[list[dict]] = []

    def log_message(self, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        if self.headers.get("Authorization") != "Bearer secret":
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"detail": "missing or wrong bearer token"}')
            return
        body = json.loads(raw)
        _Fake.seen.append(body)
        m = np.zeros((6, 8), dtype=bool)
        m[1:4, 2:5] = True
        results = [
            {
                "id": p["id"],
                "candidates": [
                    {
                        "index": 2,
                        "score": 0.9,
                        "bbox": sam_core.mask_bbox(m),
                        "area": int(m.sum()),
                        "rle": sam_core.rle_encode(m),
                    }
                ],
            }
            for p in body["prompts"]
        ]
        out = {
            "model": "sam-2.1-hiera-large",
            "device": "cuda",
            "image_size": [8, 6],
            "results": results,
            "timings_ms": {"embed": 1.0},
        }
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture()
def fake_url() -> str:
    srv = HTTPServer(("127.0.0.1", 0), _Fake)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_client_segment_decodes_masks(fake_url: str) -> None:
    res = client.segment(
        b"jpeg-bytes",
        [client.SamPrompt(1, box=(0, 0, 4, 4)), {"id": "w", "positive_points": [[3, 3]]}],
        url=fake_url,
        token="secret",
        ipv4=False,
    )
    assert set(res.masks) == {1, "w"}
    best = res.masks[1][0]
    assert best.mask.shape == (6, 8) and best.area == 9 and best.bbox == (2, 1, 5, 4)
    assert res.image_size == (8, 6)
    assert _Fake.seen[-1]["prompts"][1] == {"id": "w", "positive_points": [[3, 3]]}


def test_client_reports_http_errors(fake_url: str) -> None:
    with pytest.raises(client.SamServiceError) as exc:
        client.segment(b"x", [client.SamPrompt(1, box=(0, 0, 1, 1))], url=fake_url, token="no")
    assert exc.value.status == 401
