"""Reference objects of known size: tyre codes, vehicle wheels, kerbs.

Tyre outer diameters come from the size code:

* metric codes ``W/A-R`` or ``W/A R R`` (for example ``90/100-10`` or
  ``165/80 R14``): ``D = R * 25.4 + 2 * W * A / 100`` millimetres;
* inch codes ``W-R`` (for example ``2.75-18`` or ``4.00-8``): the section is
  taken as square, so ``D = (R + 2 * W) * 25.4`` millimetres.

Inch-series bias tyres run a few percent larger than the inch formula
(maker sheets put a 4.00-8 at 423 to 429 mm against 406 mm, and a 10.00-20 at
1064 to 1070 mm against 1016 mm). Where a maker sheet gives the measured
diameter, the catalogue uses it and says so.

Every entry records where its tyre code came from and whether the value was
seen on that page (``verified``) or only on a secondary source.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum


class RefKind(StrEnum):
    TWO_WHEELER_WHEEL = "two_wheeler_wheel"
    CAR_WHEEL = "car_wheel"
    AUTO_WHEEL = "auto_wheel"
    BUS_WHEEL = "bus_wheel"
    KERB = "kerb"
    PERSON = "person"


WHEEL_KINDS = frozenset(
    {RefKind.TWO_WHEELER_WHEEL, RefKind.CAR_WHEEL, RefKind.AUTO_WHEEL, RefKind.BUS_WHEEL}
)

# --------------------------------------------------------------------------- tyre codes

_METRIC = re.compile(
    r"^\s*(\d{2,3})\s*/\s*(\d{2,3})\s*(?:-|Z?R|B|D)\s*(\d{1,2}(?:\.\d)?)\b", re.IGNORECASE
)
_INCH = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,2})?)\s*(?:-|R)\s*(\d{1,2}(?:\.\d)?)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class TyreCode:
    code: str
    width_mm: float
    aspect_pct: float
    rim_in: float
    system: str  # "metric" or "inch"

    @property
    def diameter_mm(self) -> float:
        return self.rim_in * 25.4 + 2.0 * self.width_mm * self.aspect_pct / 100.0

    @property
    def diameter_cm(self) -> float:
        return self.diameter_mm / 10.0


def parse_tyre_code(code: str) -> TyreCode:
    """Parse a tyre size code such as ``90/100-10``, ``165/80 R14`` or ``2.75-18``."""
    m = _METRIC.match(code)
    if m:
        return TyreCode(code, float(m.group(1)), float(m.group(2)), float(m.group(3)), "metric")
    m = _INCH.match(code)
    if m:
        width_in = float(m.group(1))
        return TyreCode(code, width_in * 25.4, 100.0, float(m.group(2)), "inch")
    raise ValueError(f"unrecognised tyre size code: {code!r}")


def tyre_diameter_cm(code: str) -> float:
    return parse_tyre_code(code).diameter_cm


# --------------------------------------------------------------------------- catalogue

# Per-tyre uncertainty for a known size. It covers tread wear (a worn tread
# loses several millimetres of radius), inflation and manufacturing spread.
# These are engineering assumptions, not measured values.
SIGMA_TWO_WHEELER_CM = 1.0
SIGMA_CAR_CM = 1.5
SIGMA_HEAVY_CM = 2.0


@dataclass(frozen=True, slots=True)
class WheelEntry:
    vehicle: str  # catalogue key, such as "honda_activa_110"
    label: str
    kind: RefKind
    position: str  # "front", "rear" or "any"
    tyre_code: str
    diameter_cm: float
    sigma_cm: float
    source_url: str
    verified: bool
    note: str = ""
    # Unloaded radius minus static loaded radius. The fitted circle follows the
    # unloaded outline, so it reaches below the road by this much and the depth
    # reads high by it. Zero where no verified figure exists.
    squash_cm: float = 0.0

    @property
    def source(self) -> str:
        return f"catalogue:{self.tyre_code}"


def _w(
    vehicle: str,
    label: str,
    kind: RefKind,
    position: str,
    code: str,
    url: str,
    verified: bool,
    *,
    measured_cm: float | None = None,
    note: str = "",
    squash_cm: float = 0.0,
) -> WheelEntry:
    sigma = {
        RefKind.TWO_WHEELER_WHEEL: SIGMA_TWO_WHEELER_CM,
        RefKind.CAR_WHEEL: SIGMA_CAR_CM,
    }.get(kind, SIGMA_HEAVY_CM)
    d = measured_cm if measured_cm is not None else tyre_diameter_cm(code)
    return WheelEntry(
        vehicle, label, kind, position, code, round(d, 1), sigma, url, verified, note, squash_cm
    )


_HONDA_110 = "https://honda2wheelersindia.com/activa110-OBD2B/specifications"
_HONDA_125 = "https://www.autocarindia.com/bikes/honda-bikes/activa-125/specifications"
_TVS = "https://www.tvsmotor.com/tvs-jupiter/jupiter-disc-smartxonnect"
_HERO = "https://www.heromotocorp.com/en-in/motorcycles/splendor-plus.html"
_PULSAR = "https://www.bajajauto.com/bikes/pulsar/pulsar-150/specifications"
_RE = "https://www.royalenfield.com/in/en/motorcycles/classic-350/"
_SWIFT = (
    "https://www.marutisuzuki.com/content/dam/msil/arena/in/en/assets/cars/swift/brochures/"
    "Swift%20Brochure%20-%20Vertical%20-%205th%20Oct.pdf"
)
_I20 = "https://www.hyundai.com/content/dam/hyundai/in/en/data/brochure/i20.pdf"
_WAGONR = "https://www.autocarindia.com/cars/maruti-suzuki/wagon-r/lxi-10"
_RE_AUTO = (
    "https://www.bajajauto.com/en-kh/-/media/globalbajajauto/common-media/brochure/3wheelers/"
    "bajaj-re-4s-lpg-with-door-brochure.pdf"
)
_RE_AUTO_OD = "https://www.tyreplex.com/mrf-tyres/automiler/sw-115-rs-8"
_BUS = (
    "https://www.trucksbuses.com/buses/fully-built-buses/inter-city/"
    "ashok-leyland-freedom-12m-fe-177-hp/specifications"
)
_BUS_OD = "https://admin.bigtyres.co.uk/media/landingpages/documents/double_coin_rlb800.pdf"

TWO = RefKind.TWO_WHEELER_WHEEL
CAR = RefKind.CAR_WHEEL

CATALOGUE: tuple[WheelEntry, ...] = (
    _w("honda_activa_110", "Honda Activa 110", TWO, "front", "90/90-12", _HONDA_110, True),
    _w("honda_activa_110", "Honda Activa 110", TWO, "rear", "90/100-10", _HONDA_110, True),
    _w(
        "honda_activa_125",
        "Honda Activa 125",
        TWO,
        "front",
        "90/90-12",
        _HONDA_125,
        False,
        note="secondary source (Autocar India)",
    ),
    _w(
        "honda_activa_125",
        "Honda Activa 125",
        TWO,
        "rear",
        "90/100-10",
        _HONDA_125,
        False,
        note="secondary source (Autocar India)",
    ),
    _w("tvs_jupiter", "TVS Jupiter 110", TWO, "front", "90/90-12", _TVS, True),
    _w("tvs_jupiter", "TVS Jupiter 110", TWO, "rear", "90/90-12", _TVS, True),
    _w("hero_splendor_plus", "Hero Splendor Plus", TWO, "front", "80/100-18", _HERO, True),
    _w("hero_splendor_plus", "Hero Splendor Plus", TWO, "rear", "80/100-18", _HERO, True),
    _w(
        "bajaj_pulsar_150",
        "Bajaj Pulsar 150 (single disc)",
        TWO,
        "front",
        "80/100-17",
        _PULSAR,
        True,
    ),
    _w(
        "bajaj_pulsar_150",
        "Bajaj Pulsar 150 (single disc)",
        TWO,
        "rear",
        "100/90-17",
        _PULSAR,
        True,
    ),
    _w(
        "bajaj_pulsar_150_td",
        "Bajaj Pulsar 150 (twin disc)",
        TWO,
        "front",
        "90/90-17",
        _PULSAR,
        True,
    ),
    _w(
        "bajaj_pulsar_150_td",
        "Bajaj Pulsar 150 (twin disc)",
        TWO,
        "rear",
        "120/80-17",
        _PULSAR,
        True,
    ),
    _w("re_classic_350", "Royal Enfield Classic 350", TWO, "front", "100/90-19", _RE, True),
    _w("re_classic_350", "Royal Enfield Classic 350", TWO, "rear", "120/80-18", _RE, True),
    _w("maruti_swift", "Maruti Suzuki Swift (LXi, VXi)", CAR, "any", "165/80 R14", _SWIFT, True),
    _w("maruti_swift", "Maruti Suzuki Swift (ZXi, ZXi+)", CAR, "any", "185/65 R15", _SWIFT, True),
    _w("hyundai_i20", "Hyundai i20 (Era)", CAR, "any", "185/70 R14", _I20, True),
    _w("hyundai_i20", "Hyundai i20 (Magna)", CAR, "any", "185/65 R15", _I20, True),
    _w("hyundai_i20", "Hyundai i20 (Sportz, Asta)", CAR, "any", "195/55 R16", _I20, True),
    _w(
        "maruti_wagonr",
        "Maruti Suzuki WagonR (LXi 1.0)",
        CAR,
        "any",
        "155/80 R13",
        _WAGONR,
        False,
        note="secondary source (Autocar India)",
    ),
    _w(
        "maruti_wagonr",
        "Maruti Suzuki WagonR (VXi 1.0, ZXi 1.2)",
        CAR,
        "any",
        "165/70 R14",
        _WAGONR,
        False,
        note="secondary source (Autocar India)",
    ),
    _w(
        "bajaj_re_auto",
        "Bajaj RE auto-rickshaw",
        RefKind.AUTO_WHEEL,
        "any",
        "4.00-8",
        _RE_AUTO,
        True,
        measured_cm=42.6,
        note=f"code from the Bajaj RE4S brochure; diameter measured by MRF ({_RE_AUTO_OD}), "
        "423 to 429 mm against 406 mm by formula; static loaded radius 192 to 195.5 mm",
        squash_cm=1.9,  # (426 / 2 - 193.75) mm from the same MRF listings
    ),
    _w(
        "city_bus",
        "City bus (Ashok Leyland 12 m class)",
        RefKind.BUS_WHEEL,
        "any",
        "10.00 R20",
        _BUS,
        False,
        measured_cm=106.4,
        note=f"code from a secondary spec page; diameter from a Double Coin sheet ({_BUS_OD}); "
        "static loaded radius about 500 mm on the same sheet; "
        "the Chennai MTC fleet mix is not verified",
        squash_cm=3.2,  # 1064 / 2 - 500 mm
    ),
)


@dataclass(frozen=True, slots=True)
class KerbEntry:
    height_cm: float
    sigma_cm: float
    source: str
    citation: str


# IRC:103-2012 cl. 6.1.2 caps the kerb at a step riser, 150 mm; IRC:86-1983 cl. 7.2
# shows barrier 200 mm, semi-barrier 150 mm and mountable 100 mm exposed heights.
KERB = KerbEntry(
    height_cm=15.0,
    sigma_cm=3.0,
    source="irc:103-2012",
    citation=(
        "IRC:103-2012 Guidelines for Pedestrian Facilities, cl. 6.1.2 and 6.2.1 "
        "(https://law.resource.org/pub/in/bis/irc/irc.gov.in.103.2012.pdf); "
        "IRC:86-1983 cl. 7.2 and Fig. 1 "
        "(https://law.resource.org/pub/in/bis/irc/irc.gov.in.086.1983.pdf)"
    ),
)


@dataclass(frozen=True, slots=True)
class ReferenceChoice:
    kind: RefKind
    diameter_cm: float
    diameter_sigma_cm: float
    source: str
    label: str = ""
    squash_cm: float = 0.0


def mixture(entries: Iterable[WheelEntry]) -> tuple[float, float]:
    """Mean and standard deviation of an equal-weight mixture of wheel entries."""
    items = list(entries)
    if not items:
        raise ValueError("empty mixture")
    w = 1.0 / len(items)
    mean = sum(w * e.diameter_cm for e in items)
    second = sum(w * (e.sigma_cm**2 + e.diameter_cm**2) for e in items)
    return mean, math.sqrt(max(0.0, second - mean * mean))


def entries_for(kind: RefKind) -> list[WheelEntry]:
    return [e for e in CATALOGUE if e.kind == kind]


def vehicles() -> dict[str, str]:
    """Catalogue vehicle keys and labels (used to constrain the scene model's answer)."""
    out: dict[str, str] = {}
    for e in CATALOGUE:
        out.setdefault(e.vehicle, e.label.split(" (")[0])
    return out


def resolve_reference(
    kind: RefKind | str,
    *,
    vehicle: str | None = None,
    position: str | None = None,
    hint_diameter_cm: float | None = None,
) -> ReferenceChoice:
    """Pick the diameter and its uncertainty for a reference wheel.

    Order of precedence: a reporter's ``hint_diameter_cm``; a catalogue vehicle
    (one wheel position, or a mixture over positions and variants); the generic
    prior for the kind, which is the mixture of every catalogue wheel of that
    kind and so carries a larger sigma.
    """
    kind = RefKind(kind)
    if hint_diameter_cm is not None:
        if not 10.0 <= hint_diameter_cm <= 200.0:
            raise ValueError("hint_diameter_cm must be between 10 and 200")
        return ReferenceChoice(
            kind, hint_diameter_cm, max(1.0, 0.02 * hint_diameter_cm), "hint", "reporter supplied"
        )
    if kind not in WHEEL_KINDS:
        raise ValueError(f"{kind} is not a wheel reference")
    if vehicle:
        found = [e for e in CATALOGUE if e.vehicle == vehicle]
        if found:
            if position in ("front", "rear"):
                exact = [e for e in found if e.position == position]
                found = exact or found
            codes = sorted({e.tyre_code for e in found})
            mean, sd = mixture(found)
            return ReferenceChoice(
                found[0].kind,
                round(mean, 1),
                round(sd, 2),
                "catalogue:" + "|".join(codes),
                found[0].label.split(" (")[0],
                sum(e.squash_cm for e in found) / len(found),
            )
    pool = entries_for(kind)
    mean, sd = mixture(pool)
    return ReferenceChoice(kind, round(mean, 1), round(sd, 2), f"prior:{kind.value}", "generic")
