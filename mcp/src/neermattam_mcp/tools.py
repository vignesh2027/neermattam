"""The tools, as plain async functions over a Gateway.

Rules every answer keeps (AGENTS.md, Safety):
- it carries the advisory wording and never calls a road, cell or site safe;
- a place with no evidence is "unknown", never safe or dry;
- every depth comes with its uncertainty;
- nothing is invented: every number is read from the gateway's answer.
"""

from __future__ import annotations

import math
import mimetypes
import os
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from .gateway import ADVISORY, Gateway, GatewayError

UNKNOWN_NOTE = "Unknown means nobody has reported this place recently. It is not safe and not dry."

VEHICLES = {
    "walking": "pedestrian", "walk": "pedestrian", "pedestrian": "pedestrian", "foot": "pedestrian",
    "two_wheeler": "two_wheeler", "twowheeler": "two_wheeler", "scooter": "two_wheeler", "bike": "two_wheeler", "motorcycle": "two_wheeler",
    "auto": "auto", "auto_rickshaw": "auto", "rickshaw": "auto",
    "car": "car",
    "bus": "bus",
}

SITE_KINDS = ("data_centre", "hospital", "telephone_exchange", "substation", "cable_landing_station")

METHOD_LABELS = {
    "wheel_geometry": "measured: geometry of a wheel of known size",
    "person_reference": "measured against a standing adult (wide uncertainty)",
    "model_estimate": "estimate: the scene model's visual estimate, not a measurement",
    "not_measured": "not measured",
}

ZONE_WORDS = {
    "over": "water over the threshold reported in this zone",
    "nearby": "water over the threshold reported in a larger ring, not the nearest one",
    "under": "the cells with recent reports are below the threshold; cells without reports are unknown, not safe",
    "unknown": "too few recent reports to say; unknown, not safe",
}

ROUTE_STATUS_WORDS = {
    "open": "a water-aware route exists on current reports (advisory, not a promise the road is clear)",
    "no_safe_route": "every route crosses water reported too deep for this vehicle; the least-bad one is given",
    "unavailable": "no route could be planned",
}

MAX_PHOTO_BYTES = 25 << 20


def _fail(e: GatewayError, what: str) -> ToolError:
    return ToolError(f"{what}: {e} {ADVISORY}")


def _check_lat_lon(lat: float, lon: float, name: str = "point") -> None:
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or math.isnan(lat) or math.isnan(lon):
        raise ToolError(f"The {name} must be a latitude between -90 and 90 and a longitude between -180 and 180, in that order.")


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _centroid(geom: dict[str, Any] | None) -> tuple[float, float] | None:
    try:
        ring = geom["coordinates"][0]  # type: ignore[index]
        pts = ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else ring
        lon = sum(p[0] for p in pts) / len(pts)
        lat = sum(p[1] for p in pts) / len(pts)
        return lat, lon
    except (KeyError, IndexError, TypeError, ZeroDivisionError):
        return None


def _age_minutes(ts: Any, now: datetime) -> float | None:
    if not isinstance(ts, str):
        return None
    try:
        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return round((now - t).total_seconds() / 60, 1)


async def _cities(gw: Gateway) -> list[dict[str, Any]]:
    try:
        ans = await gw.get("/api/cities")
    except GatewayError as e:
        raise _fail(e, "Could not read the covered cities") from e
    return list((ans.body or {}).get("cities") or [])


def _city_of(cities: list[dict[str, Any]], lat: float, lon: float) -> dict[str, Any] | None:
    for c in cities:
        b = c.get("bbox") or []
        if len(b) == 4 and b[0] <= lon <= b[2] and b[1] <= lat <= b[3]:
            return c
    return None


# ---------------------------------------------------------------- list_cities


async def list_cities(gw: Gateway) -> dict[str, Any]:
    try:
        ans = await gw.get("/api/india")
    except GatewayError as e:
        raise _fail(e, "Could not read the cities") from e
    body = ans.body or {}
    cities = []
    for c in body.get("cities") or []:
        cities.append({
            "id": c.get("id"),
            "name": c.get("name"),
            "centre": {"lat": c.get("lat"), "lon": c.get("lon")},
            "rain_mm_1h": c.get("rain_mm_1h"),
            "rain_mm_24h": c.get("rain_mm_24h"),
            "rain_read_at": c.get("rain_at"),
            "rain_source": c.get("rain_source"),
            "real_reports_24h": c.get("real_reports_24h"),
            "cells_with_evidence": c.get("cells_with_evidence"),
            "deepest_cm": c.get("deepest_cm"),
            "critical_sites": c.get("critical_sites"),
            "map_url": gw.url("/map?city=" + quote(str(c.get("id") or ""))),
        })
    return {
        "generated_at": body.get("generated_at"),
        "cities": cities,
        "notes": [
            "Rainfall is Open-Meteo's modelled precipitation at the city centre. null means it could not be read, never zero.",
            "real_reports_24h counts real reports fused in the last 24 hours. A city with none has an unknown street picture, not a dry one.",
        ],
        "advisory": ADVISORY,
    }


# ------------------------------------------------------------ water_depth_near


async def water_depth_near(gw: Gateway, lat: float, lon: float, radius_m: float = 500, now: datetime | None = None) -> dict[str, Any]:
    _check_lat_lon(lat, lon)
    if not 50 <= radius_m <= 5000:
        raise ToolError("radius_m must be between 50 and 5000 metres.")
    now = now or datetime.now(timezone.utc)
    city = _city_of(await _cities(gw), lat, lon)
    if city is None:
        raise ToolError(
            f"The point ({lat:.5f}, {lon:.5f}) is outside every city Neermattam covers, so there is no depth data for it. "
            f"Call list_cities for the covered cities. The order is latitude, then longitude. {ADVISORY}"
        )
    dlat = radius_m / 111_320
    dlon = radius_m / (111_320 * max(math.cos(math.radians(lat)), 0.01))
    bbox = f"{lon - dlon:.6f},{lat - dlat:.6f},{lon + dlon:.6f},{lat + dlat:.6f}"
    path = "/api/v1/cells" if gw.keyed else "/api/cells"
    try:
        ans = await gw.get(path, {"bbox": bbox}, keyed=gw.keyed)
    except GatewayError as e:
        raise _fail(e, "Could not read the depth map") from e
    body = ans.body or {}
    cells = []
    for f in body.get("features") or []:
        p = f.get("properties") or {}
        c = _centroid(f.get("geometry"))
        if c is None:
            continue
        dist = haversine_m(lat, lon, c[0], c[1])
        if dist > radius_m:
            continue
        unknown = bool(p.get("unknown")) or p.get("hazard_class") == "unknown" or p.get("depth_cm") is None
        status = "unknown" if unknown else "reported"
        note = None
        if p.get("expired"):
            status = "unknown"
            note = "Reports here have expired. If the last level was deep, treat it as flooded until someone reports again."
        elif unknown and p.get("depth_cm") is not None:
            note = "Evidence here is old or too uncertain; the depth is kept for reference and is still used to block routes."
        cells.append({
            "cell": p.get("cell"),
            "centre": {"lat": round(c[0], 6), "lon": round(c[1], 6)},
            "distance_m": round(dist),
            "status": status,
            "depth_cm": p.get("depth_cm"),
            "depth_sigma_cm": p.get("depth_sigma_cm"),
            "depth_upper_cm": p.get("depth_upper_cm"),
            "velocity_mps": p.get("velocity_mps"),
            "hazard_class": p.get("hazard_class"),
            "n_readings": p.get("n_readings"),
            "updated_at": p.get("updated_at"),
            "age_minutes": _age_minutes(p.get("updated_at"), now),
            "origin": p.get("origin"),
            "simulated": p.get("origin") == "simulated",
            "note": note,
        })
    cells.sort(key=lambda x: x["distance_m"])
    known = [x for x in cells if x["status"] == "reported" and x["depth_cm"] is not None]
    deepest = max(known, key=lambda x: x["depth_cm"]) if known else None
    if not cells:
        summary = (
            f"No reports are in force within {round(radius_m)} m of this point in {city.get('name')}. "
            f"The water depth here is unknown, not safe."
        )
    elif deepest is None:
        summary = f"{len(cells)} map cells within {round(radius_m)} m carry evidence, but none is known well enough to give a current depth. Treat them as unknown, not safe."
    else:
        summary = (
            f"{len(known)} of {len(cells)} map cells within {round(radius_m)} m have a current depth. "
            f"The deepest is {deepest['depth_cm']:.0f} cm (sigma {deepest['depth_sigma_cm']:.0f} cm), {deepest['distance_m']} m away. "
            f"Every other street nearby is unknown, not safe."
        )
    return {
        "point": {"lat": lat, "lon": lon},
        "radius_m": radius_m,
        "city": city.get("id"),
        "summary": summary,
        "cells": cells,
        "cells_with_depth": len(known),
        "cells_listed": len(cells),
        "unknown_note": UNKNOWN_NOTE + " The map lists only cells with evidence; every street not listed is unknown.",
        "simulated_scenario": bool(body.get("simulated_scenario")),
        "map_url": gw.url(f"/map?city={quote(str(city.get('id')))}&at={lat:.5f},{lon:.5f}"),
        "advisory": ADVISORY,
    }


# ----------------------------------------------------------------- safe_route


def _route_props(f: dict[str, Any]) -> dict[str, Any]:
    p = (f or {}).get("properties") or {}
    return {
        "distance_m": p.get("distance_m"),
        "eta_min": p.get("eta_min"),
        "cells_on_route": p.get("cells_on_route"),
        "cells_avoided": p.get("cells_avoided"),
        "unknown_cells_crossed": p.get("unknown_cells_crossed"),
        "impassable_cells_crossed": p.get("impassable_cells_crossed"),
        "old_deep_cells_crossed": p.get("old_deep_cells_crossed"),
        "max_known_depth_cm": p.get("max_known_depth_cm", p.get("max_depth_cm_on_route")),
        "max_depth_sigma_cm": p.get("max_depth_sigma_cm"),
        "max_unknown_depth_cm": p.get("max_unknown_depth_cm"),
        "passable": p.get("passable"),
        "explanation": p.get("explanation"),
    }


async def safe_route(
    gw: Gateway,
    from_lat: float,
    from_lon: float,
    to_lat: float,
    to_lon: float,
    vehicle: str = "car",
    city: str | None = None,
    include_geometry: bool = False,
) -> dict[str, Any]:
    _check_lat_lon(from_lat, from_lon, "start")
    _check_lat_lon(to_lat, to_lon, "end")
    key = vehicle.strip().lower().replace("-", "_").replace(" ", "_")
    if key in ("truck", "tanker", "lorry"):
        raise ToolError(
            "The public route API plans for walking, two_wheeler, auto, car and bus. Truck and diesel tanker routes are "
            "computed only as access routes to critical sites: call site_status for a site to see them."
        )
    v = VEHICLES.get(key)
    if v is None:
        raise ToolError("vehicle must be walking, two_wheeler, auto, car or bus.")
    params: dict[str, Any] = {
        "from": f"{from_lat:.6f},{from_lon:.6f}",
        "to": f"{to_lat:.6f},{to_lon:.6f}",
        "vehicle": v,
    }
    if city:
        params["city"] = city.strip().lower()
    path = "/api/v1/route" if gw.keyed else "/api/route"
    try:
        ans = await gw.get(path, params, keyed=gw.keyed, timeout=90)
    except GatewayError as e:
        if e.code == "no_road":
            raise ToolError(f"No route: {e.message} {ADVISORY}") from e
        raise _fail(e, "The route could not be planned") from e
    f = ans.body or {}
    p = f.get("properties") or {}
    route = _route_props(f)
    base = _route_props(f.get("baseline") or {})
    notes = []
    if p.get("passable") is False:
        notes.append("No route avoids every cell reported too deep for this vehicle. This is the least-bad route; it crosses water over the vehicle's limit. Do not travel unless you must.")
    if (route["unknown_cells_crossed"] or 0) > 0:
        notes.append(f"{route['unknown_cells_crossed']} of the {route['cells_on_route']} map cells on this route have no recent report, so conditions there are unknown, not safe.")
    if p.get("simulated_scenario"):
        notes.append("This answer is from the simulated replay, not real reports. Do not use it for real decisions.")
    page = gw.url("/route?" + urlencode({"city": p.get("city") or params.get("city", ""), "from": params["from"], "to": params["to"], "vehicle": v}))
    out: dict[str, Any] = {
        "city": p.get("city"),
        "vehicle": v,
        "route_kind": p.get("route_kind"),
        "depth_limit_cm": p.get("depth_limit_cm"),
        "advisory_flag": p.get("advisory", True),
        **route,
        "baseline_shortest_route": {k: base[k] for k in ("distance_m", "eta_min", "impassable_cells_crossed", "unknown_cells_crossed", "explanation")},
        "extra_distance_m": (round(route["distance_m"] - base["distance_m"]) if isinstance(route["distance_m"], (int, float)) and isinstance(base["distance_m"], (int, float)) else None),
        "computed_at": p.get("computed_at"),
        "notes": notes,
        "route_page": page,
        "simulated_scenario": bool(p.get("simulated_scenario")),
        "advisory": ADVISORY,
    }
    coords = (f.get("geometry") or {}).get("coordinates") or []
    out["points_on_route"] = len(coords)
    if include_geometry:
        out["coordinates_lon_lat"] = coords
    return out


# ---------------------------------------------------------------------- sites


def _zone(z: dict[str, Any]) -> dict[str, Any]:
    d = z.get("deepest") or None
    return {
        "radius_m": z.get("radius_m"),
        "status": z.get("status"),
        "meaning": ZONE_WORDS.get(str(z.get("status")), ""),
        "cells": z.get("cells"),
        "cells_reported": z.get("cells_reported"),
        "coverage": z.get("coverage"),
        "deepest": None if d is None else {
            "depth_block_cm": d.get("depth_block_cm"),
            "depth_cm": d.get("depth_cm"),
            "depth_sigma_cm": d.get("depth_sigma_cm"),
            "age_h": d.get("age_h"),
            "unknown": d.get("unknown"),
        },
        "old_deep_cells": z.get("old_deep_cells"),
        "old_deep_max_cm": z.get("old_deep_max_cm"),
        "newest_evidence_age_h": z.get("newest_evidence_age_h"),
        "trend": z.get("trend"),
    }


def _site_brief(s: dict[str, Any]) -> dict[str, Any]:
    zones = [_zone(z) for z in s.get("zones") or []]
    return {
        "id": s.get("id"),
        "kind": s.get("kind"),
        "name": s.get("name") or "(unnamed in OpenStreetMap)",
        "operator": s.get("operator"),
        "lat": s.get("lat"),
        "lon": s.get("lon"),
        "city": s.get("city"),
        "source": s.get("source"),
        "status": s.get("status"),
        "meaning": ZONE_WORDS.get(str(s.get("status")), "").replace("in this zone", "in the nearest ring"),
        "zones": zones,
    }


async def list_sites(gw: Gateway, city: str, kind: str = "data_centre", limit: int = 20, offset: int = 0) -> dict[str, Any]:
    kind = kind.strip().lower()
    if kind not in SITE_KINDS:
        raise ToolError("kind must be one of " + ", ".join(SITE_KINDS) + ".")
    if not 1 <= limit <= 100:
        raise ToolError("limit must be between 1 and 100.")
    if not gw.keyed:
        raise ToolError(
            "Listing critical sites needs a Neermattam developer API key (GET /api/v1/sites). Set NEERMATTAM_API_KEY "
            f"(see {gw.url('/developers')}). Without a key, open {gw.url('/sites?city=' + quote(city))} in a browser, or call "
            "site_status with an OpenStreetMap site id (osm:node:..., osm:way:...), which is public."
        )
    try:
        ans = await gw.get("/api/v1/sites", {"city": city.strip().lower(), "kind": kind, "limit": limit, "offset": offset}, keyed=True)
    except GatewayError as e:
        raise _fail(e, "Could not list the sites") from e
    b = ans.body or {}
    return {
        "city": b.get("city"),
        "kind": b.get("kind"),
        "total": b.get("total"),
        "count": b.get("count"),
        "offset": b.get("offset"),
        "threshold_cm": b.get("threshold_cm"),
        "rings_m": b.get("rings_m"),
        "min_coverage": b.get("min_coverage"),
        "sites": [_site_brief(s) for s in b.get("sites") or []],
        "order": "over, nearby, under, unknown, then deepest first, then name",
        "osm_attribution": (b.get("osm") or {}).get("attribution"),
        "terms": b.get("terms"),
        "advisory_flag": b.get("advisory", True),
        "advisory": ADVISORY,
    }


async def site_status(gw: Gateway, site_id: str, include_route_points: bool = False) -> dict[str, Any]:
    site_id = site_id.strip()
    if not site_id:
        raise ToolError("site_id is required, such as osm:node:12406588855.")
    if gw.keyed:
        path = "/api/v1/sites/" + quote(site_id, safe=":")
    else:
        if not site_id.startswith("osm:"):
            raise ToolError("Without an API key only OpenStreetMap sites (ids starting osm:) can be read. Operator sites need the key that added them.")
        path = "/api/sites/" + quote(site_id, safe=":")
    try:
        ans = await gw.get(path, keyed=gw.keyed, timeout=120)
    except GatewayError as e:
        if e.status == 404:
            raise ToolError(f"No such site: {site_id}. {ADVISORY}") from e
        raise _fail(e, "Could not read the site") from e
    s = ans.body or {}
    out = _site_brief(s)
    routes = []
    for r in (s.get("access") or {}).get("routes") or []:
        item = {
            "target": r.get("target"),
            "vehicle": r.get("vehicle"),
            "status": r.get("status"),
            "meaning": ROUTE_STATUS_WORDS.get(str(r.get("status")), ""),
            "reason": r.get("reason"),
            "destination": (r.get("destination") or {}).get("name") or "(unnamed)",
            "distance_m": r.get("distance_m"),
            "detour_m": r.get("detour_m"),
            "eta_min": r.get("eta_min"),
            "cells_on_route": r.get("cells_on_route"),
            "unknown_cells_crossed": r.get("unknown_cells_crossed"),
            "impassable_cells_crossed": r.get("impassable_cells_crossed"),
            "max_known_depth_cm": r.get("max_known_depth_cm"),
            "explanation": r.get("explanation"),
        }
        if include_route_points:
            item["coordinates_lon_lat"] = r.get("coordinates")
        routes.append(item)
    out["access_routes"] = routes
    out["access_computed_at"] = (s.get("access") or {}).get("computed_at")
    risk = s.get("risk") or {}
    out["flood_record"] = {"since": risk.get("since"), "areas": len(risk.get("areas") or []), "note": risk.get("note")}
    out["threshold_cm"] = s.get("threshold_cm")
    out["page"] = gw.url(f"/sites?city={quote(str(s.get('city') or ''))}&site={quote(site_id, safe=':')}")
    out["osm_attribution"] = s.get("osm_attribution")
    out["advisory_flag"] = s.get("advisory", True)
    out["advisory"] = ADVISORY
    return out


# --------------------------------------------------------------- measure_photo


async def _load_media(source: str) -> tuple[str, bytes, str]:
    if source.startswith(("http://", "https://")):
        try:
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
                async with c.stream("GET", source) as r:
                    r.raise_for_status()
                    data = bytearray()
                    async for chunk in r.aiter_bytes():
                        data += chunk
                        if len(data) > MAX_PHOTO_BYTES:
                            raise ToolError("The file at that URL is larger than 25 MB.")
                    ctype = r.headers.get("Content-Type", "").split(";")[0].strip()
        except httpx.HTTPError as e:
            raise ToolError(f"Could not download the photo: {e.__class__.__name__}.") from e
        name = os.path.basename(httpx.URL(source).path) or "photo.jpg"
        return name, bytes(data), ctype or (mimetypes.guess_type(name)[0] or "application/octet-stream")
    path = os.path.expanduser(source)
    if not os.path.isfile(path):
        raise ToolError(f"No file at {source}.")
    if os.path.getsize(path) > MAX_PHOTO_BYTES:
        raise ToolError("The file is larger than 25 MB.")
    with open(path, "rb") as fh:
        data = fh.read()
    name = os.path.basename(path)
    return name, data, mimetypes.guess_type(name)[0] or "application/octet-stream"


def _field_for(name: str, ctype: str) -> str:
    if ctype.startswith("video/") or name.lower().endswith((".mp4", ".mov", ".m4v")):
        return "video"
    if ctype.startswith("image/") or name.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".heic")):
        return "image"
    raise ToolError("measure_photo takes a JPEG or PNG photo, or an MP4 or MOV video.")


async def measure_photo(
    gw: Gateway,
    source: str,
    lat: float,
    lon: float,
    city: str | None = None,
    hint_reference: str | None = None,
) -> dict[str, Any]:
    _check_lat_lon(lat, lon)
    name, data, ctype = await _load_media(source)
    field = _field_for(name, ctype)
    form = {"lat": f"{lat:.6f}", "lon": f"{lon:.6f}"}
    if city:
        form["city"] = city.strip().lower()
    if hint_reference:
        form["hint_reference"] = hint_reference
    try:
        ans = await gw.post_report({field: (name, data, ctype)}, form)
    except GatewayError as e:
        msg = e.message.lower()
        if e.status == 429 and e.code == "rate_limited":
            raise ToolError(f"Rate limited: {e.message} Nothing was measured. {ADVISORY}") from e
        if "daily limit" in msg or "paused for today" in msg:
            raise ToolError(
                "The live demo's daily measurement cap has been reached, so nothing was measured. "
                f"The gateway said: {e.message} The live map and routes still work. {ADVISORY}"
            ) from e
        raise _fail(e, "The photo could not be measured") from e
    b = ans.body or {}
    m = b.get("measurement") or {}
    method = m.get("method") or ("not_measured" if b.get("depth_cm") is None else None)
    cert = b.get("certificate_url")
    mid = m.get("measurement_id")
    ver = b.get("verification") or {}
    cell = b.get("cell") or {}
    status = b.get("status")
    status_words = {
        "fused": "measured and added to the live map",
        "unusable": "no depth could be read from this photo",
        "rejected": "measured, but the gateway's checks refused it for the map",
        "sample": "one of the gateway's sample photos; measured, never added to the map",
    }
    return {
        "status": status,
        "meaning": status_words.get(str(status), ""),
        "summary": b.get("summary"),
        "measurement_id": mid,
        "depth_cm": b.get("depth_cm"),
        "depth_sigma_cm": b.get("depth_sigma_cm"),
        "method": method,
        "method_label": METHOD_LABELS.get(str(method), "") if method else "",
        "is_estimate": method == "model_estimate",
        "confidence": m.get("confidence"),
        "reasons": m.get("reasons") or [],
        "model_estimate": m.get("model_estimate"),
        "models": m.get("models"),
        "place": b.get("place"),
        "verification": {"accepted": ver.get("accepted"), "trust": ver.get("trust"),
                         "refused_by": [r.get("reason") for r in ver.get("rules") or [] if r.get("outcome") == "reject"]} if ver else None,
        "cell": {"cell": cell.get("cell"), "depth_cm": cell.get("depth_cm"), "depth_sigma_cm": cell.get("depth_sigma_cm"),
                 "unknown": cell.get("unknown"), "n_readings": cell.get("n_readings")} if cell else None,
        "verify_url": gw.url(cert) if cert else (gw.url(f"/verify/{mid}") if mid else None),
        "certificate_api_url": gw.url(f"/api/certificates/{mid}") if mid and cert else None,
        "overlay_url": gw.url(b["overlay_url"]) if b.get("overlay_url") else None,
        "processed_ms": b.get("processed_ms"),
        "advisory": b.get("advisory") or ADVISORY,
    }


# ---------------------------------------------------------- verify_certificate


async def verify_certificate(gw: Gateway, measurement_id: str) -> dict[str, Any]:
    import json

    mid = measurement_id.strip()
    if mid.startswith(("http://", "https://")) or "/verify/" in mid:
        mid = mid.rstrip("/").rsplit("/", 1)[-1]
    if not mid.startswith("m_") or not mid.replace("_", "").isalnum():
        raise ToolError("A measurement id looks like m_01M6C1RE0T43R0YKRCE6Q51DHA.")
    try:
        ans = await gw.get(f"/api/certificates/{mid}")
    except GatewayError as e:
        if e.status == 404:
            raise ToolError(f"This gateway holds no certificate for {mid}.") from e
        raise _fail(e, "Could not read the certificate") from e
    cert = ans.body or {}
    verified = ans.headers.get("X-Neermattam-Verified", "").lower() == "true"
    try:
        rec = json.loads(cert.get("payload") or "{}")
    except ValueError:
        rec = {}
    dec = rec.get("decision") or {}
    checks = rec.get("checks") or {}
    method = dec.get("method")
    return {
        "measurement_id": mid,
        "signature_verified_by_gateway": verified,
        "verdict": (
            "The Ed25519 signature checks out against the gateway's published keys, so this record came from the measurement service unchanged."
            if verified else
            "The signature did NOT verify against the published keys. Treat the claims in this record as unverified."
        ),
        "key_id": cert.get("key_id"),
        "issued_at": rec.get("issued_at"),
        "input": rec.get("input"),
        "models": rec.get("models"),
        "prompts": [p.get("id") for p in rec.get("prompts") or []],
        "steps": len(rec.get("steps") or []),
        "check_failures": checks.get("failures") or [],
        "result": {
            "usable": dec.get("usable"),
            "depth_cm": dec.get("depth_cm"),
            "depth_sigma_cm": dec.get("depth_sigma_cm"),
            "method": method,
            "method_label": METHOD_LABELS.get(str(method), "") if method else "",
            "confidence": dec.get("confidence"),
            "reasons": dec.get("reasons") or [],
        },
        "verify_page": gw.url(f"/verify/{mid}"),
        "keys_url": gw.url("/.well-known/neermattam-keys.json"),
        "note": "Whether the gateway added this measurement to the live map is shown on the verify page, not in the signed record.",
        "advisory": ADVISORY,
    }
