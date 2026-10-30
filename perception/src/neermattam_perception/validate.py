"""Deterministic checks on every Muse Spark Open answer, before anything uses it.

The parsers in ``muse_open.py`` and ``agent.py`` are tolerant on purpose: a
coordinate off the grid is clamped onto it, a kind they do not know is skipped.
Tolerance alone hides a model's mistakes, so every answer passes these checks
first (docs/CONTRACTS.md, section 1.1):

* **schema**: each field has the type the prompt asks for;
* **grid**: every point and box lies on the 0 to 1000 grid, and a box is
  ``[x_min, y_min, x_max, y_max]`` with its corners in that order;
* **inside image**: every box, once in pixels, lies inside the image or crop;
* **allowed values**: kinds, postures, landmarks, choices and language codes
  come from fixed lists; shares lie in 0 to 1; depths in 0 to 1000 cm.

A check returns the answer with every failing field or list element left out,
one plain sentence per problem, and ``fatal`` when a field the decision rests on
failed: the caller then asks once more and declines if it fails again. Nothing
here is silent: every problem is returned for the trace.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from .person import LANDMARK_SHARE
from .references import RefKind, vehicles
from .voice import LANGUAGES

RULES: tuple[str, ...] = ("schema", "grid", "inside image", "allowed values")

GRID_MAX = 1000.0
MAX_DEPTH_CM = 1000.0

REFERENCE_KINDS = frozenset(k.value for k in RefKind)
VEHICLE_KINDS = frozenset({"car", "two_wheeler", "auto_rickshaw", "bus", "truck", "other"})
POSTURES = frozenset(
    {"standing", "walking", "wading", "bending", "sitting", "swimming", "carrying_on_head"}
)
LANDMARKS = frozenset(LANDMARK_SHARE)
POSITIONS = frozenset({"front", "rear", "unknown"})
OUTLINE_QUALITY = frozenset({"good", "partial", "wrong"})
VIEWS = frozenset({"side", "oblique", "head_on"})
COVERAGE = frozenset({"good", "partial", "wrong"})


@dataclass
class Checked:
    """An answer after the checks: what survived, what was dropped, and whether a
    field the decision rests on failed."""

    data: dict[str, Any]
    problems: list[str] = field(default_factory=list)
    fatal: str | None = None

    @property
    def ok(self) -> bool:
        return self.fatal is None and not self.problems


# --------------------------------------------------------------------------- primitives


def _number(v: Any) -> float | None:
    """A finite JSON number (booleans are not numbers here)."""
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _show(v: Any) -> str:
    text = repr(v)
    return text if len(text) <= 40 else text[:37] + "..."


def grid_point_problem(v: Any) -> str | None:
    """Why ``v`` is not a point ``[x, y]`` on the 0 to 1000 grid, or None."""
    if isinstance(v, dict):
        v = [v.get("x"), v.get("y")]
    if not isinstance(v, list | tuple) or len(v) != 2:
        return f"{_show(v)} is not a point [x, y]"
    nums = [_number(c) for c in v]
    if any(n is None for n in nums):
        return f"{_show(v)} has a coordinate that is not a number"
    if any(not 0.0 <= n <= GRID_MAX for n in nums if n is not None):
        return f"{_show(v)} is outside the 0 to 1000 grid"
    return None


def grid_box_problem(v: Any) -> str | None:
    """Why ``v`` is not a box ``[x_min, y_min, x_max, y_max]`` on the grid, or None."""
    if isinstance(v, dict):
        v = [v.get(k) for k in ("x_min", "y_min", "x_max", "y_max")]
    if not isinstance(v, list | tuple) or len(v) != 4:
        return f"{_show(v)} is not a box [x_min, y_min, x_max, y_max]"
    nums = [_number(c) for c in v]
    if any(n is None for n in nums):
        return f"{_show(v)} has a coordinate that is not a number"
    x0, y0, x1, y1 = (float(n) for n in nums if n is not None)
    if any(not 0.0 <= n <= GRID_MAX for n in (x0, y0, x1, y1)):
        return f"{_show(v)} is outside the 0 to 1000 grid"
    if not (x0 < x1 and y0 < y1):
        return f"{_show(v)} does not have x_min < x_max and y_min < y_max"
    return None


def pixel_box_problem(box: tuple[int, int, int, int], width: int, height: int) -> str | None:
    """Why a pixel box ``(left, top, right, bottom)`` is not inside a ``width`` by
    ``height`` image (right and bottom half-open), or None."""
    x0, y0, x1, y1 = box
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        return f"box {list(box)} px is not inside the {width}x{height} image"
    return None


class _Checker:
    """Collects problems while copying the fields that pass."""

    def __init__(self, data: Any, what: str) -> None:
        self.what = what
        self.problems: list[str] = []
        self.fatal: str | None = None
        self.src: dict[str, Any] = data if isinstance(data, dict) else {}
        if not isinstance(data, dict):
            self.fatal = f"{what} is not a JSON object"
        self.out: dict[str, Any] = {}

    def drop(self, path: str, why: str) -> None:
        self.problems.append(f"{path}: {why}; dropped")

    def need(self, path: str, why: str) -> None:
        if self.fatal is None:
            self.fatal = f"{path}: {why}"

    def result(self) -> Checked:
        return Checked(self.out, self.problems, self.fatal)


def _bool(c: _Checker, src: dict[str, Any], out: dict[str, Any], key: str, path: str) -> None:
    v = src.get(key)
    if v is None:
        return
    if isinstance(v, bool):
        out[key] = v
    else:
        c.drop(path, f"{_show(v)} is not true or false")


def _share(c: _Checker, src: dict[str, Any], out: dict[str, Any], key: str, path: str) -> None:
    v = src.get(key)
    if v is None:
        return
    n = _number(v)
    if n is None or not 0.0 <= n <= 1.0:
        c.drop(path, f"{_show(v)} is not a share between 0 and 1")
    else:
        out[key] = n


def _depth(c: _Checker, src: dict[str, Any], out: dict[str, Any], key: str, path: str) -> None:
    v = src.get(key)
    if v is None:
        return
    n = _number(v)
    if n is None or not 0.0 <= n <= MAX_DEPTH_CM:
        c.drop(path, f"{_show(v)} is not a depth between 0 and {MAX_DEPTH_CM:.0f} cm")
    else:
        out[key] = n


def _text(c: _Checker, src: dict[str, Any], out: dict[str, Any], key: str, path: str) -> None:
    v = src.get(key)
    if v is None:
        return
    if isinstance(v, str):
        out[key] = v
    else:
        c.drop(path, f"{_show(v)} is not text")


def _enum(
    c: _Checker,
    src: dict[str, Any],
    out: dict[str, Any],
    key: str,
    path: str,
    allowed: frozenset[str],
    *,
    lower: bool = True,
) -> None:
    v = src.get(key)
    if v is None:
        return
    s = v.strip() if isinstance(v, str) else None
    if s is not None and lower:
        s = s.lower()
    if s is None or s not in allowed:
        c.drop(path, f"{_show(v)} is not one of the allowed values")
    else:
        out[key] = s


def _point(c: _Checker, src: dict[str, Any], out: dict[str, Any], key: str, path: str) -> None:
    v = src.get(key)
    if v is None:
        return
    why = grid_point_problem(v)
    if why is None:
        out[key] = v
    else:
        c.drop(path, why)


def _box(
    c: _Checker,
    src: dict[str, Any],
    out: dict[str, Any],
    key: str,
    path: str,
    size: tuple[int, int] | None = None,
) -> None:
    v = src.get(key)
    if v is None:
        return
    why = grid_box_problem(v)
    if why is None and size is not None:
        why = _px_problem(v, size)
    if why is None:
        out[key] = v
    else:
        c.drop(path, why)


def _px_problem(v: Any, size: tuple[int, int]) -> str | None:
    from .muse_open import box_to_px, grid_box  # local: muse_open imports this module

    g = grid_box(v)
    if g is None:
        return f"{_show(v)} is not a box"
    return pixel_box_problem(box_to_px(g, size[0], size[1]), size[0], size[1])


def _points(
    c: _Checker, src: dict[str, Any], out: dict[str, Any], key: str, path: str, limit: int
) -> None:
    v = src.get(key)
    if v is None:
        return
    if not isinstance(v, list):
        c.drop(path, f"{_show(v)} is not a list of points")
        return
    kept = []
    for i, p in enumerate(v[:limit]):
        why = grid_point_problem(p)
        if why is None:
            kept.append(p)
        else:
            c.drop(f"{path}[{i}]", why)
    if len(v) > limit:
        c.drop(path, f"{len(v)} points, more than the {limit} asked for; the rest")
    out[key] = kept


def _waterline(c: _Checker, src: dict[str, Any], out: dict[str, Any], key: str, path: str) -> None:
    v = src.get(key)
    if v is None:
        return
    if not isinstance(v, list) or len(v) not in (0, 2):
        c.drop(path, f"{_show(v)} is not two points or []")
        return
    for p in v:
        why = grid_point_problem(p)
        if why is not None:
            c.drop(path, why)
            return
    out[key] = v


# --------------------------------------------------------------------------- scene


def check_scene(data: Any, width: int, height: int) -> Checked:
    """The scene reading (``muse_open.SCENE_PROMPT``) for a ``width`` by ``height`` photo."""
    c = _Checker(data, "the scene answer")
    src, out = c.src, c.out
    size = (width, height)
    if c.fatal is not None:
        return c.result()
    wv = src.get("water_visible")
    if isinstance(wv, bool):
        out["water_visible"] = wv
    else:
        c.need("water_visible", f"{_show(wv)} is not true or false")

    water = src.get("water")
    if water is not None:
        if isinstance(water, dict):
            w: dict[str, Any] = {}
            _box(c, water, w, "box", "water.box", size)
            _point(c, water, w, "point", "water.point")
            out["water"] = w
        else:
            c.drop("water", f"{_show(water)} is not an object")
    if src.get("water_points") is not None:  # an older shape, still accepted
        _points(c, src, out, "water_points", "water_points", 3)

    refs = src.get("references")
    if refs is not None and not isinstance(refs, list):
        c.drop("references", f"{_show(refs)} is not a list")
        refs = None
    kept_refs: list[dict[str, Any]] = []
    for i, r in enumerate((refs or [])[:5]):
        path = f"references[{i}]"
        if not isinstance(r, dict):
            c.drop(path, f"{_show(r)} is not an object")
            continue
        kind = r.get("kind")
        k = kind.strip() if isinstance(kind, str) else None
        if k not in REFERENCE_KINDS:
            c.drop(path, f"kind {_show(kind)} is not an allowed reference kind")
            continue
        o: dict[str, Any] = {"kind": k}
        _box(c, r, o, "box", f"{path}.box", size)
        _point(c, r, o, "point", f"{path}.point")
        if "box" not in o and "point" not in o:
            c.drop(path, "no valid box or point")
            continue
        _point(c, r, o, "rim_point", f"{path}.rim_point")
        _point(c, r, o, "head_top", f"{path}.head_top")
        _points(c, r, o, "tyre_points", f"{path}.tyre_points", 4)
        _points(c, r, o, "exclude_points", f"{path}.exclude_points", 3)
        _waterline(c, r, o, "waterline", f"{path}.waterline")
        _enum(c, r, o, "posture", f"{path}.posture", POSTURES)
        _bool(c, r, o, "adult", f"{path}.adult")
        _enum(c, r, o, "landmark", f"{path}.landmark", LANDMARKS)
        _share(c, r, o, "visible_fraction", f"{path}.visible_fraction")
        _enum(c, r, o, "vehicle", f"{path}.vehicle", _VEHICLE_MODELS, lower=False)
        _enum(c, r, o, "position", f"{path}.position", POSITIONS)
        _depth(c, r, o, "depth_cm", f"{path}.depth_cm")
        _text(c, r, o, "depth_reason", f"{path}.depth_reason")
        _share(c, r, o, "confidence", f"{path}.confidence")
        kept_refs.append(o)
    if refs is not None and len(refs) > 5:
        c.drop("references", f"{len(refs)} listed, more than the 5 asked for; the rest")
    out["references"] = kept_refs

    vs = src.get("vehicles")
    if vs is not None and not isinstance(vs, list):
        c.drop("vehicles", f"{_show(vs)} is not a list")
        vs = None
    kept_vs: list[dict[str, Any]] = []
    for i, v in enumerate((vs or [])[:4]):
        path = f"vehicles[{i}]"
        if not isinstance(v, dict):
            c.drop(path, f"{_show(v)} is not an object")
            continue
        o = {}
        _enum(c, v, o, "kind", f"{path}.kind", VEHICLE_KINDS)
        if "kind" not in o:
            c.drop(path, "no allowed vehicle kind")
            continue
        _box(c, v, o, "box", f"{path}.box", size)
        if "box" not in o:
            c.drop(path, "no valid box")
            continue
        _enum(c, v, o, "vehicle", f"{path}.vehicle", _VEHICLE_MODELS, lower=False)
        _bool(c, v, o, "wheels_visible", f"{path}.wheels_visible")
        kept_vs.append(o)
    if vs is not None and len(vs) > 4:
        c.drop("vehicles", f"{len(vs)} listed, more than the 4 asked for; the rest")
    out["vehicles"] = kept_vs

    overall = src.get("overall")
    if overall is not None:
        if isinstance(overall, dict):
            ov: dict[str, Any] = {}
            for key in ("depth_cm", "low_cm", "high_cm"):
                _depth(c, overall, ov, key, f"overall.{key}")
            if "low_cm" in ov and "high_cm" in ov and ov["low_cm"] > ov["high_cm"]:
                c.drop("overall.low_cm, overall.high_cm", "the low bound is above the high bound")
                del ov["low_cm"], ov["high_cm"]
            _text(c, overall, ov, "reason", "overall.reason")
            out["overall"] = ov
        else:
            c.drop("overall", f"{_show(overall)} is not an object")

    faces = src.get("faces")
    if faces is not None:
        if isinstance(faces, list):
            kept_faces = []
            for i, fb in enumerate(faces[:32]):
                b = fb.get("box") if isinstance(fb, dict) and "box" in fb else fb
                why = grid_box_problem(b) or _px_problem(b, size)
                if why is None:
                    kept_faces.append(b)
                else:
                    c.drop(f"faces[{i}]", why)
            out["faces"] = kept_faces
        else:
            c.drop("faces", f"{_show(faces)} is not a list")
    _text(c, src, out, "floating_debris", "floating_debris")

    q = src.get("image_quality")
    if q is not None:
        if isinstance(q, dict):
            qo: dict[str, Any] = {}
            _share(c, q, qo, "score", "image_quality.score")
            _bool(c, q, qo, "blurred", "image_quality.blurred")
            _bool(c, q, qo, "too_dark", "image_quality.too_dark")
            issues = q.get("issues")
            if issues is not None:
                if isinstance(issues, list) and all(isinstance(s, str) for s in issues):
                    qo["issues"] = issues
                else:
                    c.drop("image_quality.issues", f"{_show(issues)} is not a list of text")
            out["image_quality"] = qo
        else:
            c.drop("image_quality", f"{_show(q)} is not an object")
    return c.result()


# The vehicle catalogue's keys, plus "unknown" as the prompt allows.
_VEHICLE_MODELS: frozenset[str] = frozenset({*vehicles(), "unknown"})


# --------------------------------------------------------------------------- checks of masks


def check_wheel_answer(data: Any, panels: int) -> Checked:
    """The verifier's judgement of one or two proposed wheel outlines
    (``muse_open.verify_prompt``)."""
    c = _Checker(data, "the wheel check")
    src, out = c.src, c.out
    if c.fatal is not None:
        return c.result()
    choices = {"A", "B", "NONE"} if panels == 2 else {"A", "NONE"}
    ch = src.get("choice")
    if ch is None:
        if panels == 2:
            c.need("choice", "missing, but two outlines were shown")
    elif isinstance(ch, str) and ch.strip().upper() in choices:
        out["choice"] = ch.strip().upper() if ch.strip().upper() != "NONE" else "none"
    else:
        c.need("choice", f"{_show(ch)} is not one of {', '.join(sorted(choices))}")
    it = src.get("is_target")
    if isinstance(it, bool):
        out["is_target"] = it
    else:
        c.need("is_target", f"{_show(it)} is not true or false")
    oq = src.get("outline_quality")
    if isinstance(oq, str) and oq.strip().lower() in OUTLINE_QUALITY:
        out["outline_quality"] = oq.strip().lower()
    else:
        c.need("outline_quality", f"{_show(oq)} is not good, partial or wrong")
    _enum(c, src, out, "view", "view", VIEWS)
    _bool(c, src, out, "standing", "standing")
    _share(c, src, out, "tyre_edge_visible", "tyre_edge_visible")
    _bool(c, src, out, "waterline_visible", "waterline_visible")
    _waterline(c, src, out, "waterline", "waterline")
    _box(c, src, out, "tyre_box", "tyre_box")
    _box(c, src, out, "corrected_box", "corrected_box")
    _points(c, src, out, "tyre_points", "tyre_points", 4)
    _points(c, src, out, "exclude_points", "exclude_points", 3)
    _text(c, src, out, "reason", "reason")
    return c.result()


def check_water_answer(data: Any) -> Checked:
    """The verifier's judgement of the proposed water region (``water_verify_prompt``)."""
    c = _Checker(data, "the water check")
    src, out = c.src, c.out
    if c.fatal is not None:
        return c.result()
    iw = src.get("is_water")
    if isinstance(iw, bool):
        out["is_water"] = iw
    else:
        c.need("is_water", f"{_show(iw)} is not true or false")
    cov = src.get("coverage")
    if isinstance(cov, str) and cov.strip().lower() in COVERAGE:
        out["coverage"] = cov.strip().lower()
    else:
        c.need("coverage", f"{_show(cov)} is not good, partial or wrong")
    _box(c, src, out, "corrected_box", "corrected_box")
    _point(c, src, out, "corrected_point", "corrected_point")
    _text(c, src, out, "reason", "reason")
    return c.result()


# --------------------------------------------------------------------------- voice


def _language_code(p: Any) -> str | None:
    """An ISO 639-1 code from ``LANGUAGES``, allowing a region tag (``ta-IN``)."""
    if not isinstance(p, str):
        return None
    code = p.strip().lower()
    if code in LANGUAGES:
        return code
    if len(code) > 3 and code[2] in "-_" and code[:2] in LANGUAGES:
        return code[:2]
    return None


def check_transcript(data: Any) -> Checked:
    """A voice note's transcript (``muse_open.TRANSCRIBE_PROMPT``). An empty
    transcript is not a failed check: the caller asks again for that itself."""
    c = _Checker(data, "the transcript")
    src, out = c.src, c.out
    if c.fatal is not None:
        return c.result()
    text = src.get("text")
    if text is None or isinstance(text, str):
        out["text"] = text or ""
    else:
        c.need("text", f"{_show(text)} is not text")
    lang = src.get("language")
    if lang is not None:
        parts: list[Any]
        if isinstance(lang, str):
            parts = [p for p in re.split(r"[+,/\s]+", lang) if p]
        elif isinstance(lang, list):
            parts = list(lang)
        else:
            parts = []
            c.drop("language", f"{_show(lang)} is not language codes")
        good: list[str] = []
        for p in parts:
            code = _language_code(p)
            if code is None:
                c.drop("language", f"{_show(p)} is not a supported language code")
            elif code not in good:
                good.append(code)
        out["language"] = "+".join(good)
    for key in ("place_spoken", "place_text", "depth_words", "depth_words_en"):
        _text(c, src, out, key, key)
    for key in ("depth_cm", "depth_low_cm", "depth_high_cm"):
        _depth(c, src, out, key, key)
    lo, hi = out.get("depth_low_cm"), out.get("depth_high_cm")
    if lo is not None and hi is not None and lo > hi:
        c.drop("depth_low_cm, depth_high_cm", "the low bound is above the high bound")
        del out["depth_low_cm"], out["depth_high_cm"]
    return c.result()
