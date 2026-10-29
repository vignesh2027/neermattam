"""Live checks against Baseten and the local SAM 2.1 weights.

Skipped unless ``NEERMATTAM_LIVE=1``. They spend a few cents of Baseten credit
(two Muse Spark Open calls) and need the ``local`` extra for SAM 2.1:

    NEERMATTAM_LIVE=1 uv run --extra local pytest tests/test_live.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from neermattam_perception.media import image_from_rgb, load_image
from neermattam_perception.muse_open import MuseSparkOpenAdapter
from neermattam_perception.sam import ObjectPrompt, PointPrompt

LIVE = os.environ.get("NEERMATTAM_LIVE") == "1"
pytestmark = pytest.mark.skipif(not LIVE, reason="live test: set NEERMATTAM_LIVE=1")
PHOTOS = Path(__file__).resolve().parents[2] / "eval" / "photos"


def test_live_scene_reading_on_a_commons_photo() -> None:
    photo = PHOTOS / "c3.jpg"
    if not photo.exists():
        pytest.skip("run eval/real_photos.py once to download the photos")
    muse = MuseSparkOpenAdapter.from_env()
    reading = asyncio.run(muse.read_scene(load_image(photo.read_bytes())))
    assert reading.water_visible
    assert reading.references, "people wading should be listed"
    assert reading.overall_depth_cm is not None and reading.cost_usd > 0


def test_live_tamil_voice_note() -> None:
    if shutil.which("say") is None or shutil.which("afconvert") is None:
        pytest.skip("needs macOS say and afconvert to make the voice note")
    tmp = Path(os.environ.get("TMPDIR", "/tmp")) / "neermattam_live_ta"
    tmp.mkdir(exist_ok=True)
    text = "வேளச்சேரி மெயின் ரோட்டில் முட்டி அளவு தண்ணீர் இருக்கு. bike போக முடியாது."
    subprocess.run(["say", "-v", "Vani", "-o", str(tmp / "ta.aiff"), text], check=True)
    subprocess.run(
        ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", str(tmp / "ta.aiff"), str(tmp / "ta.wav")],
        check=True,
    )
    t = asyncio.run(
        MuseSparkOpenAdapter.from_env().transcribe((tmp / "ta.wav").read_bytes(), "audio/wav")
    )
    assert "முட்டி" in t.text or "knee" in t.depth_words.lower()
    assert t.depth_cm is not None and 25 <= t.depth_cm <= 70
    assert t.depth_sigma_cm is not None and t.depth_sigma_cm >= 10


def test_live_sam21_loads_once_and_reuses_the_embedding() -> None:
    pytest.importorskip("torch")
    from neermattam_perception.sam_local import Sam21LocalAdapter

    sam = Sam21LocalAdapter()
    sam.load()
    rgb = np.zeros((480, 640, 3), dtype=np.uint8)
    rgb[100:300, 200:400] = (200, 40, 40)  # one red square
    img = image_from_rgb(rgb)
    big = ObjectPrompt(1, (PointPrompt(300, 200),), (180, 80, 420, 320))
    r1 = asyncio.run(sam.segment(img, [big]))
    assert not sam.last_embed_cached
    r2 = asyncio.run(sam.segment(img, [big]))
    assert sam.last_embed_cached
    m = r1.masks[1].mask
    assert np.array_equal(m, r2.masks[1].mask)
    inter = m[100:300, 200:400].sum()
    assert inter / m.sum() > 0.8 and inter / (200 * 200) > 0.8


VOICE_NOTES = {
    # language: (macOS voice, what it says, landmark it should map to)
    "hi": ("Lekha", "वेलाचेरी मेन रोड पर घुटनों तक पानी भरा है, बाइक नहीं जा सकती।", "knee"),
    "te": ("Geeta", "వేలచేరి మెయిన్ రోడ్ లో నడుము వరకు నీళ్ళు ఉన్నాయి.", "waist"),
}


@pytest.mark.parametrize("lang", sorted(VOICE_NOTES))
def test_live_voice_note_in_other_languages(lang: str, tmp_path: Path) -> None:
    voice, text, landmark = VOICE_NOTES[lang]
    if shutil.which("say") is None or shutil.which("afconvert") is None:
        pytest.skip("needs macOS say and afconvert")
    voices = subprocess.run(["say", "-v", "?"], capture_output=True, text=True).stdout
    if not any(line.split()[:1] == [voice] for line in voices.splitlines()):
        pytest.skip(f"the macOS voice {voice} is not installed")
    subprocess.run(["say", "-v", voice, "-o", str(tmp_path / "v.aiff"), text], check=True)
    subprocess.run(
        [
            "afconvert",
            "-f",
            "WAVE",
            "-d",
            "LEI16@16000",
            str(tmp_path / "v.aiff"),
            str(tmp_path / "v.wav"),
        ],
        check=True,
    )
    t = asyncio.run(
        MuseSparkOpenAdapter.from_env().transcribe((tmp_path / "v.wav").read_bytes(), "audio/wav")
    )
    assert t.languages and t.languages[0] == lang
    assert "Velachery" in t.place_text
    assert t.depth_basis.startswith(f"phrase table: {landmark}")
