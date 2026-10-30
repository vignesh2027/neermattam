"""Image and video intake: limits, EXIF handling, sanitising.

Uploaded photos are decoded with EXIF orientation applied (the SAM docs require
prompt coordinates in the media's displayed size), shrunk to a working size, and
re-encoded without any metadata. Only the re-encoded copy is sent to models or
stored, so GPS tags never leave this process. Before discarding the metadata we
read ``DateTimeOriginal`` (for freshness checks) and the 35 mm equivalent focal
length (for the optional perspective correction).
"""

from __future__ import annotations

import hashlib
import io
import math
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import cv2
import numpy as np
from numpy.typing import NDArray
from PIL import Image, ImageOps, UnidentifiedImageError

MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_VIDEO_BYTES = 60 * 1024 * 1024
MAX_SIDE_PX = 8192
MAX_PIXELS = 40_000_000
MIN_SIDE_PX = 64
WORKING_MAX_SIDE = 2048
MAX_VIDEO_SECONDS = 30.0

_EXIF_IFD = 0x8769
_GPS_IFD = 0x8825
_DATETIME_ORIGINAL = 36867
_OFFSET_TIME_ORIGINAL = 36881
_FOCAL_35MM = 41989


class MediaRejected(ValueError):
    """The upload breaks a limit or cannot be decoded; the message is user-facing."""


@dataclass(frozen=True, slots=True)
class ImageInput:
    rgb: NDArray[np.uint8] = field(repr=False)
    jpeg: bytes = field(repr=False)  # metadata-free, orientation applied, working size
    width: int
    height: int
    captured_at: str | None = None  # RFC 3339; UTC when the camera recorded an offset
    focal_35mm: float | None = None
    # SHA-256 (hex) of the uploaded file exactly as received; None for an image
    # made in memory (a video's key frame, a test fixture).
    source_sha256: str | None = None

    @property
    def media_type(self) -> str:
        return "image/jpeg"


def _captured_at(exif_ifd: dict[int, object]) -> str | None:
    raw = exif_ifd.get(_DATETIME_ORIGINAL)
    if not isinstance(raw, str):
        return None
    try:
        local = datetime.strptime(raw.strip("\x00 "), "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None
    off = exif_ifd.get(_OFFSET_TIME_ORIGINAL)
    if isinstance(off, str) and len(off.strip()) >= 6:
        o = off.strip()
        sign = -1 if o.startswith("-") else 1
        try:
            hh, mm = int(o[1:3]), int(o[4:6])
        except ValueError:
            return local.isoformat()
        tz = timezone(sign * timedelta(hours=hh, minutes=mm))
        return local.replace(tzinfo=tz).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return local.isoformat()  # no offset recorded: local time, zone unknown


UTC = timezone.utc  # noqa: UP017  (kept explicit for readers)


def load_image(data: bytes) -> ImageInput:
    """Decode, check limits, apply EXIF orientation, shrink and strip metadata."""
    if len(data) > MAX_IMAGE_BYTES:
        raise MediaRejected(f"image is larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB")
    too_big = f"image is larger than {MAX_SIDE_PX} px or {MAX_PIXELS // 10**6} MP"
    try:
        im = Image.open(io.BytesIO(data))
    except Image.DecompressionBombError as exc:  # not an OSError
        raise MediaRejected(too_big) from exc
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        raise MediaRejected("image could not be decoded") from exc
    # Many phone cameras write JPEGs with a multi-picture extension; Pillow
    # reports those as MPO. The first frame is the photo.
    if im.format not in ("JPEG", "MPO", "PNG", "WEBP"):
        raise MediaRejected("image must be JPEG or PNG")
    w, h = im.size
    if max(w, h) > MAX_SIDE_PX or w * h > MAX_PIXELS:
        raise MediaRejected(too_big)
    if min(w, h) < MIN_SIDE_PX:
        raise MediaRejected(f"image is smaller than {MIN_SIDE_PX} px")
    # Hostile files can fail anywhere in Pillow (oversized PNG text or ICC
    # chunks raise ValueError, a truncated PNG raises OSError while EXIF is
    # read, a malformed EXIF block can raise struct.error), so every decoding
    # step is guarded and turned into a 422, never a 500.
    try:
        exif = im.getexif()
        exif_ifd = dict(exif.get_ifd(_EXIF_IFD)) if exif else {}
        captured = _captured_at(exif_ifd)
        focal = exif_ifd.get(_FOCAL_35MM)
        focal_35 = (
            float(focal) if isinstance(focal, int | float) and 8 <= float(focal) <= 400 else None
        )
        upright: Image.Image = ImageOps.exif_transpose(im).convert("RGB")
    except Exception as exc:  # see above: any decoding failure is a bad upload
        raise MediaRejected("image could not be decoded") from exc
    return _from_pil(upright, captured, focal_35, hashlib.sha256(data).hexdigest())


def _from_pil(
    im: Image.Image, captured: str | None, focal_35: float | None, sha: str | None = None
) -> ImageInput:
    w, h = im.size
    scale = min(1.0, WORKING_MAX_SIDE / max(w, h))
    if scale < 1.0:
        im = im.resize(
            (max(1, round(w * scale)), max(1, round(h * scale))), Image.Resampling.LANCZOS
        )
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=92)  # no exif= argument: all metadata dropped
    rgb = np.asarray(im, dtype=np.uint8)
    return ImageInput(rgb, buf.getvalue(), im.size[0], im.size[1], captured, focal_35, sha)


def image_from_rgb(rgb: NDArray[np.uint8]) -> ImageInput:
    return _from_pil(Image.fromarray(rgb), None, None)


def sanitize_for_storage(data: bytes) -> bytes:
    """Re-encode an uploaded image with no metadata at all (so no GPS)."""
    return load_image(data).jpeg


def has_gps(data: bytes) -> bool:
    try:
        exif = Image.open(io.BytesIO(data)).getexif()
    except (UnidentifiedImageError, OSError):
        return False
    return bool(exif) and bool(exif.get_ifd(_GPS_IFD))


def sharpness(rgb: NDArray[np.uint8]) -> float:
    """Variance of the Laplacian on the grey image: low means blurred."""
    grey = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    return float(cv2.Laplacian(grey, cv2.CV_64F).var())


@dataclass(frozen=True, slots=True)
class VideoInput:
    data: bytes = field(repr=False)
    media_type: str
    fps: float
    n_frames: int
    width: int
    height: int
    key_index: int
    key_frame: ImageInput
    source_sha256: str | None = None  # of the uploaded file exactly as received

    @property
    def seconds(self) -> float:
        return self.n_frames / self.fps if self.fps > 0 else math.nan


def load_video(data: bytes, media_type: str = "video/mp4", scan_frames: int = 30) -> VideoInput:
    """Check limits and pick the sharpest of the first frames as the key frame."""
    if len(data) > MAX_VIDEO_BYTES:
        raise MediaRejected(f"video is larger than {MAX_VIDEO_BYTES // (1024 * 1024)} MB")
    suffix = ".mov" if "quicktime" in media_type else ".mp4"
    fd, path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            raise MediaRejected("video could not be decoded")
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if fps <= 0 or n <= 0:
            raise MediaRejected("video has no frame rate or frames")
        if n / fps > MAX_VIDEO_SECONDS:
            raise MediaRejected(f"video is longer than {MAX_VIDEO_SECONDS:.0f} seconds")
        best, best_idx, best_score = None, 0, -1.0
        for i in range(min(n, scan_frames)):
            ok, bgr = cap.read()
            if not ok:
                break
            rgb = np.asarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB), dtype=np.uint8)
            score = sharpness(rgb)
            if score > best_score:
                best, best_idx, best_score = rgb, i, score
        cap.release()
    finally:
        os.unlink(path)
    if best is None:
        raise MediaRejected("video has no readable frames")
    h, w = best.shape[:2]
    if max(w, h) > MAX_SIDE_PX or min(w, h) < MIN_SIDE_PX:
        raise MediaRejected("video frame size is out of range")
    key = image_from_rgb(best)
    return VideoInput(
        strip_video_metadata(data),
        media_type,
        fps,
        n,
        w,
        h,
        best_idx,
        key,
        hashlib.sha256(data).hexdigest(),
    )


# Boxes that can hold location or other personal metadata in MP4 and QuickTime
# files: user data (``udta``, where ``\xa9xyz`` GPS lives), metadata (``meta``,
# where Apple's ``com.apple.quicktime.location.ISO6709`` lives) and ``uuid``
# boxes (XMP). They are turned into ``free`` boxes of the same size, with the
# payload zeroed, so every chunk offset in the file stays valid.
_VIDEO_META_BOXES = {b"udta", b"meta", b"uuid"}
_VIDEO_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"edts"}


def strip_video_metadata(data: bytes) -> bytes:
    """MP4 or MOV bytes with every metadata box blanked out, frames untouched."""
    buf = bytearray(data)

    def walk(start: int, end: int, depth: int) -> None:
        pos = start
        while pos + 8 <= end:
            size = int.from_bytes(buf[pos : pos + 4], "big")
            kind = bytes(buf[pos + 4 : pos + 8])
            header = 8
            if size == 1:
                if pos + 16 > end:
                    return
                size = int.from_bytes(buf[pos + 8 : pos + 16], "big")
                header = 16
            elif size == 0:
                size = end - pos
            if size < header or pos + size > end:
                return  # malformed: leave the rest alone (the decoder accepted it)
            if kind in _VIDEO_META_BOXES:
                buf[pos + 4 : pos + 8] = b"free"
                buf[pos + header : pos + size] = bytes(size - header)
            elif kind in _VIDEO_CONTAINERS and depth < 8:
                walk(pos + header, pos + size, depth + 1)
            pos += size

    walk(0, len(buf), 0)
    return bytes(buf)
