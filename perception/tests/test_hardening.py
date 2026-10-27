"""Regression tests for hostile uploads, event-loop blocking, metadata and the bias correction."""

from __future__ import annotations

import asyncio
import io
import os
import struct
import tempfile
import time
import zlib
from pathlib import Path

import cv2
import httpx
import numpy as np
import pytest
from PIL import Image

from neermattam_perception import adapters as ad
from neermattam_perception import geometry as g
from neermattam_perception import media, pipeline, sam
from neermattam_perception.adapters import MaskResult
from neermattam_perception.media import image_from_rgb
from neermattam_perception.pipeline import Pipeline, PipelineConfig
from neermattam_perception.service import create_app

from .conftest import FakeMasks, FakeScene, reading_for


def _chunk(kind: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)


def _png_header_only(w: int, h: int) -> bytes:
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(b"\x00" * 16))
        + _chunk(b"IEND", b"")
    )


def _small_png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (100, 100)).save(buf, "PNG")
    return buf.getvalue()


def test_decompression_bomb_header_is_rejected_not_500() -> None:
    # 200 MP: Pillow raises DecompressionBombError (not an OSError) inside open().
    with pytest.raises(media.MediaRejected, match="larger"):
        media.load_image(_png_header_only(20000, 10000))


def test_truncated_large_png_is_rejected() -> None:
    with pytest.raises(media.MediaRejected):
        media.load_image(_png_header_only(8000, 5000))


@pytest.mark.parametrize("kind", [b"zTXt", b"iCCP"])
def test_compressed_chunk_bomb_is_rejected(kind: bytes) -> None:
    png = _small_png()
    bomb = _chunk(kind, b"k\x00\x00" + zlib.compress(b"A" * 40_000_000, 9))
    with pytest.raises(media.MediaRejected):
        media.load_image(png[:33] + bomb + png[33:])


def test_multi_picture_jpeg_from_phones_is_accepted() -> None:
    im = Image.new("RGB", (120, 90), (10, 20, 30))
    buf = io.BytesIO()
    im.save(buf, "MPO", save_all=True, append_images=[im])
    assert Image.open(io.BytesIO(buf.getvalue())).format == "MPO"
    out = media.load_image(buf.getvalue())
    assert (out.width, out.height) == (120, 90)


def test_png_with_alpha_and_gps_exif_is_stripped() -> None:
    exif = Image.Exif()
    gps = exif.get_ifd(0x8825)
    gps[1], gps[2] = "N", (13.0, 4.0, 30.0)
    buf = io.BytesIO()
    Image.new("RGBA", (100, 80), (0, 0, 0, 0)).save(buf, "PNG", exif=exif.tobytes())
    assert media.has_gps(buf.getvalue())
    out = media.load_image(buf.getvalue())
    assert not media.has_gps(out.jpeg) and not Image.open(io.BytesIO(out.jpeg)).getexif()


def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def test_video_metadata_boxes_are_blanked_in_place() -> None:
    gps = b"\xa9xyz" + b"+13.0827+080.2707/"
    moov = _box(b"moov", _box(b"mvhd", b"\x00" * 20) + _box(b"udta", _box(b"\xa9xyz", gps)))
    data = _box(b"ftyp", b"isom\x00\x00\x00\x00") + moov + _box(b"mdat", b"frames")
    out = media.strip_video_metadata(data)
    assert len(out) == len(data)
    assert b"+13.0827" not in out and b"udta" not in out
    assert out.endswith(_box(b"mdat", b"frames"))


def test_uploaded_video_bytes_carry_no_location() -> None:
    fd, path = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)
    try:
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")  # type: ignore[attr-defined]
        out = cv2.VideoWriter(path, fourcc, 10.0, (160, 120))
        for _ in range(5):
            out.write(np.full((120, 160, 3), 90, np.uint8))
        out.release()
        data = Path(path).read_bytes()
    finally:
        os.unlink(path)
    # Append a QuickTime-style location box inside moov, as phones do.
    pos = data.index(b"moov") - 4
    size = struct.unpack(">I", data[pos : pos + 4])[0]
    meta = _box(b"meta", b"com.apple.quicktime.location.ISO6709+13.08+080.27/")
    moov = struct.pack(">I", size + len(meta)) + data[pos + 4 : pos + size] + meta
    tagged = data[:pos] + moov + data[pos + size :]
    v = media.load_video(tagged)
    assert b"+13.08" in tagged and b"+13.08" not in v.data
    assert media.load_video(v.data).n_frames == 5  # still a valid file


def test_scene_answer_missing_fields_is_a_model_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        bad = {"water_visible": True, "references": [{"kind": "car_wheel"}]}
        import json

        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(bad)}}]})

    adapter = ad.MetaApiAdapter("k", transport=httpx.MockTransport(handler))
    img = image_from_rgb(np.zeros((100, 100, 3), np.uint8))
    with pytest.raises(ad.ModelError):
        asyncio.run(adapter.read_scene(img))


def test_body_limit_refuses_oversized_request(easy_case) -> None:  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient

    from neermattam_perception import service

    app = create_app(Pipeline(FakeScene(reading_for(easy_case)), FakeMasks({})))
    client = TestClient(app)
    r = client.post(
        "/v1/measure",
        content=b"x" * 10,
        headers={
            "content-type": "multipart/form-data; boundary=b",
            "content-length": str(service.MAX_BODY_BYTES + 1),
        },
    )
    assert r.status_code == 413


def test_geometry_does_not_block_the_event_loop(easy_case, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    real = g.measure_wheel_depth

    def slow(*args, **kwargs):
        time.sleep(1.5)  # stands in for seconds of CPU-bound fitting
        return real(*args, **kwargs)

    monkeypatch.setattr(pipeline, "measure_wheel_depth", slow)
    by_id = {1: easy_case.wheel_mask, 2: easy_case.rim_mask, 16: easy_case.water_mask}

    class DirectMasks(FakeMasks):
        """Hands back masks without the slow wire round trip, so only geometry is timed."""

        async def segment(self, image, objects):
            out = {}
            for o in objects:
                if o.object_id in self.by_id:
                    m = self.by_id[o.object_id]
                    out[o.object_id] = sam.ObjectMask(str(o.object_id), 0, m, (0, 0, 1, 1), 0.9)
            return MaskResult(out)

    masks = DirectMasks(by_id)
    pipe = Pipeline(
        FakeScene(reading_for(easy_case)),
        masks,
        PipelineConfig(geometry=g.GeometryConfig(mc_draws=0)),
    )
    app = create_app(pipe)
    buf = io.BytesIO()
    Image.fromarray(easy_case.image).save(buf, "JPEG")

    async def run() -> float:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t", timeout=60) as c:
            t0 = time.perf_counter()

            async def health() -> float:
                await asyncio.sleep(0.3)
                r = await c.get("/healthz")
                assert r.status_code == 200
                return time.perf_counter() - t0

            files = {"image": ("p.jpg", buf.getvalue(), "image/jpeg")}
            measured, healthy = await asyncio.gather(c.post("/v1/measure", files=files), health())
            assert measured.status_code == 200
            return healthy

    assert asyncio.run(run()) < 1.2


def test_bias_correction_only_raises_depth(easy_case) -> None:  # type: ignore[no-untyped-def]
    args = (easy_case.wheel_mask, easy_case.water_mask, 43.4, 0.0)
    base = g.measure_wheel_depth(
        *args, g.GeometryConfig(mc_draws=0, model_bias_table=()), rim_mask=easy_case.rim_mask
    )
    biased = g.measure_wheel_depth(
        *args,
        g.GeometryConfig(mc_draws=0, model_bias_table=((100.0, -0.05), (360.0, -0.05))),
        rim_mask=easy_case.rim_mask,
    )
    assert isinstance(base, g.DepthMeasurement) and isinstance(biased, g.DepthMeasurement)
    assert biased.depth_cm == pytest.approx(base.depth_cm + 0.05 * 43.4, abs=1e-6)
    assert biased.fraction_bias == pytest.approx(-0.05)
    # The second waterline is corrected the same way, so agreement is unchanged.
    line = base.waterline.line
    assert g.depth_from_line(biased, line) == pytest.approx(biased.depth_cm, abs=1e-6)
    # A positive table entry is ignored: the correction never lowers a depth.
    assert g.model_bias(150.0, ((100.0, 0.1), (200.0, 0.1))) == 0.0
