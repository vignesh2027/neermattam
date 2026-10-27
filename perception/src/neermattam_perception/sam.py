"""SAM 3.1 over the Meta Model API: request bodies, prompt grammar, stream parsing, masks.

Wire format (https://dev.meta.ai/docs/sam/reading-segmentation): the response
streams one ``output_text`` lane, one line per frame::

    <Nf>id<|box;x1=..;y1=..;x2=..;y2=..;w=W;h=H[;c=..]|><|mask;x=0;y=0[;c=..];data=H,W,<enc>payload|>,id...

Boxes have inclusive ``x2``/``y2`` in source pixels. Masks are box-local
rasters, ``H`` rows by ``W`` columns, base85 encoded; ``~`` marks the lossless
encoding (the default) and ``!`` the compact ``one_bit`` encoding. Box and
point prompts always come back ``one_bit``.

Parsing and mask decoding use Meta's official ``meta-sam-parser`` package
(``meta_sam_parser``), the Python port of ``@meta-sam/parser``. This module adds
the transport-free pieces around it: request bodies, the SSE reader, and the
placement of box-local masks into full-frame coordinates.
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterable, AsyncIterator, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import cv2
import numpy as np
from meta_sam_parser import (
    FramePrompt,
    ImageSegmentationResult,
    PromptBox,
    PromptObject,
    PromptPoint,
    SegmentationMaskRecord,
    SegmentationResult,
    VideoSegmentationResult,
    build_object_prompt,
    decode_mask_to_raster,
    image_segmentation_format,
    parse_responses_stream,
    video_segmentation_format,
)
from numpy.typing import NDArray

BASE_URL = "https://api.meta.ai/v1"
MODEL = "sam-3.1"
MAX_PROMPTED_OBJECTS = 16  # per the Segmenting-with-prompts limits
IMAGE_PRICE_USD = 2.50 / 1000.0  # per image segmented
VIDEO_FRAME_PRICE_USD = 0.20 / 1000.0  # per video frame segmented
VIDEO_MIN_FRAMES = 50  # each video request is billed for at least 50 frames

MaskEncoding = Literal["lossless", "one_bit"]


# --------------------------------------------------------------------------- prompts


@dataclass(frozen=True, slots=True)
class PointPrompt:
    x: int
    y: int
    positive: bool = True


@dataclass(frozen=True, slots=True)
class ObjectPrompt:
    """One object to segment, under an id we choose.

    ``box`` is ``(left, top, right, bottom)`` with half-open right and bottom,
    in the media's displayed pixels.
    """

    object_id: int
    points: tuple[PointPrompt, ...] = ()
    box: tuple[int, int, int, int] | None = None
    frame: int = 0


def build_prompt(size: tuple[int, int], objects: Sequence[ObjectPrompt]) -> str:
    """Box and point prompt text in the SAM token grammar.

    Uses the official builder, which checks coordinates and emits, for example,
    ``<0f>1<|box;x1=..;..|>,2<|point;x=..;y=..;w=..;h=..|>-<|point;..|>``.
    Coordinates are clamped into the frame first, since a point the scene model
    placed on the border must not turn a whole request into a 400.
    """
    if not objects:
        raise ValueError("at least one object is required")
    if len(objects) > MAX_PROMPTED_OBJECTS:
        raise ValueError(f"SAM 3.1 accepts at most {MAX_PROMPTED_OBJECTS} prompted objects")
    w, h = size
    by_id: dict[int, list[FramePrompt]] = {}
    for obj in objects:
        box = None
        if obj.box is not None:
            left, top, right, bottom = obj.box
            left, top = max(0, min(left, w - 1)), max(0, min(top, h - 1))
            right, bottom = max(left + 1, min(right, w)), max(top + 1, min(bottom, h))
            box = PromptBox(left, top, right, bottom)
        points = [
            PromptPoint(
                max(0, min(p.x, w - 1)),
                max(0, min(p.y, h - 1)),
                "positive" if p.positive else "negative",
            )
            for p in obj.points
        ]
        by_id.setdefault(obj.object_id, []).append(
            FramePrompt(frame=obj.frame, box=box, points=points)
        )
    return build_object_prompt(
        size=(w, h),
        objects=[PromptObject(id=oid, prompts=prompts) for oid, prompts in by_id.items()],
    )


# --------------------------------------------------------------------------- requests


def image_data_url(data: bytes, media_type: str = "image/jpeg") -> str:
    return f"data:{media_type};base64,{base64.b64encode(data).decode('ascii')}"


def request_body(
    prompt: str,
    *,
    image_url: str | None = None,
    video_file_id: str | None = None,
    mask_encoding: MaskEncoding = "one_bit",
    include_confidence: bool = True,
    propagation_direction: Literal["both", "forward", "backward"] | None = None,
) -> dict[str, Any]:
    """A ``POST /v1/responses`` body for SAM 3.1 with streaming on.

    ``prompt`` is either one short noun phrase or box and point prompt text,
    never both. Metadata values are strings, as the API requires.
    """
    if (image_url is None) == (video_file_id is None):
        raise ValueError("give exactly one of image_url or video_file_id")
    media: dict[str, str] = (
        {"type": "input_image", "image_url": image_url}
        if image_url is not None
        else {"type": "input_video", "file_id": str(video_file_id)}
    )
    metadata: dict[str, str] = {"mask_encoding": mask_encoding}
    if include_confidence:
        metadata["include_confidence"] = "true"
    if propagation_direction is not None:
        metadata["propagation_direction"] = propagation_direction
    return {
        "model": MODEL,
        "stream": True,
        "metadata": metadata,
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}, media],
            }
        ],
    }


# --------------------------------------------------------------------------- streams


async def sse_events(lines: AsyncIterable[str]) -> AsyncIterator[dict[str, Any]]:
    """Server-sent events to Responses event dicts.

    Joins multi-line ``data:`` fields, skips comments and keep-alives, and stops
    at ``[DONE]``.
    """
    buf: list[str] = []
    async for raw in lines:
        line = raw.rstrip("\r\n")
        if not line:
            if buf:
                data = "\n".join(buf)
                buf = []
                if data == "[DONE]":
                    return
                yield json.loads(data)
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            buf.append(line[5:].lstrip(" "))
    if buf:
        data = "\n".join(buf)
        if data != "[DONE]":
            yield json.loads(data)


async def parse_image_stream(events: AsyncIterable[dict[str, Any]]) -> ImageSegmentationResult:
    """Parse a streamed image response with the official parser."""
    return await parse_responses_stream(events, image_segmentation_format()).final_result()


async def parse_video_stream(events: AsyncIterable[dict[str, Any]]) -> VideoSegmentationResult:
    parsed = parse_responses_stream(events, video_segmentation_format())
    async with parsed:
        async for _ in parsed:
            pass
        return await parsed.final_result()


def parse_text(text: str, media: Literal["image", "video"] = "image") -> SegmentationResult:
    """Parse already-accumulated output text (a non-streaming response, or a log)."""
    from meta_sam_parser import CompletedOutcome

    fmt = image_segmentation_format() if media == "image" else video_segmentation_format()
    parser = fmt.create_parser()
    parser.push(text if text.endswith("\n") or not text else text + "\n", emit=False)
    return parser.finish(CompletedOutcome()).result


# --------------------------------------------------------------------------- masks


@dataclass(frozen=True, slots=True)
class ObjectMask:
    """One object's mask on one frame, placed in full-frame coordinates."""

    object_id: str
    frame: int
    mask: NDArray[np.bool_] = field(repr=False)
    box: tuple[int, int, int, int]  # left, top, right, bottom (half-open)
    confidence: float | None

    @property
    def area(self) -> int:
        return int(self.mask.sum())

    def centroid(self) -> tuple[float, float] | None:
        ys, xs = np.nonzero(self.mask)
        if xs.size == 0:
            return None
        return float(xs.mean()), float(ys.mean())


def place_mask(record: SegmentationMaskRecord, width: int, height: int) -> NDArray[np.bool_]:
    """Decode a box-local mask and place it into a ``height`` x ``width`` frame.

    The raster's origin is the box's top-left corner (``x=0;y=0`` on the wire).
    In the documented examples the raster is exactly the box size (for example
    ``x1=211..x2=270`` with ``data=27,60``: 60 columns for inclusive 211 to 270).
    If a raster ever differs from its box, it is scaled onto the box with
    nearest-neighbour sampling, as Meta's own sticker recipe does.
    """
    m = record.mask
    raster = np.frombuffer(decode_mask_to_raster(m), dtype=np.uint8).reshape(m.height, m.width)
    b = record.bounds
    left, top = int(b.left), int(b.top)
    bw, bh = int(b.right) - left, int(b.bottom) - top
    if (bh, bw) != raster.shape and bw > 0 and bh > 0:
        raster = np.asarray(
            cv2.resize(raster, (bw, bh), interpolation=cv2.INTER_NEAREST), dtype=np.uint8
        )
    out = np.zeros((height, width), dtype=bool)
    x0, y0 = max(0, left), max(0, top)
    x1, y1 = min(width, left + raster.shape[1]), min(height, top + raster.shape[0])
    if x1 > x0 and y1 > y0:
        out[y0:y1, x0:x1] = raster[y0 - top : y1 - top, x0 - left : x1 - left] > 0
    return out


def collect_masks(result: SegmentationResult, width: int, height: int) -> list[ObjectMask]:
    """Every mask as a full-frame :class:`ObjectMask`, latest revision per object and frame."""
    latest: dict[tuple[int, str], SegmentationMaskRecord] = {}
    for rec in result.records:
        if rec.kind != "mask":
            continue
        frame = 0 if rec.frame is None else rec.frame.frame_index
        key = (frame, rec.object_id)
        if key not in latest or rec.revision >= latest[key].revision:
            latest[key] = rec
    out = []
    for (frame, oid), rec in sorted(latest.items()):
        b = rec.bounds
        out.append(
            ObjectMask(
                object_id=oid,
                frame=frame,
                mask=place_mask(rec, width, height),
                box=(int(b.left), int(b.top), int(b.right), int(b.bottom)),
                confidence=rec.confidence,
            )
        )
    return out


def result_errors(result: SegmentationResult) -> list[str]:
    """Human-readable problems: an incomplete outcome or error diagnostics."""
    problems = []
    if result.outcome.status != "completed":
        reason = getattr(result.outcome, "reason", "unknown")
        problems.append(f"segmentation incomplete ({reason})")
    problems += [f"line {d.line}: {d.message}" for d in result.diagnostics if d.severity == "error"]
    return problems


def tracks_from_masks(masks: Iterable[ObjectMask]) -> dict[str, list[tuple[int, float, float]]]:
    """Per-object ``(frame, centroid_x, centroid_y)`` lists sorted by frame."""
    tracks: dict[str, list[tuple[int, float, float]]] = {}
    for m in masks:
        c = m.centroid()
        if c is not None:
            tracks.setdefault(m.object_id, []).append((m.frame, c[0], c[1]))
    for v in tracks.values():
        v.sort()
    return tracks


def segmentation_cost_usd(media: Literal["image", "video"], frames: int = 1) -> float:
    if media == "image":
        return IMAGE_PRICE_USD
    return max(frames, VIDEO_MIN_FRAMES) * VIDEO_FRAME_PRICE_USD
