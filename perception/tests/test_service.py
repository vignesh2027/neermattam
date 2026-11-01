from __future__ import annotations

import io
import json
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from neermattam_perception import media
from neermattam_perception.adapters import MaskResult, ModelError
from neermattam_perception.geometry import GeometryConfig
from neermattam_perception.pipeline import Pipeline, PipelineConfig
from neermattam_perception.sam import ObjectMask
from neermattam_perception.service import create_app

from .conftest import FakeMasks, FakeScene, reading_for


def jpeg_with_exif(rgb: np.ndarray, *, gps: bool = True, offset: str | None = "+05:30") -> bytes:
    im = Image.fromarray(rgb)
    exif = Image.Exif()
    ifd = exif.get_ifd(0x8769)
    ifd[36867] = "2026:10:28 15:45:00"  # DateTimeOriginal
    if offset:
        ifd[36881] = offset  # OffsetTimeOriginal
    ifd[41989] = 26  # FocalLengthIn35mmFilm
    if gps:
        g = exif.get_ifd(0x8825)
        g[1], g[2], g[3], g[4] = "N", (12.0, 58.0, 44.0), "E", (80.0, 13.0, 15.0)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", exif=exif.tobytes())
    return buf.getvalue()


@pytest.fixture()
def client_and_store(easy_case, tmp_path: Path):  # type: ignore[no-untyped-def]
    scene = FakeScene(reading_for(easy_case))
    masks = FakeMasks({1: easy_case.wheel_mask, 2: easy_case.rim_mask, 16: easy_case.water_mask})
    pipe = Pipeline(scene, masks, PipelineConfig(geometry=GeometryConfig(mc_draws=5)))
    return TestClient(create_app(pipe, str(tmp_path))), tmp_path, scene


def test_health(client_and_store) -> None:  # type: ignore[no-untyped-def]
    client, _, _ = client_and_store
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").status_code == 200


def test_readyz_503_when_models_unreachable(client_and_store) -> None:  # type: ignore[no-untyped-def]
    client, _, scene = client_and_store
    scene.is_ready = False
    r = client.get("/readyz")
    assert r.status_code == 503 and r.json()["reason"] == "fake not ready"


def test_measure_image(client_and_store, easy_case) -> None:  # type: ignore[no-untyped-def]
    client, store, _ = client_and_store
    data = jpeg_with_exif(easy_case.image)
    assert media.has_gps(data)
    r = client.post(
        "/v1/measure",
        files={"image": ("p.jpg", data, "image/jpeg")},
        data={"hint_diameter_cm": "43.4"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["usable"] is True
    assert body["depth_cm"] == pytest.approx(easy_case.depth_cm, abs=2.0)
    # 15:45 at +05:30 is 10:15 UTC.
    assert r.headers["x-captured-at"] == "2026-10-28T10:15:00Z"
    stored = store / f"{body['measurement_id']}.jpg"
    assert stored.exists() and not media.has_gps(stored.read_bytes())
    assert not Image.open(stored).getexif()  # no metadata at all
    saved = json.loads((store / f"{body['measurement_id']}.json").read_text())
    assert "overlay_png_b64" not in saved and saved["depth_cm"] == body["depth_cm"]


def test_capture_time_without_offset_is_local(easy_case) -> None:  # type: ignore[no-untyped-def]
    img = media.load_image(jpeg_with_exif(easy_case.image, gps=False, offset=None))
    assert img.captured_at == "2026-10-28T15:45:00"
    assert img.focal_35mm == 26.0


def test_exif_orientation_is_applied() -> None:
    rgb = np.zeros((100, 200, 3), np.uint8)
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90 degrees clockwise for display
    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, format="JPEG", exif=exif.tobytes())
    img = media.load_image(buf.getvalue())
    assert (img.width, img.height) == (100, 200)


def test_rejects_bad_requests(client_and_store) -> None:  # type: ignore[no-untyped-def]
    client, _, _ = client_and_store
    assert client.post("/v1/measure").status_code == 422
    bad = client.post("/v1/measure", files={"image": ("x.jpg", b"not an image", "image/jpeg")})
    assert bad.status_code == 422 and "decoded" in bad.json()["detail"]
    tiny = io.BytesIO()
    Image.fromarray(np.zeros((20, 20, 3), np.uint8)).save(tiny, format="PNG")
    r = client.post("/v1/measure", files={"image": ("t.png", tiny.getvalue(), "image/png")})
    assert r.status_code == 422 and "smaller" in r.json()["detail"]
    r = client.post("/v1/measure", files={"image": ("t.gif", b"GIF89a", "image/gif")})
    assert r.status_code == 415
    ok = io.BytesIO()
    Image.fromarray(np.zeros((80, 80, 3), np.uint8)).save(ok, format="PNG")
    r = client.post(
        "/v1/measure",
        files={"image": ("o.png", ok.getvalue(), "image/png")},
        data={"hint_reference": "boat"},
    )
    assert r.status_code == 422
    r = client.post(
        "/v1/measure",
        files={"image": ("o.png", ok.getvalue(), "image/png")},
        data={"hint_diameter_cm": "900"},
    )
    assert r.status_code == 422


def test_upload_size_limit(client_and_store, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    client, _, _ = client_and_store
    from neermattam_perception import service

    monkeypatch.setattr(service, "MAX_IMAGE_BYTES", 1000)
    r = client.post("/v1/measure", files={"image": ("big.jpg", b"x" * 5000, "image/jpeg")})
    assert r.status_code == 413


def test_dimension_limit(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(media, "MAX_SIDE_PX", 100)
    buf = io.BytesIO()
    Image.fromarray(np.zeros((90, 150, 3), np.uint8)).save(buf, format="PNG")
    with pytest.raises(media.MediaRejected, match="larger"):
        media.load_image(buf.getvalue())


def test_model_failure_is_503(client_and_store, easy_case) -> None:  # type: ignore[no-untyped-def]
    client, _, scene = client_and_store

    async def boom(image):  # type: ignore[no-untyped-def]
        raise ModelError("MODEL_API_KEY is not set")

    scene.read_scene = boom
    data = jpeg_with_exif(easy_case.image)
    r = client.post("/v1/measure", files={"image": ("p.jpg", data, "image/jpeg")})
    assert r.status_code == 503 and "MODEL_API_KEY" in r.json()["detail"]


def test_key_from_dotenv(tmp_path: Path) -> None:
    from neermattam_perception.service import key_from_dotenv

    (tmp_path / ".env").write_text("# secrets\nOTHER=1\nexport MODEL_API_KEY='abc123'\n")
    nested = tmp_path / "perception" / "src"
    nested.mkdir(parents=True)
    assert key_from_dotenv(nested) == "abc123"
    (tmp_path / ".env").write_text("OTHER=1\n")
    assert key_from_dotenv(nested) is None


# --------------------------------------------------------------------------- probes


def test_readiness_answers_from_the_last_check() -> None:
    import asyncio

    from neermattam_perception.service import Readiness

    now = [100.0]
    results = [(True, "ok")]

    async def check() -> tuple[bool, str]:
        return results[-1]

    r = Readiness(check, interval_s=15.0, grace_s=45.0, clock=lambda: now[0])

    async def go() -> tuple[bool, str]:
        return await r.state()

    assert asyncio.run(go()) == (True, "ok")
    results.append((False, "Baseten did not answer within 10 s"))
    asyncio.run(r.refresh())
    now[0] += 30.0  # one failed check, 30 s after the last success: still ready
    ok, why = asyncio.run(go())
    assert ok and "last check failed" in why
    now[0] += 20.0  # 50 s without a success: unavailable
    assert asyncio.run(go()) == (False, "Baseten did not answer within 10 s")


def test_readiness_check_that_hangs_is_cut() -> None:
    import asyncio

    from neermattam_perception.service import Readiness

    async def hang() -> tuple[bool, str]:
        await asyncio.sleep(10)
        return True, "ok"

    r = Readiness(hang, inline_timeout_s=0.05)
    ok, why = asyncio.run(r.state())
    assert not ok and why == "readiness check took longer than 0.1 s"


@pytest.fixture()
def slow_app(easy_case, tmp_path: Path):  # type: ignore[no-untyped-def]
    import asyncio
    import time

    class SlowReadyScene(FakeScene):
        ready_delay_s: float = 0.0

        async def ready(self) -> tuple[bool, str]:
            await asyncio.sleep(self.ready_delay_s)
            return True, "ok"

    class SlowMasks(FakeMasks):
        async def segment(self, image, objects):  # type: ignore[no-untyped-def]
            # Local SAM runs in a worker thread for about a second per request and
            # hands back masks (the wire-format round trip of FakeMasks is skipped:
            # it is test overhead, and the hosted adapter decodes in a thread).
            await asyncio.to_thread(time.sleep, 1.0)
            out = {}
            for o in objects:
                m = self.by_id.get(o.object_id)
                if m is not None:
                    ys, xs = np.nonzero(m)
                    box = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
                    out[o.object_id] = ObjectMask(str(o.object_id), 0, m, box, 0.9)
            return MaskResult(out, 0.0)

    scene = SlowReadyScene(reading_for(easy_case))
    masks = SlowMasks({1: easy_case.wheel_mask, 2: easy_case.rim_mask, 16: easy_case.water_mask})
    pipe = Pipeline(scene, masks, PipelineConfig(geometry=GeometryConfig(mc_draws=300)))
    return create_app(pipe, str(tmp_path)), scene


def test_probes_answer_fast_while_a_photo_is_measured(slow_app, easy_case) -> None:  # type: ignore[no-untyped-def]
    import threading
    import time

    app, scene = slow_app
    data = jpeg_with_exif(easy_case.image)
    with TestClient(app) as client:
        scene.ready_delay_s = 5.0  # Baseten is slow now: the probe must not wait for it
        done: list[int] = []

        def measure() -> None:
            r = client.post("/v1/measure", files={"image": ("p.jpg", data, "image/jpeg")})
            done.append(r.status_code)

        worker = threading.Thread(target=measure)
        worker.start()
        time.sleep(0.2)
        worst = 0.0
        probes = 0
        while worker.is_alive() and probes < 40:
            for path in ("/healthz", "/readyz"):
                t0 = time.perf_counter()
                r = client.get(path)
                worst = max(worst, time.perf_counter() - t0)
                assert r.status_code == 200, (path, r.text)
                probes += 1
            time.sleep(0.05)
        worker.join()
    assert done == [200]
    assert probes >= 4
    assert worst < 0.2, f"a probe took {worst * 1000:.0f} ms"
