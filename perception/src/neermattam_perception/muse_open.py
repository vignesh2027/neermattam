"""Muse Spark Open (Meta's open-weight reasoning model) served by Baseten.

Baseten exposes it behind an OpenAI-compatible Chat Completions API. The model
always reasons first: the answer is in ``message.content`` and the reasoning in
``message.reasoning_content``. We ask for JSON in the prompt rather than through
a schema, so the reply is extracted robustly (code fences, prose around the
object) and, when that still fails, repaired once by asking for the JSON alone.

Coordinates follow Muse Spark's grounding convention: a 0 to 1000 grid over the
width and height of the image sent, (0, 0) at the top left.

Price (https://www.baseten.co/library/muse-spark-open, read 2026-10-27): input
USD 1.25, cached input USD 0.15 and output USD 4.25 per million tokens. Audio
is billed as input tokens (https://docs.baseten.co/inference/model-apis/audio).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import math
import os
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

from .adapters import (
    AnswerRejected,
    ImageQuality,
    ModelError,
    SceneObject,
    SceneReading,
    VehicleObject,
)
from .media import ImageInput
from .references import RefKind, vehicles
from .validate import check_scene, check_transcript
from .voice import depth_from_words, normalise_languages

MODEL_LABEL = "muse-spark-open"
DEFAULT_MODEL = "meta-models/muse-spark-open"
DEFAULT_BASE_URL = "https://inference.baseten.co/v1"
INPUT_USD_PER_M = 1.25
CACHED_INPUT_USD_PER_M = 0.15
OUTPUT_USD_PER_M = 4.25
PRICE_SOURCE = "https://www.baseten.co/library/muse-spark-open"

_RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}


# --------------------------------------------------------------------------- JSON


class JsonExtractError(ValueError):
    """No JSON object could be found in a model reply."""


def _balanced_objects(text: str) -> list[str]:
    """Every top-level ``{...}`` span in ``text``, skipping braces inside strings."""
    spans: list[str] = []
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                spans.append(text[start : i + 1])
                start = -1
    return spans


def _strip_trailing_commas(s: str) -> str:
    out: list[str] = []
    in_str = False
    esc = False
    for i, ch in enumerate(s):
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        if ch == ",":
            rest = s[i + 1 :].lstrip()
            if rest[:1] in ("}", "]"):
                continue
        out.append(ch)
    return "".join(out)


def extract_json(text: str | None) -> dict[str, Any]:
    """The JSON object in a model reply.

    Accepts a bare object, an object inside a fenced code block, or an object
    surrounded by prose. Trailing commas are tolerated. When several objects
    are present the largest one that parses wins.
    """
    if not text or not text.strip():
        raise JsonExtractError("empty reply")
    t = text.strip()
    candidates: list[str] = [t]
    if "```" in t:
        parts = t.split("```")
        for block in parts[1::2]:
            body = block.split("\n", 1)[1] if block[:4].lower().startswith("json") else block
            candidates.append(body.strip())
    candidates += sorted(_balanced_objects(t), key=len, reverse=True)
    for c in candidates:
        for variant in (c, _strip_trailing_commas(c)):
            try:
                obj = json.loads(variant)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(obj, dict):
                return obj
    raise JsonExtractError("no JSON object in the reply")


# --------------------------------------------------------------------------- usage


@dataclass(frozen=True, slots=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0

    @property
    def cost_usd(self) -> float:
        fresh = max(0, self.prompt_tokens - self.cached_tokens)
        return (
            fresh * INPUT_USD_PER_M
            + self.cached_tokens * CACHED_INPUT_USD_PER_M
            + self.completion_tokens * OUTPUT_USD_PER_M
        ) / 1e6

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.prompt_tokens + other.prompt_tokens,
            self.completion_tokens + other.completion_tokens,
            self.reasoning_tokens + other.reasoning_tokens,
            self.cached_tokens + other.cached_tokens,
        )

    @staticmethod
    def from_payload(u: dict[str, Any] | None) -> Usage:
        u = u or {}
        cd = u.get("completion_tokens_details") or {}
        pd = u.get("prompt_tokens_details") or {}
        return Usage(
            int(u.get("prompt_tokens") or 0),
            int(u.get("completion_tokens") or 0),
            int(cd.get("reasoning_tokens") or 0),
            int(pd.get("cached_tokens") or 0),
        )


@dataclass(frozen=True, slots=True)
class ChatReply:
    content: str
    reasoning: str
    usage: Usage
    latency_ms: int
    finish_reason: str | None = None


@dataclass(frozen=True, slots=True)
class JsonReply:
    data: dict[str, Any]
    usage: Usage  # summed over the call and any repair
    latency_ms: int
    reasoning: str
    repaired: bool = False
    cached: bool = False


# --------------------------------------------------------------------------- prompts

_KINDS = ", ".join(k.value for k in RefKind)

SCENE_PROMPT = f"""You are reading a street photo taken during urban flooding in India. Measure nothing yet: locate things precisely so a segmentation model can cut them out.

Coordinates: integers on a 0-1000 grid over this image, x from the left edge, y from the top edge. A box is [x_min, y_min, x_max, y_max]. A point is [x, y].

Return one JSON object, nothing else:
{{
  "water_visible": true or false (standing or flowing floodwater covers road or ground),
  "water": {{"box": [..] a box lying entirely on open water surface, away from people and vehicles, or null,
            "point": [..] one point on open water}},
  "references": up to 5 objects standing in the water that show its depth, best first. The best reference is a wheel of a vehicle standing upright on its wheels, seen from the side or at an angle so that the round or oval tyre is visible, with the water cutting across the tyre. List such wheels first, even small ones. Do not list as wheels: tyres seen head-on as a narrow band, vehicles lying on their side or overturned, or wheels whose waterline is hidden by splash or the photo's edge; list people standing in the water too. Each:
    {{"kind": one of {_KINDS},
      "box": [..] tight box around the visible part above the water of THIS object only (for a wheel: the tyre and rim above the water, not the bodywork, not neighbouring objects),
      "point": [..] a point inside the box on the object itself (for a wheel: on the dark tyre rubber),
      "rim_point": [..] for a wheel, a point on the metal rim or hub, else null,
      "tyre_points": for a wheel, 3 or 4 points on the black tyre rubber above the water, spread around the visible arc (for example left, top and right), else [],
      "exclude_points": for a wheel, 1 to 3 points just next to the tyre that are not tyre: the hub, the exhaust, the mudguard, a wall or another vehicle touching it, else [],
      "head_top": for a person, the top of the head (hair, not a hat or anything carried), else null,
      "posture": for a person, "standing", "walking", "bending", "sitting", "swimming" or "carrying_on_head", else null,
      "adult": for a person, true if an adult, false if a child,
      "landmark": for a person, the body part at the waterline: "ankle", "shin", "knee", "mid_thigh", "hip", "waist", "chest", "shoulder" or "neck", else null,
      "waterline": [[x, y], [x, y]] where the water surface meets the object's left and right edges, or [] if unsure,
      "visible_fraction": share of the object's full height that is above water, 0 to 1,
      "vehicle": exact model if the bodywork shows it, one of {", ".join(sorted(vehicles()))}, else "unknown",
      "position": "front", "rear" or "unknown",
      "depth_cm": your estimate of the water depth at this object in centimetres,
      "depth_reason": one short sentence: what the estimate is based on,
      "confidence": 0 to 1}},
  "vehicles": up to 4 vehicles standing in the water, nearest first. Each:
    {{"kind": "car", "two_wheeler", "auto_rickshaw", "bus", "truck" or "other",
      "box": [..] the whole vehicle as far as it is visible above the water,
      "vehicle": exact model if the bodywork shows it, one of {", ".join(sorted(vehicles()))}, else "unknown",
      "wheels_visible": true if part of any wheel's tyre shows above the water}},
  "overall": {{"depth_cm": water depth on the street where most of it is visible, "low_cm": .., "high_cm": .., "reason": one short sentence}},
  "faces": boxes [..] around every human head or face visible anywhere in the photo (anyone, in the water or not, however small), [] if none,
  "floating_debris": a short noun phrase for things floating on the water, or "",
  "image_quality": {{"score": 0 to 1, "blurred": bool, "too_dark": bool, "issues": [short phrases]}}
}}
Boxes must be tight and must not include neighbouring objects. Omit what you cannot see; never invent hidden objects. If no floodwater is visible, return water_visible false with empty references."""


# The wheel check is one template with a few parts chosen per call: one panel
# (A) or two (A and B), and the kind of wheel. Its version (see prompt_version)
# covers every fixed part, so it names the template, not one filled-in copy.
_VERIFY_INTRO = {
    2: (
        "The two images above, A (first) and B (second), show the same crop of a flood "
        "photo. In each, a segmentation model's proposed outline of the {kind_text} is drawn "
        "in bright magenta, and the box it was given in yellow. Choose the outline that best "
        "follows the tyre, then judge that one."
    ),
    1: (
        "This is a crop of a flood photo (panel A). A segmentation model's proposed outline "
        "of the {kind_text} is drawn in bright magenta, and the box it was given in yellow."
    ),
}
_VERIFY_CHOICE = {
    2: '  "choice": "A" or "B" (the better outline), or "none" if neither is the tyre,\n',
    1: '  "choice": "A", or "none" if the outline is not the tyre,\n',
}
_VERIFY_GRID = {
    2: "over image A (B shows the same crop, so its grid is the same)",
    1: "over the crop",
}
_VERIFY_BODY = """{intro} Judge strictly: the outline will be used to measure the water depth from the wheel's shape, so an outline that strays onto other parts gives a wrong depth. A straight lower edge of an outline is where it meets the water; that is expected.

Coordinates: integers on a 0-1000 grid {grid}, x from the left, y from the top. A box is [x_min, y_min, x_max, y_max].

Answer with one JSON object, nothing else:
{{
{choice}  "is_target": true only if the chosen outline is the {kind_text} itself: it follows the round outer edge of the tyre above the water, with the rim inside it. False if it takes in a mudguard, fork, suspension, exhaust, bodywork, a leg, splash, a reflection or a neighbouring object, or if it covers only the rim or only part of the tyre,
  "outline_quality": "good" only if the chosen outline follows the tyre's outer edge closely everywhere above the water; "partial" if it is the right tyre but misses or adds a noticeable part; otherwise "wrong",
  "view": "side" (the tyre's round or oval face is seen), "oblique", or "head_on" (the tyre is seen edge-on, as a narrow upright band),
  "standing": true if the vehicle stands upright on this wheel on the ground under the water (false if it is overturned, lying on its side or lifted),
  "tyre_edge_visible": the share of the tyre's outer edge that is visible above the water and not hidden, 0 to 1,
  "waterline_visible": true if you can see where the water surface cuts across this tyre,
  "waterline": [[x, y], [x, y]] where the water surface meets the tyre's left and right edges, or [],
  "tyre_box": a tight box around this tyre's whole visible part above the water (tyre and rim only, no mudguard or bodywork), whatever the outline does, or null if the tyre is not visible,
  "corrected_box": if the outline is not good and the {kind_text} is visible, the same box as tyre_box, else null,
  "tyre_points": 3 or 4 points on the black tyre rubber above the water, spread around its visible arc (for example left, top-left, top-right, right),
  "exclude_points": up to 3 points that are not tyre: the hub, and anything next to the tyre or wrongly inside the outline (mudguard, fork, exhaust, bodywork, wall, splash, reflection),
  "reason": one short sentence
}}"""


def verify_prompt(kind_text: str, panels: int = 1) -> str:
    """The check of proposed outlines for a wheel, shown as one panel (A) or two (A and B)."""
    n = 2 if panels == 2 else 1
    return _VERIFY_BODY.format(
        intro=_VERIFY_INTRO[n].format(kind_text=kind_text),
        grid=_VERIFY_GRID[n],
        choice=_VERIFY_CHOICE[n],
        kind_text=kind_text,
    )


_WATER_VERIFY_PROMPT = """This is a flood photo. A segmentation model was asked for the floodwater surface. Its proposed region is tinted magenta and outlined, and the box it was given is drawn in yellow.

Coordinates: integers on a 0-1000 grid over this image, x from the left, y from the top. A box is [x_min, y_min, x_max, y_max]. A point is [x, y].

Answer with one JSON object, nothing else:
{
  "is_water": true only if the magenta region is mostly the standing or flowing water surface (not road, walls, sky, vehicles or people),
  "coverage": "good" if it covers the water around the people and vehicles standing in it (it need not reach every distant patch), "partial" if it misses water right next to them, "wrong" if it is not water,
  "corrected_box": if not good, a box lying entirely on open water, else null,
  "corrected_point": if not good, one point on open water, else null,
  "reason": one short sentence
}"""


def water_verify_prompt() -> str:
    return _WATER_VERIFY_PROMPT


TRANSCRIBE_PROMPT = """The audio clip in this message is a voice note from someone in India reporting street flooding. Listen to it. It may be in Hindi, Tamil, Telugu, Kannada, Malayalam, Bengali, Marathi, Gujarati, Punjabi, Odia, Urdu or English, and people often mix two of them (for example Tamil with English words).

Return one JSON object, nothing else:
{
  "text": the transcript, in the words spoken, each language in its own script (English in Latin script),
  "language": ISO 639-1 codes of the languages spoken, main one first, joined with "+" (codes: hi, ta, te, kn, ml, bn, mr, gu, pa, or, ur, en), for example "ta+en",
  "place_spoken": the place or street named, as spoken, or "",
  "place_text": the same place in its usual English spelling, as on a map (for example "Velachery Main Road"), or "",
  "depth_words": the words used for how deep the water is, as spoken (for example "घुटने तक", "mutti alavu", "two feet"), or "",
  "depth_words_en": those words in plain English (for example "knee deep", "two feet"), or "",
  "depth_cm": your estimate of the water depth in centimetres from those words, or null,
  "depth_low_cm": a low bound, or null,
  "depth_high_cm": a high bound, or null
}"""


DEBRIS_PROMPT = """This is one frame of a short video of a flooded street. A segmentation model will follow objects through the video to measure how fast the water flows, so it needs things that float and drift with the current.

Coordinates: integers on a 0-1000 grid over this image, x from the left edge, y from the top edge. A box is [x_min, y_min, x_max, y_max]. A point is [x, y].

Return one JSON object, nothing else:
{
  "debris": up to 8 separate objects floating on the water surface that will drift with the flow: leaves, twigs, plastic, bottles, foam patches, rubbish. Not people, animals or vehicles, not anything fixed (poles, kerbs, signs, walls, parked or stuck objects), not reflections, ripples, waves or rain splashes. Prefer objects with a clear outline against the water, spread over the water rather than in one clump. Each:
    {"box": [..] a tight box around this one object, "point": [..] a point on the object itself, "what": one or two words},
  "anchor": one small, sharp, high-contrast detail of something fixed that stays in view and is not on the water (a sign, a pole base, a window corner, a painted road marking on dry ground, a kerb corner): {"box": [..], "point": [..], "what": one or two words}, or null,
  "flow": one short sentence: which way the water moves in the image, or "not visible"
}
Boxes must be tight and must not include neighbouring objects. Omit what you cannot see; never invent objects. If nothing floats, return "debris": []."""


# --------------------------------------------------------------------------- prompt versions


def prompt_sha256(*parts: str) -> str:
    """SHA-256 (hex) of a prompt's text, or of every fixed part of a template.

    Parts are joined with a NUL byte, which no prompt contains, so a change to
    any part, or a part moving from one to another, changes the hash."""
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


def prompt_version(name: str, *parts: str) -> str:
    """A stable version id: the prompt's short name, ``@``, 12 hex digits of its hash."""
    return f"{name}@{prompt_sha256(*parts)[:12]}"


def _verify_wheel_parts() -> tuple[str, ...]:
    return (
        _VERIFY_BODY,
        *(_VERIFY_INTRO[n] for n in (1, 2)),
        *(_VERIFY_CHOICE[n] for n in (1, 2)),
        *(_VERIFY_GRID[n] for n in (1, 2)),
    )


# The fixed text of every Muse Spark Open prompt, by name.
PROMPT_PARTS: dict[str, tuple[str, ...]] = {
    "scene": (SCENE_PROMPT,),
    "verify-wheel": _verify_wheel_parts(),
    "verify-water": (_WATER_VERIFY_PROMPT,),
    "locate-debris": (DEBRIS_PROMPT,),
    "transcribe": (TRANSCRIBE_PROMPT,),
}
PROMPT_VERSIONS: dict[str, str] = {n: prompt_version(n, *p) for n, p in PROMPT_PARTS.items()}
SCENE_PROMPT_VERSION = PROMPT_VERSIONS["scene"]
VERIFY_WHEEL_VERSION = PROMPT_VERSIONS["verify-wheel"]
VERIFY_WATER_VERSION = PROMPT_VERSIONS["verify-water"]
TRANSCRIBE_VERSION = PROMPT_VERSIONS["transcribe"]
DEBRIS_VERSION = PROMPT_VERSIONS["locate-debris"]


def prompt_digest(version: str) -> str | None:
    """The full SHA-256 behind a version id this module issued, or None."""
    name = version.split("@", 1)[0]
    parts = PROMPT_PARTS.get(name)
    if parts is None or PROMPT_VERSIONS.get(name) != version:
        return None
    return prompt_sha256(*parts)


# --------------------------------------------------------------------------- parsing helpers


def grid_point(p: Any) -> tuple[float, float] | None:
    """A point on the 0 to 1000 grid from ``[x, y]`` or ``{"x":, "y":}``."""
    try:
        if isinstance(p, dict):
            x, y = float(p["x"]), float(p["y"])
        elif isinstance(p, list | tuple) and len(p) >= 2:
            x, y = float(p[0]), float(p[1])
        else:
            return None
    except (KeyError, TypeError, ValueError):
        return None
    if not (math.isfinite(x) and math.isfinite(y)):
        return None
    return min(max(x, 0.0), 1000.0), min(max(y, 0.0), 1000.0)


def grid_box(b: Any) -> tuple[float, float, float, float] | None:
    """A box on the 0 to 1000 grid from a list of four or a dict; corners sorted."""
    try:
        if isinstance(b, dict):
            v = [float(b[k]) for k in ("x_min", "y_min", "x_max", "y_max")]
        elif isinstance(b, list | tuple) and len(b) >= 4:
            v = [float(x) for x in b[:4]]
        else:
            return None
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(x) for x in v):
        return None
    x0, x1 = sorted((min(max(v[0], 0.0), 1000.0), min(max(v[2], 0.0), 1000.0)))
    y0, y1 = sorted((min(max(v[1], 0.0), 1000.0), min(max(v[3], 0.0), 1000.0)))
    if x1 - x0 < 1.0 or y1 - y0 < 1.0:
        return None
    return x0, y0, x1, y1


def to_px(p: tuple[float, float], w: int, h: int) -> tuple[int, int]:
    return (
        min(max(round(p[0] * w / 1000.0), 0), w - 1),
        min(max(round(p[1] * h / 1000.0), 0), h - 1),
    )


def box_to_px(b: tuple[float, float, float, float], w: int, h: int) -> tuple[int, int, int, int]:
    """Grid box to a pixel box ``(left, top, right, bottom)``, right and bottom half-open."""
    x0, y0 = to_px((b[0], b[1]), w, h)
    x1, y1 = to_px((b[2], b[3]), w, h)
    return x0, y0, max(x1 + 1, x0 + 2), max(y1 + 1, y0 + 2)


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def parse_open_scene(data: dict[str, Any], width: int, height: int) -> SceneReading:
    """Muse Spark Open's scene JSON (0 to 1000 grid) in pixel coordinates.

    Tolerant of missing or malformed fields: a reference without a usable box
    or point is dropped, numbers that do not parse become None.
    """
    refs: list[SceneObject] = []
    for r in (data.get("references") or [])[:5]:
        if not isinstance(r, dict):
            continue
        try:
            kind = RefKind(str(r.get("kind", "")).strip())
        except ValueError:
            continue
        gb = grid_box(r.get("box"))
        gp = grid_point(r.get("point"))
        if gb is None and gp is None:
            continue
        box = box_to_px(gb, width, height) if gb is not None else None
        if gp is None and gb is not None:
            gp = ((gb[0] + gb[2]) / 2.0, (gb[1] + gb[3]) / 2.0)
        assert gp is not None
        rp = grid_point(r.get("rim_point"))
        wl = [grid_point(p) for p in (r.get("waterline") or [])[:2]]
        wl_px = tuple(to_px(p, width, height) for p in wl if p is not None)
        vehicle = r.get("vehicle")
        pos = r.get("position")
        vf = _num(r.get("visible_fraction"))
        refs.append(
            SceneObject(
                kind=kind,
                point=to_px(gp, width, height),
                box=box,
                rim_point=to_px(rp, width, height) if rp is not None else None,
                vehicle=vehicle if isinstance(vehicle, str) and vehicle in vehicles() else None,
                position=pos if pos in ("front", "rear") else None,
                confidence=min(1.0, max(0.0, _num(r.get("confidence")) or 0.5)),
                waterline_points=wl_px,
                tyre_points=tuple(
                    to_px(p, width, height)
                    for p in (grid_point(q) for q in (r.get("tyre_points") or [])[:4])
                    if p is not None
                ),
                head_top=(
                    to_px(ht, width, height)
                    if (ht := grid_point(r.get("head_top"))) is not None
                    else None
                ),
                posture=str(r.get("posture")).strip().lower() if r.get("posture") else None,
                adult=bool(r.get("adult", True)),
                landmark=str(r.get("landmark")).strip().lower() if r.get("landmark") else None,
                exclude_points=tuple(
                    to_px(p, width, height)
                    for p in (grid_point(q) for q in (r.get("exclude_points") or [])[:3])
                    if p is not None
                ),
                visible_fraction=None if vf is None else min(1.0, max(0.0, vf)),
                depth_cm=_num(r.get("depth_cm")),
                depth_reason=str(r.get("depth_reason") or "").strip()[:200] or None,
            )
        )
    water = data.get("water") or {}
    if not isinstance(water, dict):
        water = {}
    wb = grid_box(water.get("box"))
    wp = grid_point(water.get("point"))
    water_points: list[tuple[int, int]] = []
    if wp is not None:
        water_points.append(to_px(wp, width, height))
    for p in (data.get("water_points") or [])[:3]:  # older shape, still accepted
        gp2 = grid_point(p)
        if gp2 is not None:
            water_points.append(to_px(gp2, width, height))
    if not water_points and wb is not None:
        water_points.append(to_px(((wb[0] + wb[2]) / 2, (wb[1] + wb[3]) / 2), width, height))
    overall = data.get("overall") or {}
    if not isinstance(overall, dict):
        overall = {}
    q = data.get("image_quality") or {}
    if not isinstance(q, dict):
        q = {}
    issues = q.get("issues") or []
    first = refs[0] if refs else None
    found_vehicles = []
    for v in (data.get("vehicles") or [])[:4]:
        if not isinstance(v, dict):
            continue
        vb = grid_box(v.get("box"))
        if vb is None:
            continue
        model = v.get("vehicle")
        found_vehicles.append(
            VehicleObject(
                kind=str(v.get("kind") or "other").strip().lower(),
                box=box_to_px(vb, width, height),
                vehicle=model if isinstance(model, str) and model in vehicles() else None,
                wheels_visible=bool(v.get("wheels_visible", True)),
            )
        )
    faces: list[tuple[int, int, int, int]] = []
    raw_faces = data.get("faces")
    for fb in raw_faces if isinstance(raw_faces, list) else []:
        if isinstance(fb, dict) and "box" in fb:  # the model sometimes wraps each box
            fb = fb["box"]
        g = grid_box(fb)
        if g is not None and len(faces) < 32:
            faces.append(box_to_px(g, width, height))
    return SceneReading(
        water_visible=bool(data.get("water_visible", False)),
        water_points=tuple(water_points[:3]),
        references=tuple(refs),
        waterline_points=first.waterline_points if first is not None else (),
        floating_debris=(str(data.get("floating_debris") or "").strip() or None),
        quality=ImageQuality(
            score=min(1.0, max(0.0, _num(q.get("score")) or 0.5)),
            blurred=bool(q.get("blurred", False)),
            too_dark=bool(q.get("too_dark", False)),
            issues=tuple(str(i) for i in issues[:5]) if isinstance(issues, list) else (),
        ),
        water_box=box_to_px(wb, width, height) if wb is not None else None,
        overall_depth_cm=_num(overall.get("depth_cm")),
        overall_low_cm=_num(overall.get("low_cm")),
        overall_high_cm=_num(overall.get("high_cm")),
        overall_reason=str(overall.get("reason") or "").strip()[:200] or None,
        vehicles=tuple(found_vehicles),
        faces=tuple(faces),
    )


# --------------------------------------------------------------------------- adapter


# JPEG quality of images sent to the model. On the development network the
# request time grew with the upload size (a 1280 px photo took 14 s at quality
# 95, 552 KB, and 6.8 s at quality 60, 163 KB, for the same one-line answer),
# so images are sent compressed; boxes are drawn by the model on a 0 to 1000
# grid, which compression does not move.
MODEL_JPEG_QUALITY = 75


def jpeg_for_model(image: ImageInput, max_side: int, quality: int = MODEL_JPEG_QUALITY) -> bytes:
    """The image as sent to the model: the sanitised photo, shrunk and compressed."""
    im = Image.fromarray(image.rgb)
    if max(image.width, image.height) > max_side:
        s = max_side / max(image.width, image.height)
        im = im.resize((max(1, round(image.width * s)), max(1, round(image.height * s))))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def data_url(data: bytes, media_type: str) -> str:
    return f"data:{media_type};base64,{base64.b64encode(data).decode('ascii')}"


@dataclass(frozen=True, slots=True)
class TranscriptResult:
    text: str
    language: str  # "+"-joined ISO 639-1 codes, main language first
    place_text: str  # normalised, Latin script
    depth_words: str
    depth_cm: float | None
    depth_sigma_cm: float | None
    usage: Usage
    latency_ms: int
    languages: tuple[str, ...] = ()
    place_spoken: str = ""
    depth_words_en: str = ""
    depth_low_cm: float | None = None
    depth_high_cm: float | None = None
    depth_basis: str = ""
    prompt_version: str = ""
    validation: tuple[str, ...] = ()  # what the checks of the answer dropped (validate.py)


def env_from_dotenv(names: Sequence[str], start: Path | None = None) -> dict[str, str]:
    """Named variables from the nearest ``.env`` at or above ``start``; never logged."""
    here = (start or Path.cwd()).resolve()
    out: dict[str, str] = {}
    for folder in (here, *here.parents):
        env = folder / ".env"
        if not env.is_file():
            continue
        for raw in env.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("export "):
                line = line[7:].strip()
            key, sep, value = line.partition("=")
            key = key.strip()
            if sep and key in names:
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                    value = value[1:-1]
                if value:
                    out[key] = value
        return out
    return out


@dataclass
class MuseSparkOpenAdapter:
    """:class:`~neermattam_perception.adapters.SceneModel` on Baseten, plus verification and audio.

    ``transport`` lets tests plug in an ``httpx.MockTransport``. ``ipv4``
    binds outgoing connections to IPv4 (some networks advertise IPv6 that does
    not route, and the request then hangs until the connect timeout).
    """

    api_key: str = field(default="", repr=False)
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    transport: httpx.AsyncBaseTransport | None = None
    timeout_s: float = 120.0  # per attempt (read); connect is 15 s
    total_timeout_s: float = 240.0  # all attempts of one call, with backoff
    max_retries: int = 3
    backoff_s: float = 2.0
    # Hard limits per attempt for the calls on a user's critical path, with one
    # retry. Baseten's answers are usually 6 to 12 s for a check and 8 to 27 s
    # for a scene reading, with a rare tail of 80 to 160 s; a stuck call is cut
    # and asked again rather than waited for (eval/RESULTS_REAL.md, latency).
    verify_attempt_timeout_s: float | None = 20.0
    scene_attempt_timeout_s: float | None = 45.0
    critical_retries: int = 1
    ipv4: bool = True
    max_image_side: int = 1024
    scene_max_tokens: int = 8000
    verify_max_tokens: int = 4000
    # Reasoning effort per call kind (None: the model's default). The checks of a
    # proposed mask are narrow questions on a crop; "low" gave the same verdicts
    # as the default on the probe crops in about half the time.
    # Baseten's Muse Spark Open accepts low, medium, high and xhigh (the
    # default); it always reasons (https://docs.baseten.co/inference/model-apis/reasoning).
    scene_effort: str | None = "low"
    verify_effort: str | None = "low"
    confirm_effort: str | None = None
    # Sampling: greedy, with a fixed seed, so that the same photo gets the same
    # reading as far as the server allows (the same photo read very differently
    # over live runs with the server's default sampling). Baseten accepts both; that
    # the model then answers identically is not guaranteed, and is measured in
    # eval/RESULTS_REAL.md, not assumed. None leaves the server's default.
    temperature: float | None = 0.0
    seed: int | None = 7
    audio_attempts: int = 3
    name: str = "baseten-muse-spark-open"
    sleep: Callable[[float], Awaitable[None]] = field(default=asyncio.sleep, repr=False)
    scene_cache_size: int = 64
    # When set, scene readings are also kept on disk, so a photo read once is not
    # read again (evaluation runs, service restarts).
    scene_cache_dir: Path | None = None
    _scene_cache: OrderedDict[str, JsonReply] = field(default_factory=OrderedDict, repr=False)

    @classmethod
    def from_env(cls, **kw: Any) -> MuseSparkOpenAdapter:
        names = ("BASETEN_API_KEY", "BASETEN_BASE_URL", "BASETEN_MODEL")
        env = {n: os.environ[n] for n in names if os.environ.get(n)}
        if len(env) < len(names):
            env = {**env_from_dotenv(names), **env}
        return cls(
            api_key=env.get("BASETEN_API_KEY", ""),
            base_url=env.get("BASETEN_BASE_URL", DEFAULT_BASE_URL),
            model=env.get("BASETEN_MODEL", DEFAULT_MODEL),
            **kw,
        )

    @property
    def scene_model_name(self) -> str:
        return MODEL_LABEL

    @property
    def scene_model_version(self) -> str:
        """The exact model and settings: the Baseten model id and reasoning efforts."""
        return (
            f"{self.model} on Baseten, reasoning effort {self.scene_effort or 'default'} "
            f"(scene), {self.verify_effort or 'default'} (checks), temperature "
            f"{'default' if self.temperature is None else self.temperature}, seed "
            f"{'none' if self.seed is None else self.seed}"
        )

    @property
    def scene_prompt_version(self) -> str:
        return SCENE_PROMPT_VERSION

    def _client(self) -> httpx.AsyncClient:
        if not self.api_key:
            raise ModelError("BASETEN_API_KEY is not set")
        transport = self.transport
        if transport is None and self.ipv4:
            transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
        return httpx.AsyncClient(
            base_url=self.base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=httpx.Timeout(self.timeout_s, connect=15.0),
            transport=transport,
        )

    async def ready(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, "BASETEN_API_KEY is not set"

        async def get() -> httpx.Response:
            async with self._client() as c:
                return await c.get("/models")

        try:
            r = await asyncio.wait_for(get(), 10.0)  # a readiness probe must answer fast
        except TimeoutError:
            return False, "Baseten did not answer within 10 s"
        except httpx.HTTPError as exc:
            return False, f"Baseten unreachable: {type(exc).__name__}"
        if r.status_code in (200, 404, 405):  # reachable and authorised (404: no list route)
            return True, "ok"
        return False, f"Baseten returned HTTP {r.status_code}"

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int,
        what: str,
        effort: str | None = None,
        attempt_timeout_s: float | None = None,
        retries: int | None = None,
    ) -> ChatReply:
        """One Chat Completions call with retries, under a total time limit.

        Each attempt has the client timeout, or ``attempt_timeout_s`` when given
        (a hard limit, after which the attempt is abandoned and retried); all
        attempts together, with their backoff, must finish within
        ``total_timeout_s`` or the call fails with a ModelError (never an
        unbounded wait).
        """
        total = self.total_timeout_s
        if attempt_timeout_s is not None:
            n = (self.max_retries if retries is None else retries) + 1
            total = min(total, n * attempt_timeout_s + 2 * self.backoff_s + 1.0)
        try:
            return await asyncio.wait_for(
                self._chat(
                    messages,
                    max_tokens=max_tokens,
                    what=what,
                    effort=effort,
                    attempt_timeout_s=attempt_timeout_s,
                    retries=retries,
                ),
                total,
            )
        except TimeoutError as exc:
            raise ModelError(f"{what} took longer than {total:.0f} s") from exc

    async def _chat(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int,
        what: str,
        effort: str | None = None,
        attempt_timeout_s: float | None = None,
        retries: int | None = None,
    ) -> ChatReply:
        body: dict[str, Any] = {"model": self.model, "max_tokens": max_tokens, "messages": messages}
        if effort is not None:
            body["reasoning_effort"] = effort
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.seed is not None:
            body["seed"] = self.seed
        t0 = time.perf_counter()
        last = ""
        max_retries = self.max_retries if retries is None else retries
        async with self._client() as c:
            for attempt in range(max_retries + 1):
                retry_after: float | None = None
                try:
                    post = c.post("/chat/completions", json=body)
                    if attempt_timeout_s is not None:
                        r = await asyncio.wait_for(post, attempt_timeout_s)
                    else:
                        r = await post
                except TimeoutError:
                    last = f"{what} gave no answer within {attempt_timeout_s:.0f} s"
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last = f"{what} request failed: {type(exc).__name__}"
                else:
                    if r.status_code == 200:
                        return self._reply(r, t0, what)
                    msg = ""
                    try:
                        err = r.json().get("error")
                        msg = err.get("message", "") if isinstance(err, dict) else str(err or "")
                    except (json.JSONDecodeError, AttributeError):
                        pass
                    last = f"{what} failed with HTTP {r.status_code}: {msg[:200]}".rstrip(": ")
                    if r.status_code not in _RETRY_STATUS:
                        raise ModelError(last)
                    ra = r.headers.get("retry-after", "")
                    retry_after = float(ra) if ra.replace(".", "", 1).isdigit() else None
                if attempt < max_retries:
                    delay = retry_after if retry_after is not None else self.backoff_s * 2**attempt
                    await self.sleep(min(delay, 30.0))
        raise ModelError(last or f"{what} failed")

    @staticmethod
    def _reply(r: httpx.Response, t0: float, what: str) -> ChatReply:
        try:
            payload = r.json()
            choice = payload["choices"][0]
            msg = choice["message"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise ModelError(f"{what} returned an unexpected shape") from exc
        reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
        return ChatReply(
            content=msg.get("content") or "",
            reasoning=reasoning if isinstance(reasoning, str) else "",
            usage=Usage.from_payload(payload.get("usage")),
            latency_ms=round(1000 * (time.perf_counter() - t0)),
            finish_reason=choice.get("finish_reason"),
        )

    async def chat_json(
        self,
        parts: list[dict[str, Any]],
        *,
        max_tokens: int,
        what: str,
        effort: str | None = None,
        attempt_timeout_s: float | None = None,
        retries: int | None = None,
    ) -> JsonReply:
        """A call whose answer must be a JSON object, with one repair retry."""
        messages: list[dict[str, Any]] = [{"role": "user", "content": parts}]
        limits: dict[str, Any] = {"attempt_timeout_s": attempt_timeout_s, "retries": retries}
        first = await self.chat(messages, max_tokens=max_tokens, what=what, effort=effort, **limits)
        try:
            return JsonReply(
                extract_json(first.content), first.usage, first.latency_ms, first.reasoning
            )
        except JsonExtractError:
            pass
        repair = [
            *messages,
            {"role": "assistant", "content": first.content or "(no answer)"},
            {
                "role": "user",
                "content": "Reply with the JSON object only: no prose, no code fences.",
            },
        ]
        second = await self.chat(
            repair, max_tokens=max_tokens, what=f"{what} (JSON repair)", effort=effort, **limits
        )
        try:
            data = extract_json(second.content)
        except JsonExtractError as exc:
            raise ModelError(f"{what} did not return JSON after one repair") from exc
        return JsonReply(
            data,
            first.usage + second.usage,
            first.latency_ms + second.latency_ms,
            second.reasoning or first.reasoning,
            repaired=True,
        )

    def _image_part(self, jpeg: bytes) -> dict[str, Any]:
        return {"type": "image_url", "image_url": {"url": data_url(jpeg, "image/jpeg")}}

    def _scene_key(self, image: ImageInput) -> str:
        h = hashlib.sha256()
        for part in (image.jpeg, SCENE_PROMPT.encode(), self.model.encode()):
            h.update(part)
        h.update(f"{self.scene_effort}:{self.max_image_side}".encode())
        if self.temperature is not None or self.seed is not None:
            h.update(f":t={self.temperature}:s={self.seed}".encode())
        return h.hexdigest()[:32]

    async def read_scene_json(self, image: ImageInput, *, fresh: bool = False) -> JsonReply:
        """The scene model's answer for this photo, from the cache when it was read before.

        The key covers the photo's bytes, the prompt, the model and its settings,
        so a changed prompt is never answered from an old reading. A cached
        answer costs nothing and is marked ``cached``. ``fresh`` skips the cache
        (the answer there failed its checks). Only an answer whose decision
        fields pass the checks (``validate.check_scene``) is cached.
        """
        key = self._scene_key(image)
        hit = None if fresh else self._scene_cache.get(key)
        if hit is None and not fresh and self.scene_cache_dir is not None:
            f = self.scene_cache_dir / f"{key}.json"
            if f.is_file():
                hit = JsonReply(json.loads(f.read_text(encoding="utf-8")), Usage(), 0, "")
        if hit is not None:
            self._scene_cache[key] = hit
            self._scene_cache.move_to_end(key)
            return JsonReply(hit.data, Usage(), 0, hit.reasoning, hit.repaired, cached=True)
        jpeg = await asyncio.to_thread(jpeg_for_model, image, self.max_image_side)
        reply = await self.chat_json(
            [self._image_part(jpeg), {"type": "text", "text": SCENE_PROMPT}],
            max_tokens=self.scene_max_tokens,
            what="scene model",
            effort=self.scene_effort,
            attempt_timeout_s=self.scene_attempt_timeout_s,
            retries=self.critical_retries,
        )
        if check_scene(reply.data, image.width, image.height).fatal is not None:
            self._scene_cache.pop(key, None)
            return reply
        self._scene_cache[key] = reply
        while len(self._scene_cache) > self.scene_cache_size:
            self._scene_cache.popitem(last=False)
        if self.scene_cache_dir is not None:
            self.scene_cache_dir.mkdir(parents=True, exist_ok=True)
            (self.scene_cache_dir / f"{key}.json").write_text(json.dumps(reply.data), "utf-8")
        return reply

    async def read_scene(self, image: ImageInput) -> SceneReading:
        """The scene reading, after the checks of ``validate.check_scene``.

        An answer whose ``water_visible`` fails is asked once more, bypassing the
        cache; failing again raises :class:`AnswerRejected`. Any other field or
        element that fails is left out and listed in ``dropped``.
        """
        reply = await self.read_scene_json(image)
        checked = check_scene(reply.data, image.width, image.height)
        cost = reply.usage.cost_usd
        retried_because = None
        if checked.fatal is not None:
            retried_because = checked.fatal
            reply = await self.read_scene_json(image, fresh=True)
            cost += reply.usage.cost_usd
            checked = check_scene(reply.data, image.width, image.height)
            if checked.fatal is not None:
                raise AnswerRejected(
                    "the scene model's answer", [retried_because, checked.fatal], cost
                )
        try:
            reading = parse_open_scene(checked.data, image.width, image.height)
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
            raise ModelError("scene model returned an unexpected shape") from exc
        return replace(
            reading,
            cost_usd=cost,
            cached=reply.cached,
            prompt_version=SCENE_PROMPT_VERSION,
            retried_because=retried_because,
            dropped=tuple(checked.problems),
        )

    async def ask_image(
        self, jpeg: bytes | Sequence[bytes], prompt: str, *, what: str
    ) -> JsonReply:
        """One or more images plus one question, answered as JSON (the verifier's call).

        Each attempt is cut after ``verify_attempt_timeout_s`` and retried once."""
        images = [jpeg] if isinstance(jpeg, bytes) else list(jpeg)
        return await self.chat_json(
            [*(self._image_part(j) for j in images), {"type": "text", "text": prompt}],
            max_tokens=self.verify_max_tokens,
            what=what,
            effort=self.verify_effort,
            attempt_timeout_s=self.verify_attempt_timeout_s,
            retries=self.critical_retries,
        )

    async def locate_debris_json(self, image: ImageInput) -> JsonReply:
        """Boxes on the floating objects and one fixed anchor in a video's key frame."""
        jpeg = await asyncio.to_thread(jpeg_for_model, image, self.max_image_side)
        return await self.chat_json(
            [self._image_part(jpeg), {"type": "text", "text": DEBRIS_PROMPT}],
            max_tokens=self.verify_max_tokens,
            what="debris locator",
            effort=self.scene_effort,
            attempt_timeout_s=self.scene_attempt_timeout_s,
            retries=self.critical_retries,
        )

    async def confirm_image(self, jpeg: bytes, prompt: str, *, what: str) -> JsonReply:
        """The same question with full reasoning, used to confirm a quick "accept"."""
        return await self.chat_json(
            [self._image_part(jpeg), {"type": "text", "text": prompt}],
            max_tokens=self.scene_max_tokens,
            what=what,
            effort=self.confirm_effort,
        )

    async def transcribe(self, audio: bytes, media_type: str) -> TranscriptResult:
        """Transcript, languages, place and depth words from a voice note.

        Any of the major Indian languages, mixed or not (``voice.LANGUAGES``).
        The depth comes from ``voice.depth_from_words``: a spoken number with
        a unit, then the phrase table, then the model's own reading.
        """
        part = {"type": "audio_url", "audio_url": {"url": data_url(audio, media_type)}}
        # On the probe the model sometimes answered as if no audio were attached
        # (an empty transcript, or prose saying so), more often when the audio
        # came before the text. So the text goes first, and an empty transcript
        # is asked again, up to ``audio_attempts`` times in all.
        usage = Usage()
        latency = 0
        d: dict[str, Any] = {}
        problems: list[str] = []
        failed: list[str] = []
        for _ in range(self.audio_attempts):
            try:
                reply = await self.chat_json(
                    [{"type": "text", "text": TRANSCRIBE_PROMPT}, part],
                    max_tokens=self.verify_max_tokens,
                    what="transcription",
                )
            except ModelError:
                continue
            usage, latency = usage + reply.usage, latency + reply.latency_ms
            checked = check_transcript(reply.data)
            if checked.fatal is not None:
                failed.append(checked.fatal)
                d = {}
                continue
            d, problems = checked.data, checked.problems
            if str(d.get("text") or "").strip():
                break
        if not str(d.get("text") or "").strip():
            why = f"; its answers failed the checks: {failed[-1]}" if failed else ""
            raise ModelError(
                f"the audio model returned no transcript in {self.audio_attempts} attempts "
                "(it answered as if it had not received the audio)" + why
            )
        problems = [f"an earlier answer failed its checks ({f}); asked again" for f in failed] + [
            *problems
        ]
        words = str(d.get("depth_words") or "").strip()
        words_en = str(d.get("depth_words_en") or "").strip()
        reading = depth_from_words(
            words,
            words_en,
            _num(d.get("depth_cm")),
            _num(d.get("depth_low_cm")),
            _num(d.get("depth_high_cm")),
        )
        langs = normalise_languages(d.get("language"))
        if not langs:
            langs = ["en"] if str(d.get("text") or "").isascii() else []
        return TranscriptResult(
            text=str(d.get("text") or "").strip(),
            language="+".join(langs) or "unknown",
            place_text=str(d.get("place_text") or d.get("place_spoken") or "").strip(),
            depth_words=words,
            depth_cm=None if reading is None else reading.depth_cm,
            depth_sigma_cm=None if reading is None else reading.sigma_cm,
            languages=tuple(langs),
            place_spoken=str(d.get("place_spoken") or "").strip(),
            depth_words_en=words_en,
            depth_low_cm=None if reading is None else round(reading.low_cm, 1),
            depth_high_cm=None if reading is None else round(reading.high_cm, 1),
            depth_basis="" if reading is None else reading.basis,
            usage=usage,
            latency_ms=latency,
            prompt_version=TRANSCRIBE_VERSION,
            validation=tuple(problems),
        )
