"""Client for the Neermattam SAM service on Modal (see docs/MODAL.md).

Needs only numpy and the standard library (Pillow too if you pass a numpy
image). The URL and token come from ``MODAL_SAM_URL`` and ``MODAL_SAM_TOKEN``
unless given explicitly.

    from client import SamPrompt, segment

    res = segment("photo.jpg", [SamPrompt(id=1, box=(560, 580, 700, 660))])
    best = res.masks[1][0]          # highest scoring candidate for prompt 1
    best.mask                       # numpy bool array, image height x width
    best.score, best.bbox, best.area

Coordinates are pixels of the image as sent, after EXIF orientation (the
server applies ``ImageOps.exif_transpose``). Boxes are ``(x1, y1, x2, y2)``,
points ``(x, y)``. ``bbox`` comes back with exclusive ``x2`` and ``y2``.

``segment`` is synchronous; an async adapter can wrap it in
``asyncio.to_thread``.
"""

from __future__ import annotations

import base64
import io
import json
import os
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

DEFAULT_TIMEOUT_S = 120.0


class SamServiceError(RuntimeError):
    """The service refused the request or could not be reached."""

    def __init__(self, status: int | None, detail: str) -> None:
        super().__init__(f"SAM service error {status}: {detail}")
        self.status = status
        self.detail = detail


@dataclass(frozen=True, slots=True)
class SamPrompt:
    """One object to segment. Give a box, positive points, or both."""

    id: str | int
    box: tuple[float, float, float, float] | None = None
    positive_points: tuple[tuple[float, float], ...] = ()
    negative_points: tuple[tuple[float, float], ...] = ()

    def to_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id}
        if self.box is not None:
            d["box"] = [float(v) for v in self.box]
        if self.positive_points:
            d["positive_points"] = [[float(x), float(y)] for x, y in self.positive_points]
        if self.negative_points:
            d["negative_points"] = [[float(x), float(y)] for x, y in self.negative_points]
        return d


@dataclass(frozen=True, slots=True)
class SamMask:
    """One candidate mask for one prompt."""

    mask: NDArray[np.bool_] = field(repr=False)
    score: float  # SAM's predicted IoU
    bbox: tuple[int, int, int, int] | None  # x1, y1, x2, y2 (x2, y2 exclusive)
    area: int
    index: int  # which of SAM's three proposals this was


@dataclass(frozen=True, slots=True)
class SamResponse:
    masks: dict[str | int, list[SamMask]]  # prompt id: candidates, best first
    model: str
    device: str
    image_size: tuple[int, int]  # width, height
    timings_ms: dict[str, float]
    round_trip_ms: float
    raw: dict[str, Any] = field(repr=False)


def rle_decode(rle: dict[str, Any]) -> NDArray[np.bool_]:
    """Inverse of the server's encoding: COCO uncompressed RLE, column-major, zeros first."""
    h, w = (int(v) for v in rle["size"])
    counts = np.asarray(rle["counts"], dtype=np.int64)
    if counts.sum() != h * w:
        raise ValueError(f"RLE counts sum to {int(counts.sum())}, expected {h * w}")
    values = np.zeros(counts.size, dtype=bool)
    values[1::2] = True
    flat = np.repeat(values, counts)
    return flat.reshape((h, w), order="F")


def _image_bytes(image: bytes | str | Path | NDArray[np.uint8] | Any) -> bytes:
    if isinstance(image, bytes):
        return image
    if isinstance(image, (str, Path)):
        return Path(image).read_bytes()
    from PIL import Image

    pil = Image.fromarray(np.asarray(image)) if isinstance(image, np.ndarray) else image
    buf = io.BytesIO()
    # PNG keeps pixels exact, so prompt coordinates and mask pixels line up.
    pil.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def _prompt_json(p: SamPrompt | dict[str, Any]) -> dict[str, Any]:
    return p.to_json() if isinstance(p, SamPrompt) else dict(p)


def _ipv4_only() -> Any:
    """Context that resolves names to IPv4 only (for networks whose IPv6 does not route)."""
    import contextlib

    @contextlib.contextmanager
    def ctx() -> Any:
        real = socket.getaddrinfo

        def v4(host: Any, *args: Any, **kwargs: Any) -> Any:
            res = real(host, *args, **kwargs)
            return [r for r in res if r[0] == socket.AF_INET] or res

        socket.getaddrinfo = v4  # type: ignore[assignment]
        try:
            yield
        finally:
            socket.getaddrinfo = real  # type: ignore[assignment]

    return ctx()


def _ssl_context() -> Any:
    """Certifi's CA bundle when installed (python.org builds on macOS ship none), else the system's."""
    import ssl

    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def _request(
    method: str,
    url: str,
    token: str,
    body: bytes | None,
    timeout: float,
    ipv4: bool,
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    import contextlib

    guard = _ipv4_only() if ipv4 else contextlib.nullcontext()
    try:
        with guard, urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            return json.loads(resp.read())  # type: ignore[no-any-return]
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read()).get("detail", "")
        except (json.JSONDecodeError, AttributeError):
            detail = exc.reason
        raise SamServiceError(exc.code, str(detail)) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SamServiceError(None, str(exc)) from exc


def _config(url: str | None, token: str | None) -> tuple[str, str]:
    url = url or os.environ.get("MODAL_SAM_URL")
    token = token or os.environ.get("MODAL_SAM_TOKEN")
    if not url or not token:
        raise SamServiceError(None, "set MODAL_SAM_URL and MODAL_SAM_TOKEN")
    return url.rstrip("/"), token


def segment(
    image: bytes | str | Path | NDArray[np.uint8] | Any,
    prompts: Iterable[SamPrompt | dict[str, Any]],
    *,
    model: str = "large",
    max_masks: int = 3,
    precision: str | None = None,
    url: str | None = None,
    token: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    ipv4: bool = True,
) -> SamResponse:
    """Segment every prompt in one image with one request (one image embedding).

    ``image`` is encoded bytes (JPEG or PNG), a file path, an RGB numpy array
    or a PIL image. ``model`` is ``large`` (default), ``base-plus``, ``small``,
    or the gated ``sam3.1`` and ``sam3``. Returns each prompt's candidates
    sorted best first, with masks decoded to full-image boolean arrays.
    """
    base, tok = _config(url, token)
    payload: dict[str, Any] = {
        "image_b64": base64.b64encode(_image_bytes(image)).decode("ascii"),
        "prompts": [_prompt_json(p) for p in prompts],
        "model": model,
        "max_masks": max_masks,
    }
    if precision is not None:
        payload["precision"] = precision
    t0 = time.perf_counter()
    raw = _request("POST", f"{base}/segment", tok, json.dumps(payload).encode(), timeout, ipv4)
    rtt = round(1000 * (time.perf_counter() - t0), 1)
    masks: dict[str | int, list[SamMask]] = {}
    for res in raw["results"]:
        masks[res["id"]] = [
            SamMask(
                mask=rle_decode(c["rle"]),
                score=float(c["score"]),
                bbox=tuple(c["bbox"]) if c["bbox"] is not None else None,  # type: ignore[arg-type]
                area=int(c["area"]),
                index=int(c["index"]),
            )
            for c in res["candidates"]
        ]
    w, h = raw["image_size"]
    return SamResponse(masks, raw["model"], raw["device"], (w, h), raw["timings_ms"], rtt, raw)


def health(
    *,
    url: str | None = None,
    token: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    ipv4: bool = True,
) -> dict[str, Any]:
    """The loaded model and device. Starts a GPU container if none is running."""
    base, tok = _config(url, token)
    return _request("GET", f"{base}/health", tok, None, timeout, ipv4)


def prompts_from_boxes(
    boxes: Sequence[tuple[float, float, float, float]],
) -> list[SamPrompt]:
    """Convenience: one box prompt per box, ids 1, 2, 3 and so on."""
    return [SamPrompt(id=i + 1, box=tuple(b)) for i, b in enumerate(boxes)]  # type: ignore[arg-type]
