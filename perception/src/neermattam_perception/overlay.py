"""Measurement overlay PNG in the project palette.

Primary blue #2563EB draws the fitted wheel (near sidewall), deep violet
#4C1D95 the far sidewall and the scene model's second waterline, and deep red
#9F1239 the measured waterline and the depth label.
"""

from __future__ import annotations

import base64
import math
from collections.abc import Sequence

import cv2
import numpy as np
from numpy.typing import NDArray

from .geometry import DepthMeasurement, Ellipse, Line, vertical_diameter

PRIMARY = (0x25, 0x63, 0xEB)
VIOLET = (0x4C, 0x1D, 0x95)
DANGER = (0x9F, 0x12, 0x39)
SURFACE = (0xFF, 0xFF, 0xFF)
SKY = (0x38, 0xBD, 0xF8)


def _poly(
    img: NDArray[np.uint8],
    e: Ellipse,
    colour: tuple[int, int, int],
    width: int,
    dashed: bool = False,
) -> None:
    pts = np.rint(e.sample(240) * 16).astype(np.int32)
    if not dashed:
        cv2.polylines(img, [pts], True, colour, width, cv2.LINE_AA, shift=4)
        return
    for i in range(0, len(pts), 8):
        seg = pts[i : i + 5]
        if len(seg) >= 2:
            cv2.polylines(img, [seg], False, colour, width, cv2.LINE_AA, shift=4)


def _line(
    img: NDArray[np.uint8],
    line: Line,
    half_len: float,
    colour: tuple[int, int, int],
    width: int,
    centre: NDArray[np.float64] | None = None,
) -> None:
    c = line.point
    if centre is not None:  # the point on the line nearest to centre
        c = line.point + float((centre - line.point) @ line.direction) * line.direction
    p0 = c - half_len * line.direction
    p1 = c + half_len * line.direction
    cv2.line(
        img,
        (round(p0[0] * 16), round(p0[1] * 16)),
        (round(p1[0] * 16), round(p1[1] * 16)),
        colour,
        width,
        cv2.LINE_AA,
        shift=4,
    )


def render_overlay(
    rgb: NDArray[np.uint8],
    m: DepthMeasurement | None,
    *,
    label: str | None = None,
    second_line: Line | None = None,
    max_side: int = 1024,
    water_mask: NDArray[np.bool_] | None = None,
    object_masks: Sequence[NDArray[np.bool_]] = (),
    boxes: Sequence[tuple[int, int, int, int]] = (),
) -> NDArray[np.uint8]:
    """The measurement drawn on the photo.

    Optional context: the water mask tinted sky blue, the outlines of the
    accepted object masks in violet, and boxes (for example the objects the
    scene model's estimate is based on) in deep red.
    """
    img = np.ascontiguousarray(rgb.copy())
    scale = max(img.shape[:2]) / 900.0
    w = max(2, round(2 * scale))
    if water_mask is not None and water_mask.any():
        tint = img.astype(np.float32)
        tint[water_mask] = 0.7 * tint[water_mask] + 0.3 * np.array(SKY, dtype=np.float32)
        img = np.ascontiguousarray(tint.astype(np.uint8))
    for om in object_masks:
        contours, _ = cv2.findContours(
            np.ascontiguousarray(om, dtype=np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE
        )
        cv2.drawContours(img, contours, -1, VIOLET, max(1, w // 2), cv2.LINE_AA)
    for b in boxes:
        cv2.rectangle(img, (b[0], b[1]), (b[2], b[3]), DANGER, max(1, w // 2), cv2.LINE_AA)
    if m is not None:
        e = m.ellipse
        if m.outline.tread_px > 0:
            _poly(img, m.outline.far, VIOLET, max(1, w // 2), dashed=True)
        _poly(img, e, PRIMARY, w)
        half = 1.3 * e.a
        _top, cross, bottom = vertical_diameter(e, m.waterline.line, m.waterline.water_normal)
        _line(img, m.waterline.line, half, DANGER, w + 1, cross)
        if second_line is not None:
            _line(img, second_line, half * 0.8, VIOLET, max(1, w // 2), cross)
        # Depth bar: from the waterline down to the wheel's bottom along the vertical diameter.
        cv2.arrowedLine(
            img,
            (round(cross[0]), round(cross[1])),
            (round(bottom[0]), round(bottom[1])),
            DANGER,
            w,
            cv2.LINE_AA,
            tipLength=0.08,
        )
    if label:
        font = cv2.FONT_HERSHEY_SIMPLEX
        fs = 0.7 * scale
        th = max(1, round(1.6 * scale))
        (tw, tht), base = cv2.getTextSize(label, font, fs, th)
        pad = round(10 * scale)
        x0, y0 = pad, pad
        cv2.rectangle(img, (x0, y0), (x0 + tw + 2 * pad, y0 + tht + base + 2 * pad), SURFACE, -1)
        cv2.rectangle(img, (x0, y0), (x0 + tw + 2 * pad, y0 + tht + base + 2 * pad), DANGER, th)
        cv2.putText(img, label, (x0 + pad, y0 + pad + tht), font, fs, DANGER, th, cv2.LINE_AA)
    h, wd = img.shape[:2]
    s = min(1.0, max_side / max(h, wd))
    if s < 1.0:
        img = np.asarray(
            cv2.resize(img, (math.floor(wd * s), math.floor(h * s)), interpolation=cv2.INTER_AREA),
            dtype=np.uint8,
        )
    return img


def png_b64(rgb: NDArray[np.uint8]) -> str:
    ok, buf = cv2.imencode(".png", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
    if not ok:
        raise RuntimeError("PNG encoding failed")
    return base64.b64encode(buf.tobytes()).decode("ascii")
