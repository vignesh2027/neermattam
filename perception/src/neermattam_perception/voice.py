"""Spoken depth words in the major Indian languages, turned into a depth with a range.

People report flood depth against their own body: ankle deep, knee deep,
waist deep. The table below maps those landmarks to a depth range for an adult
standing in the water, and lists the words for each landmark in Hindi, Tamil,
Telugu, Kannada, Malayalam, Bengali, Marathi, Gujarati, Punjabi, Odia, Urdu and
English (native script, plus common romanisations of the longer words).

The ranges are assumptions about an average adult, not measurements. They match
the landmark depths the gateway uses (``gateway/internal/report/voice.go``):
the centre is the gateway's depth and the range is that depth plus or minus its
sigma. A spoken number with a unit ("two feet", "30 cm") takes precedence;
when nothing matches, the model's own reading is used and labelled as such.

Matching works on whole words, by prefix, so inflected forms match (Hindi
"घुटने" and "घुटनों" both start with "घुटन"). Words that are short and common
in other meanings are deliberately left out; the model's English gloss
(``depth_words_en``) covers them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

LANGUAGES: dict[str, str] = {
    "hi": "Hindi",
    "ta": "Tamil",
    "te": "Telugu",
    "kn": "Kannada",
    "ml": "Malayalam",
    "bn": "Bengali",
    "mr": "Marathi",
    "gu": "Gujarati",
    "pa": "Punjabi",
    "or": "Odia",
    "ur": "Urdu",
    "en": "English",
}


@dataclass(frozen=True, slots=True)
class Landmark:
    name: str
    low_cm: float
    high_cm: float

    @property
    def centre_cm(self) -> float:
        return (self.low_cm + self.high_cm) / 2.0


# Centre and half-width as in gateway/internal/report/voice.go (depth, sigma).
LANDMARKS: dict[str, Landmark] = {
    "ankle": Landmark("ankle", 5.0, 15.0),
    "shin": Landmark("shin", 17.0, 33.0),
    "knee": Landmark("knee", 37.0, 53.0),
    "thigh": Landmark("thigh", 55.0, 75.0),
    "waist": Landmark("waist", 83.0, 107.0),
    "chest": Landmark("chest", 110.0, 140.0),
    "neck": Landmark("neck", 125.0, 155.0),
}

# landmark: language: word prefixes (lower case for Latin script).
PHRASES: dict[str, dict[str, tuple[str, ...]]] = {
    "ankle": {
        "en": ("ankle",),
        "hi": ("टखन", "takhn"),
        "ta": ("கணுக்கால்", "kanukkal"),
        "te": ("చీలమండ", "cheelamanda"),
        "ml": ("കണങ്കാൽ", "kanankal"),
        "bn": ("গোড়ালি", "gorali"),
        "mr": ("घोट्या", "घोटा"),
        "gu": ("ઘૂંટી", "ghunti"),
        "pa": ("ਗਿੱਟ", "gitte"),
        "or": ("ଗୋଇଠି",),
        "ur": ("ٹخن", "takhn"),
    },
    "shin": {
        "en": ("shin", "calf", "calves"),
        "hi": ("पिंडली", "pindli"),
        "ta": ("கெண்டைக்கால்",),
        "mr": ("पोटरी",),
        "ur": ("پنڈلی",),
    },
    "knee": {
        "en": ("knee",),
        "hi": ("घुटन", "ghutn", "ghutne"),
        "ta": ("முட்டி", "முழங்கால்", "mutti", "muzhangal"),
        "te": ("మోకాల", "mokal"),
        "kn": ("ಮೊಣಕಾಲ", "monakal"),
        "ml": ("മുട്ട", "muttu"),
        "bn": ("হাঁটু", "hantu"),
        "mr": ("गुडघ", "gudgh"),
        "gu": ("ઘૂંટણ", "ghutan"),
        "pa": ("ਗੋਡ", "gode"),
        "or": ("ଆଣ୍ଠୁ", "anthu"),
        "ur": ("گھٹن", "ghutn"),
    },
    "thigh": {
        "en": ("thigh",),
        "hi": ("जांघ", "जाँघ", "jangh"),
        "ta": ("தொடை", "thodai"),
        "te": ("తొడ",),
        "kn": ("ತೊಡೆ",),
        "ml": ("തുട",),
        "bn": ("উরু",),
        "mr": ("मांडी", "mandi"),
        "gu": ("જાંઘ",),
        "pa": ("ਪੱਟ",),
        "or": ("ଜଙ୍ଘ",),
        "ur": ("ران",),
    },
    "waist": {
        "en": ("waist", "hip"),
        "hi": ("कमर", "kamar"),
        "ta": ("இடுப்பு", "idupp", "idupu"),
        "te": ("నడుము", "nadumu"),
        "kn": ("ಸೊಂಟ", "sonta"),
        "ml": ("അരക്കെട്ട", "arakett"),
        "bn": ("কোমর", "komor"),
        "mr": ("कंबर", "kambar"),
        "gu": ("કમર",),
        "pa": ("ਲੱਕ",),
        "or": ("ଅଣ୍ଟା",),
        "ur": ("کمر", "kamar"),
    },
    "chest": {
        "en": ("chest",),
        "hi": ("छाती", "chhati", "seene", "सीने", "सीना"),
        "ta": ("நெஞ்சு", "nenju"),
        "te": ("ఛాతీ", "chaati", "ఛాతి"),
        "kn": ("ಎದೆ",),
        "ml": ("നെഞ്ച", "nenju"),
        "bn": ("বুক",),
        "mr": ("छाती",),
        "gu": ("છાતી",),
        "pa": ("ਛਾਤੀ",),
        "or": ("ଛାତି",),
        "ur": ("سینے", "سینہ", "چھاتی"),
    },
    "neck": {
        "en": ("neck", "shoulder"),
        "hi": ("गर्दन", "gardan"),
        "ta": ("கழுத்து", "kazhuthu"),
        "te": ("మెడ",),
        "kn": ("ಕುತ್ತಿಗೆ", "kuttige"),
        "ml": ("കഴുത്ത", "kazhuth"),
        "bn": ("গলা",),
        "gu": ("ગરદન",),
        "pa": ("ਗਰਦਨ",),
        "or": ("ବେକ",),
        "ur": ("گردن", "gardan"),
    },
}

_TOKEN = re.compile(r"[^\s,.;:!?()\[\]\"'/\-]+")
_NUMBER = re.compile(
    r"(\d+(?:\.\d+)?)\s*(cm|centimet\w*|inch\w*|in\b|feet|foot|ft|metre\w*|meter\w*|m\b)"
)
_WORD_NUMBERS = {
    "one": 1.0,
    "half": 0.5,
    "two": 2.0,
    "three": 3.0,
    "four": 4.0,
    "five": 5.0,
}


@dataclass(frozen=True, slots=True)
class DepthReading:
    depth_cm: float
    sigma_cm: float
    low_cm: float
    high_cm: float
    basis: str


def _sigma(depth: float, low: float, high: float) -> float:
    return max(10.0, 0.3 * depth, (high - low) / 2.0)


def match_landmarks(*texts: str) -> list[tuple[str, str]]:
    """Every (landmark, language) whose word starts a token in any of ``texts``."""
    found: list[tuple[str, str]] = []
    for text in texts:
        tokens = [t.lower() for t in _TOKEN.findall(text or "")]
        for name, by_lang in PHRASES.items():
            for lang, prefixes in by_lang.items():
                hit = any(tok.startswith(p) for tok in tokens for p in prefixes)
                if hit and (name, lang) not in found:
                    found.append((name, lang))
    return found


def spoken_number_cm(text: str) -> float | None:
    """A depth spoken as a number with a unit, in centimetres ("2 feet", "two feet")."""
    t = (text or "").lower()
    for word, value in _WORD_NUMBERS.items():
        t = re.sub(rf"\b{word}\b", str(value), t)
    m = _NUMBER.search(t)
    if m is None:
        return None
    v = float(m.group(1))
    unit = m.group(2)
    if unit.startswith(("in",)):
        v *= 2.54
    elif unit in ("feet", "foot", "ft"):
        v *= 30.48
    elif unit.startswith(("metre", "meter")) or unit == "m":
        v *= 100.0
    return v if 0 < v <= 300 else None


def depth_from_words(
    depth_words: str,
    depth_words_en: str,
    model_depth_cm: float | None = None,
    model_low_cm: float | None = None,
    model_high_cm: float | None = None,
) -> DepthReading | None:
    """Depth from spoken words: a number with a unit, then the phrase table, then the model."""
    num = spoken_number_cm(depth_words_en) or spoken_number_cm(depth_words)
    if num is not None:
        lo, hi = 0.7 * num, 1.3 * num
        return DepthReading(round(num, 1), round(_sigma(num, lo, hi), 1), lo, hi, "spoken number")
    hits = match_landmarks(depth_words_en, depth_words)
    if hits:
        marks = [LANDMARKS[name] for name, _ in hits]
        lo = min(m.low_cm for m in marks)
        hi = max(m.high_cm for m in marks)
        centre = (lo + hi) / 2.0
        names = sorted({name for name, _ in hits}, key=list(LANDMARKS).index)
        langs = sorted({lang for _, lang in hits})
        basis = f"phrase table: {' to '.join(names)} ({', '.join(langs)})"
        return DepthReading(round(centre, 1), round(_sigma(centre, lo, hi), 1), lo, hi, basis)
    if model_depth_cm is not None and model_depth_cm >= 0:
        lo = model_low_cm if model_low_cm is not None else 0.7 * model_depth_cm
        hi = model_high_cm if model_high_cm is not None else 1.3 * model_depth_cm
        lo, hi = min(lo, hi), max(lo, hi)
        return DepthReading(
            round(model_depth_cm, 1),
            round(_sigma(model_depth_cm, lo, hi), 1),
            lo,
            hi,
            "model estimate (no table phrase matched)",
        )
    return None


def normalise_languages(raw: object) -> list[str]:
    """Language codes from the model's answer, main language first, known codes only."""
    if isinstance(raw, list):
        parts = [str(x) for x in raw]
    else:
        parts = re.split(r"[+,/\s]+", str(raw or ""))
    out: list[str] = []
    for p in parts:
        code = p.strip().lower()[:2]
        if code in LANGUAGES and code not in out:
            out.append(code)
    return out
