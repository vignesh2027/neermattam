"""Spoken depth words in the major Indian languages, and transcription replayed
from recorded Baseten responses (macOS voices read the notes; see test_live.py)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from neermattam_perception.muse_open import MuseSparkOpenAdapter
from neermattam_perception.voice import (
    LANDMARKS,
    LANGUAGES,
    PHRASES,
    depth_from_words,
    match_landmarks,
    normalise_languages,
    spoken_number_cm,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    ("words", "landmark", "lang"),
    [
        ("घुटनों तक पानी", "knee", "hi"),
        ("mutti alavu thanni", "knee", "ta"),
        ("முழங்கால் வரை", "knee", "ta"),
        ("నడుము వరకు", "waist", "te"),
        ("ಮೊಣಕಾಲು ವರೆಗೆ", "knee", "kn"),
        ("മുട്ടോളം വെള്ളം", "knee", "ml"),
        ("কোমর পর্যন্ত জল", "waist", "bn"),
        ("गुडघ्यापर्यंत पाणी", "knee", "mr"),
        ("ઘૂંટણ સુધી પાણી", "knee", "gu"),
        ("ਗੋਡਿਆਂ ਤੱਕ ਪਾਣੀ", "knee", "pa"),
        ("ଆଣ୍ଠୁ ପର୍ଯ୍ୟନ୍ତ ପାଣି", "knee", "or"),
        ("گھٹنوں تک پانی", "knee", "ur"),
        ("ankle deep", "ankle", "en"),
        ("टखने तक", "ankle", "hi"),
        ("छाती तक पानी", "chest", "hi"),
        ("kazhuthu varai", "neck", "ta"),
    ],
)
def test_phrase_table_covers_each_language(words: str, landmark: str, lang: str) -> None:
    assert (landmark, lang) in match_landmarks(words)


def test_every_language_has_knee_and_waist_and_ranges_rise() -> None:
    for lang in LANGUAGES:
        assert lang in PHRASES["knee"] and lang in PHRASES["waist"], lang
    order = [LANDMARKS[k].centre_cm for k in LANDMARKS]
    assert order == sorted(order)
    assert LANDMARKS["knee"].centre_cm == 45.0  # the gateway's knee depth


def test_depth_from_words_order_number_then_table_then_model() -> None:
    num = depth_from_words("dho foot", "two feet", 45.0)
    assert num is not None and num.depth_cm == pytest.approx(61.0, abs=0.1)
    assert num.basis == "spoken number" and num.sigma_cm == pytest.approx(18.3, abs=0.1)
    knee = depth_from_words("घुटनों तक", "knee deep", 80.0)
    assert knee is not None and knee.depth_cm == 45.0 and knee.sigma_cm == 13.5
    assert knee.basis == "phrase table: knee (en, hi)"
    span = depth_from_words("between knee and waist", "between knee and waist")
    assert span is not None and (span.low_cm, span.high_cm) == (37.0, 107.0)
    model = depth_from_words("konjam thanni", "a little water", 8.0)
    assert model is not None and model.basis.startswith("model estimate")
    assert model.sigma_cm == 10.0
    assert depth_from_words("", "", None) is None


def test_spoken_numbers() -> None:
    assert spoken_number_cm("30 cm") == 30.0
    assert spoken_number_cm("1.5 feet") == pytest.approx(45.72)
    assert spoken_number_cm("one metre") == 100.0
    assert spoken_number_cm("knee deep") is None
    assert spoken_number_cm("900 cm") is None


def test_language_codes_are_normalised() -> None:
    assert normalise_languages("ta+en") == ["ta", "en"]
    assert normalise_languages(["hi", "EN", "xx"]) == ["hi", "en"]
    assert normalise_languages("te, en") == ["te", "en"]
    assert normalise_languages(None) == []


def _replay(name: str) -> MuseSparkOpenAdapter:
    rec = json.loads((FIXTURES / name).read_text(encoding="utf-8"))

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=rec)

    async def no_sleep(_: float) -> None:
        return None

    return MuseSparkOpenAdapter(api_key="k", transport=httpx.MockTransport(handler), sleep=no_sleep)


@pytest.mark.parametrize(
    ("lang", "language", "place", "depth", "landmark"),
    [
        ("hi", "hi+en", "Velachery Main Road", 45.0, "knee"),
        ("te", "te+en", "Velachery Main Road", 95.0, "waist"),
        ("kn", "kn", "Agyar Bridge", 45.0, "knee"),
        # The Bengali note was transcribed in Devanagari and labelled Hindi, and
        # "T Nagar" was misheard: recorded as it happened. The depth word still
        # maps, through the Hindi row of the table.
        ("bn", "hi", "Ati Nagar", 95.0, "waist"),
    ],
)
def test_recorded_transcriptions(
    lang: str, language: str, place: str, depth: float, landmark: str
) -> None:
    t = asyncio.run(_replay(f"baseten_transcribe_{lang}.json").transcribe(b"RIFF", "audio/wav"))
    assert t.language == language and t.languages == tuple(language.split("+"))
    assert t.place_text == place
    assert t.depth_cm == depth and t.depth_basis.startswith(f"phrase table: {landmark}")
    assert t.depth_words and t.depth_words_en
    assert t.depth_low_cm is not None and t.depth_high_cm is not None


# Written sentences a reporter might say, three per language, each naming one
# body landmark in its own inflected form (and a few romanised). This checks
# the depth parser only: it does not test recognising speech, which depends on
# the voice model. Each sentence must give exactly its landmark and nothing else.
SPOKEN_PHRASES: list[tuple[str, str, str]] = [
    ("hi", "सड़क पर घुटनों तक पानी है", "knee"),
    ("hi", "यहाँ कमर तक पानी भरा है", "waist"),
    ("hi", "ghutne tak paani hai", "knee"),
    ("ta", "முழங்கால் அளவு தண்ணீர் இருக்கு", "knee"),
    ("ta", "இடுப்பு வரை தண்ணீர் நிக்குது", "waist"),
    ("ta", "கணுக்கால் வரை தண்ணீர்", "ankle"),
    ("te", "మోకాలి లోతు నీళ్ళు ఉన్నాయి", "knee"),
    ("te", "నడుము లోతు నీళ్ళు", "waist"),
    ("te", "చీలమండ వరకు నీరు", "ankle"),
    ("kn", "ಮೊಣಕಾಲಿನವರೆಗೆ ನೀರು ಇದೆ", "knee"),
    ("kn", "ಸೊಂಟದವರೆಗೆ ನೀರು ನಿಂತಿದೆ", "waist"),
    ("kn", "ಎದೆಯವರೆಗೆ ನೀರು", "chest"),
    ("ml", "മുട്ടോളം വെള്ളം ഉണ്ട്", "knee"),
    ("ml", "അരക്കെട്ടോളം വെള്ളം", "waist"),
    ("ml", "കഴുത്തോളം വെള്ളം", "neck"),
    ("bn", "রাস্তায় হাঁটু পর্যন্ত জল জমেছে", "knee"),
    ("bn", "কোমর সমান জল", "waist"),
    ("bn", "বুক পর্যন্ত জল", "chest"),
    ("mr", "रस्त्यावर गुडघ्यापर्यंत पाणी साचले आहे", "knee"),
    ("mr", "कंबरेपर्यंत पाणी आहे", "waist"),
    ("mr", "मांडीपर्यंत पाणी", "thigh"),
    ("gu", "રસ્તા પર ઘૂંટણ સુધી પાણી ભરાયું છે", "knee"),
    ("gu", "કમર સુધી પાણી છે", "waist"),
    ("gu", "ઘૂંટી સુધી પાણી", "ankle"),
    ("pa", "ਸੜਕ ਤੇ ਗੋਡਿਆਂ ਤੱਕ ਪਾਣੀ ਹੈ", "knee"),
    ("pa", "ਲੱਕ ਤੱਕ ਪਾਣੀ", "waist"),
    ("pa", "ਛਾਤੀ ਤੱਕ ਪਾਣੀ ਹੈ", "chest"),
    ("or", "ରାସ୍ତାରେ ଆଣ୍ଠୁ ପର୍ଯ୍ୟନ୍ତ ପାଣି", "knee"),
    ("or", "ଅଣ୍ଟା ପର୍ଯ୍ୟନ୍ତ ପାଣି", "waist"),
    ("or", "ଛାତି ପର୍ଯ୍ୟନ୍ତ ପାଣି", "chest"),
    ("ur", "سڑک پر گھٹنوں تک پانی ہے", "knee"),
    ("ur", "کمر تک پانی ہے", "waist"),
    ("ur", "سینے تک پانی", "chest"),
    ("en", "the water is knee deep on the main road", "knee"),
    ("en", "waist-deep near the bus stop", "waist"),
    ("en", "ankle deep water outside the station", "ankle"),
]


def test_spoken_phrases_cover_twelve_languages() -> None:
    assert {lang for lang, _, _ in SPOKEN_PHRASES} == set(LANGUAGES)
    assert len(SPOKEN_PHRASES) == 36  # the count quoted in docs and on the site


@pytest.mark.parametrize(("lang", "words", "landmark"), SPOKEN_PHRASES)
def test_spoken_phrase_gives_its_depth(lang: str, words: str, landmark: str) -> None:
    assert {name for name, _ in match_landmarks(words)} == {landmark}
    got = depth_from_words(words, "")
    assert got is not None and got.basis.startswith(f"phrase table: {landmark} (")
    assert got.depth_cm == LANDMARKS[landmark].centre_cm
