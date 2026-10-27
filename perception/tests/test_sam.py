from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import numpy as np
import pytest

from neermattam_perception import sam

from .conftest import docs_fixture_line, encode_line, sse_stream


async def _lines(text: str) -> AsyncIterator[str]:
    for line in text.split("\n"):
        yield line


def _parse_sse(text: str, media: str = "image"):  # type: ignore[no-untyped-def]
    events = sam.sse_events(_lines(text))
    if media == "image":
        return asyncio.run(sam.parse_image_stream(events))
    return asyncio.run(sam.parse_video_stream(events))


def disc(h: int, w: int, cx: float, cy: float, r: float) -> np.ndarray:
    yy, xx = np.mgrid[0:h, 0:w]
    return (xx - cx) ** 2 + (yy - cy) ** 2 <= r * r


def test_documented_lossless_example_decodes() -> None:
    """The verbatim example from Meta's docs: two objects, lossless (~) masks."""
    result = sam.parse_text(docs_fixture_line(), "video")
    assert result.diagnostics == ()
    masks = sam.collect_masks(result, 320, 334)
    assert [m.object_id for m in masks] == ["0", "1"]
    m0 = masks[0]
    assert m0.box == (211, 228, 271, 255)  # x2, y2 are inclusive on the wire
    ys, xs = np.nonzero(m0.mask)
    assert xs.min() >= 211 and xs.max() <= 270 and ys.min() >= 228 and ys.max() <= 254
    assert m0.area > 0 and m0.confidence is None


def test_one_bit_round_trip_and_placement() -> None:
    h, w = 120, 160
    a = disc(h, w, 50, 60, 20)
    b = disc(h, w, 120, 40, 15)
    line = encode_line(0, [(3, a), (7, b)], w, h, confidence=0.875)
    assert "!" in line.split("data=")[1][:12]  # one_bit marker
    masks = {m.object_id: m for m in sam.collect_masks(sam.parse_text(line), w, h)}
    assert np.array_equal(masks["3"].mask, a)
    assert np.array_equal(masks["7"].mask, b)
    assert masks["3"].confidence == pytest.approx(0.875)


def test_mask_raster_smaller_than_box_is_scaled() -> None:
    """If a raster ever differs from its box, it is stretched onto the box."""
    from meta_sam_parser._mask_codec import _encode_raster

    raster = np.zeros((5, 5), np.uint8)
    raster[1:4, 1:4] = 1
    payload = _encode_raster(raster.ravel().tolist(), 5, 5)
    line = f"<0f>1<|box;x1=10;y1=20;x2=19;y2=29;w=64;h=64|><|mask;x=0;y=0;data=5,5,{payload}|>"
    (m,) = sam.collect_masks(sam.parse_text(line), 64, 64)
    ys, xs = np.nonzero(m.mask)
    assert (xs.min(), xs.max(), ys.min(), ys.max()) == (12, 17, 22, 27)


def test_box_clipped_at_frame_edge() -> None:
    h, w = 50, 50
    a = disc(h, w, 0, 0, 10)  # in the corner
    (m,) = sam.collect_masks(sam.parse_text(encode_line(0, [(1, a)], w, h)), w, h)
    assert np.array_equal(m.mask, a)


def test_streamed_image_response_end_to_end() -> None:
    h, w = 80, 100
    a = disc(h, w, 30, 40, 12)
    text = sse_stream([encode_line(0, [(1, a)], w, h)])
    result = _parse_sse(text)
    assert result.outcome.status == "completed"
    assert sam.result_errors(result) == []
    (m,) = sam.collect_masks(result, w, h)
    assert np.array_equal(m.mask, a)


def test_stream_finalised_by_output_text_done() -> None:
    a = disc(40, 40, 20, 20, 8)
    result = _parse_sse(sse_stream([encode_line(0, [(1, a)], 40, 40)], finalize="output_text"))
    assert result.outcome.status == "completed" and len(sam.collect_masks(result, 40, 40)) == 1


def test_zero_matches_is_a_valid_empty_result() -> None:
    result = _parse_sse(sse_stream([]))
    assert result.outcome.status == "completed"
    assert sam.collect_masks(result, 10, 10) == []


def test_video_frames_can_skip_and_reorder() -> None:
    h, w = 60, 60
    lines = [
        encode_line(30, [(1, disc(h, w, 20, 30, 6)), (2, disc(h, w, 45, 30, 6))], w, h),
        encode_line(32, [(1, disc(h, w, 24, 30, 6))], w, h),  # frame 31 omitted, object 2 gone
        encode_line(29, [(1, disc(h, w, 18, 30, 6))], w, h),  # backward pass arrives last
    ]
    result = _parse_sse(sse_stream(lines), media="video")
    masks = sam.collect_masks(result, w, h)
    assert sorted((m.frame, m.object_id) for m in masks) == [
        (29, "1"),
        (30, "1"),
        (30, "2"),
        (32, "1"),
    ]
    tracks = sam.tracks_from_masks(masks)
    assert [f for f, _, _ in tracks["1"]] == [29, 30, 32]
    assert tracks["1"][0][1] == pytest.approx(18, abs=0.5)


def test_malformed_line_is_a_diagnostic_not_a_crash() -> None:
    result = sam.parse_text(
        "<0f>1<|box;x1=5;y1=5;x2=1;y2=1;w=10;h=10|><|mask;x=0;y=0;data=1,1,!x|>"
    )
    assert sam.collect_masks(result, 10, 10) == []
    assert sam.result_errors(result)


def test_failed_response_raises() -> None:
    from meta_sam_parser import ResponsesStreamFailedError

    text = 'data: {"type": "response.failed", "response": {"error": {"message": "boom"}}}\n\n'
    with pytest.raises(ResponsesStreamFailedError):
        _parse_sse(text)


def test_prompt_builder_matches_documented_example() -> None:
    text = sam.build_prompt(
        (1800, 1200),
        [
            sam.ObjectPrompt(1, box=(444, 626, 676, 850)),
            sam.ObjectPrompt(
                2, (sam.PointPrompt(1430, 700), sam.PointPrompt(1300, 450, positive=False))
            ),
        ],
    )
    assert text == (
        "<0f>1<|box;x1=444;y1=626;x2=675;y2=849;w=1800;h=1200|>,"
        "2<|point;x=1430;y=700;w=1800;h=1200|>-<|point;x=1300;y=450;w=1800;h=1200|>"
    )


def test_prompt_builder_clamps_and_limits() -> None:
    text = sam.build_prompt((100, 50), [sam.ObjectPrompt(1, (sam.PointPrompt(140, -3),))])
    assert text == "<0f>1<|point;x=99;y=0;w=100;h=50|>"
    with pytest.raises(ValueError):
        sam.build_prompt(
            (10, 10), [sam.ObjectPrompt(i, (sam.PointPrompt(1, 1),)) for i in range(17)]
        )
    with pytest.raises(ValueError):
        sam.build_prompt((10, 10), [])


def test_request_body_shape() -> None:
    body = sam.request_body("<0f>1<|point;x=1;y=1;w=4;h=4|>", image_url="data:image/png;base64,AA")
    assert body["model"] == "sam-3.1" and body["stream"] is True
    assert body["metadata"] == {"mask_encoding": "one_bit", "include_confidence": "true"}
    content = body["input"][0]["content"]
    assert content[0]["type"] == "input_text" and content[1]["type"] == "input_image"
    video = sam.request_body(
        "ping pong ball", video_file_id="file-1", propagation_direction="forward"
    )
    assert video["input"][0]["content"][1] == {"type": "input_video", "file_id": "file-1"}
    assert video["metadata"]["propagation_direction"] == "forward"
    with pytest.raises(ValueError):
        sam.request_body("x")


def test_costs() -> None:
    assert sam.segmentation_cost_usd("image") == pytest.approx(0.0025)
    assert sam.segmentation_cost_usd("video", 20) == pytest.approx(0.01)  # 50-frame minimum
    assert sam.segmentation_cost_usd("video", 300) == pytest.approx(0.06)


def test_sse_reader_handles_comments_and_done() -> None:
    async def collect() -> list[dict[str, object]]:
        text = ': keep-alive\n\ndata: {"type": "a"}\n\ndata: [DONE]\n\ndata: {"type": "b"}\n\n'
        return [e async for e in sam.sse_events(_lines(text))]

    assert asyncio.run(collect()) == [{"type": "a"}]
