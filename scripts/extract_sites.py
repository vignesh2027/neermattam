#!/usr/bin/env python3
"""Extract critical sites and fuel stations from OpenStreetMap for the engine.

Neermattam's "Critical sites" view starts from what OpenStreetMap already
records. Nothing in the output is typed by hand: every site is an OSM node,
way or relation with one of the tags below, inside one of the engine's six
metro boxes (docs/ENGINE.md, Cities).

    data_centre  telecom=data_center|datacenter|data_centre
                 building=data_center|datacenter|data_centre
                 man_made=data_center|datacenter
                 industrial=data_center|data_centre
    hospital     amenity=hospital, healthcare=hospital
    telephone_exchange    telecom=exchange
    cable_landing_station telecom=landing_station, man_made=cable_landing_station
                 (none exist in the October 2026 extracts; the filter is
                 there so they flow in once mapped)
    fuel         amenity=fuel (supply points for access routes, not sites)

The script needs the `osmium` command (osmium-tool) and the Geofabrik India
zone extracts. It writes engine/assets/sites-osm.json, which the engine
embeds, and prints the real counts per city.

    python3 scripts/extract_sites.py --osm-dir ~/neermattam-data/osm

Licence of the output: OpenStreetMap data, (c) OpenStreetMap contributors,
Open Database Licence 1.0. The attribution travels with the file.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path

# The engine's metro boxes (minLon, minLat, maxLon, maxLat) and the
# Geofabrik zone extract each city was cut from. Keep in step with
# engine/src/city.rs.
CITIES = {
    "chennai": ((80.10, 12.85, 80.33, 13.24), "southern"),
    "mumbai": ((72.77, 18.88, 73.13, 19.32), "western"),
    "bengaluru": ((77.44, 12.82, 77.80, 13.15), "southern"),
    "hyderabad": ((78.24, 17.22, 78.66, 17.62), "southern"),
    "kolkata": ((88.20, 22.42, 88.52, 22.72), "eastern"),
    "delhi": ((76.84, 28.40, 77.35, 28.89), "northern"),
    # Not a default engine city: covered only when the engine is started
    # with it (docs/SITES.md). The box is the bounding box of the
    # Visakhapatnam District boundary (admin_level 5) in OpenStreetMap.
    "visakhapatnam": ((83.11, 17.55, 83.53, 18.07), "southern"),
}

DC_VALUES = {"data_center", "datacenter", "data_centre"}

FILTERS = [
    "nwr/telecom=data_center,datacenter,data_centre",
    "nwr/building=data_center,datacenter,data_centre",
    "nwr/man_made=data_center,datacenter",
    "nwr/industrial=data_center,data_centre",
    "nwr/amenity=hospital,fuel",
    "nwr/healthcare=hospital",
    "nwr/telecom=exchange,landing_station",
    "nwr/man_made=cable_landing_station",
]


def kind_of(tags: dict[str, str]) -> tuple[str | None, str | None]:
    """The kind and the tag that made it, or (None, None)."""
    for key in ("telecom", "building", "man_made", "industrial"):
        if tags.get(key) in DC_VALUES:
            return "data_centre", f"{key}={tags[key]}"
    if tags.get("amenity") == "hospital":
        return "hospital", "amenity=hospital"
    if tags.get("healthcare") == "hospital":
        return "hospital", "healthcare=hospital"
    if tags.get("telecom") == "exchange":
        return "telephone_exchange", "telecom=exchange"
    if tags.get("telecom") == "landing_station":
        return "cable_landing_station", "telecom=landing_station"
    if tags.get("man_made") == "cable_landing_station":
        return "cable_landing_station", "man_made=cable_landing_station"
    if tags.get("amenity") == "fuel":
        return "fuel", "amenity=fuel"
    return None, None


def ring_centroid(ring: list[list[float]]) -> tuple[float, float, float]:
    """Area-weighted centroid (lon, lat) and area of a ring, planar in a
    local frame scaled by cos(latitude); fine for buildings."""
    lat0 = sum(p[1] for p in ring) / len(ring)
    k = math.cos(math.radians(lat0))
    # Work relative to the first vertex, so the cross products stay small
    # and keep their precision.
    ox, oy = ring[0]
    a = cx = cy = 0.0
    for (x0, y0), (x1, y1) in zip(ring, ring[1:]):
        x0, x1 = (x0 - ox) * k, (x1 - ox) * k
        y0, y1 = y0 - oy, y1 - oy
        cross = x0 * y1 - x1 * y0
        a += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if abs(a) < 1e-18:
        return (sum(p[0] for p in ring) / len(ring), lat0, 0.0)
    return (ox + cx / (3 * a) / k, oy + cy / (3 * a), abs(a) / 2)


def centre(geom: dict) -> tuple[float, float] | None:
    t = geom["type"]
    if t == "Point":
        lon, lat = geom["coordinates"]
        return lat, lon
    polys = geom["coordinates"] if t == "MultiPolygon" else [geom["coordinates"]] if t == "Polygon" else []
    best = None
    for poly in polys:
        lon, lat, area = ring_centroid(poly[0])
        if best is None or area > best[2]:
            best = (lon, lat, area)
    return (best[1], best[0]) if best else None


def inside(lat: float, lon: float, geom: dict) -> bool:
    """Point in the outer rings of a (multi)polygon."""
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]] if geom["type"] == "Polygon" else []
    for poly in polys:
        ring, hit = poly[0], False
        for (x0, y0), (x1, y1) in zip(ring, ring[1:]):
            if (y0 > lat) != (y1 > lat) and lon < x0 + (lat - y0) * (x1 - x0) / (y1 - y0):
                hit = not hit
        if hit:
            return True
    return False


def city_of(lat: float, lon: float) -> str | None:
    for cid, ((x0, y0, x1, y1), _) in CITIES.items():
        if x0 <= lon <= x1 and y0 <= lat <= y1:
            return cid
    return None


def near_a_box(lat: float, lon: float, zone: str, margin: float = 0.1) -> bool:
    """Within about 10 km of a covered city's box cut from this zone."""
    for (x0, y0, x1, y1), z in CITIES.values():
        if z == zone and x0 - margin <= lon <= x1 + margin and y0 - margin <= lat <= y1 + margin:
            return True
    return False


def run(cmd: list[str]) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def features(zone_pbf: Path, work: Path) -> list[dict]:
    filtered = work / (zone_pbf.stem + ".sites.osm.pbf")
    out = work / (zone_pbf.stem + ".sites.geojsonseq")
    run(["osmium", "tags-filter", "-O", str(zone_pbf), *FILTERS, "-o", str(filtered)])
    run(["osmium", "export", "-O", "-f", "geojsonseq", "--geometry-types=point,polygon",
         "--add-unique-id=type_id", str(filtered), "-o", str(out)])
    feats = []
    for line in out.read_text(encoding="utf-8").splitlines():
        line = line.strip().lstrip("\x1e")
        if line:
            feats.append(json.loads(line))
    return feats


OSM_TYPES = {"n": "node", "w": "way", "a": "area", "r": "relation"}


def osm_ref(fid: str) -> str:
    """osmium's type_id ids are n123, w123 and, for areas, a<2*id(+1)>."""
    t, n = fid[0], int(fid[1:])
    if t == "a":
        return f"way/{n // 2}" if n % 2 == 0 else f"relation/{n // 2}"
    return f"{OSM_TYPES[t]}/{n}"


def text(tags: dict, key: str) -> str | None:
    v = tags.get(key)
    return v.strip()[:120] if isinstance(v, str) and v.strip() else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--osm-dir", type=Path, default=Path.home() / "neermattam-data" / "osm")
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent.parent / "engine" / "assets" / "sites-osm.json")
    args = ap.parse_args()

    zones = sorted({z for _, z in CITIES.values()})
    sites: list[dict] = []
    fuel: list[dict] = []
    outside: list[dict] = []
    extracts = {}
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        for zone in zones:
            pbf = args.osm_dir / f"{zone}-zone-latest.osm.pbf"
            if not pbf.exists():
                print(f"missing {pbf}; download it from https://download.geofabrik.de/asia/india/", file=sys.stderr)
                return 2
            stamp = run(["osmium", "fileinfo", "-g", "header.option.osmosis_replication_timestamp", str(pbf)]).strip()
            extracts[zone] = {"file": pbf.name, "osm_data_until": stamp}
            feats = features(pbf, work)
            polys = [f for f in feats if f["geometry"]["type"] != "Point"]
            for f in feats:
                tags = f["properties"]
                kind, tag = kind_of(tags)
                if kind is None:
                    continue
                c = centre(f["geometry"])
                if c is None:
                    continue
                lat, lon = c
                city = city_of(lat, lon)
                if city is None and kind == "data_centre" and near_a_box(lat, lon, zone):
                    # Named in the report so the gap is visible, not silent.
                    outside.append({"name": text(tags, "name"), "osm": osm_ref(f["id"]), "lat": round(lat, 6), "lon": round(lon, 6)})
                if city is None or CITIES[city][1] != zone:
                    continue
                if f["geometry"]["type"] == "Point" and any(
                    kind_of(p["properties"])[0] == kind and inside(lat, lon, p["geometry"]) for p in polys
                ):
                    continue  # the same place is also mapped as an outline; keep the outline
                rec = {
                    "id": "osm:" + osm_ref(f["id"]).replace("/", ":"),
                    "lat": round(lat, 6),
                    "lon": round(lon, 6),
                    "city": city,
                    "name": text(tags, "name") or text(tags, "name:en"),
                }
                if kind == "fuel":
                    diesel = tags.get("fuel:diesel")
                    rec["brand"] = text(tags, "brand")
                    rec["diesel"] = True if diesel == "yes" else False if diesel == "no" else None
                    fuel.append(rec)
                else:
                    rec["kind"] = kind
                    rec["operator"] = text(tags, "operator")
                    rec["osm"] = osm_ref(f["id"])
                    rec["tag"] = tag
                    sites.append(rec)
                for k in [k for k, v in rec.items() if v is None]:
                    del rec[k]  # absent tags are left out, not written as null

    sites.sort(key=lambda s: (s["city"], s["kind"], s["id"]))
    fuel.sort(key=lambda s: (s["city"], s["id"]))
    kinds = ["data_centre", "hospital", "telephone_exchange", "cable_landing_station", "fuel"]
    counts = {c: {k: 0 for k in kinds} for c in CITIES}
    for s in sites:
        counts[s["city"]][s["kind"]] += 1
    for f in fuel:
        counts[f["city"]]["fuel"] += 1

    doc = {
        "format": "neermattam-sites-osm",
        "version": 1,
        "attribution": "(c) OpenStreetMap contributors, Open Database Licence 1.0 (https://www.openstreetmap.org/copyright)",
        "generated_by": "scripts/extract_sites.py",
        "generated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "extracts": extracts,
        "tags": {
            "data_centre": FILTERS[:4],
            "hospital": ["amenity=hospital", "healthcare=hospital"],
            "telephone_exchange": ["telecom=exchange"],
            "cable_landing_station": ["telecom=landing_station", "man_made=cable_landing_station"],
            "fuel": ["amenity=fuel"],
        },
        "counts": counts,
        "outside_boxes": outside,
        "sites": sites,
        "fuel": fuel,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"wrote {args.out} ({args.out.stat().st_size // 1024} KiB)")
    print(f"{'city':<14} {'data centres':>12} {'hospitals':>10} {'exchanges':>10} {'landing':>8} {'fuel':>6}")
    for c, n in counts.items():
        print(f"{c:<14} {n['data_centre']:>12} {n['hospital']:>10} {n['telephone_exchange']:>10} {n['cable_landing_station']:>8} {n['fuel']:>6}")
    for o in outside:
        print(f"data centre within 10 km of a box but outside it (not covered): {o['name'] or 'unnamed'} {o['osm']} at {o['lat']}, {o['lon']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
