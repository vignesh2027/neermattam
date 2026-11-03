"""Every tool against a mocked gateway, including the error paths."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import httpx
import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError

from neermattam_mcp import tools
from neermattam_mcp.gateway import ADVISORY, Gateway
from neermattam_mcp.server import build_server

pytestmark = pytest.mark.anyio

BASE = "https://nm.test"

CITIES = {"cities": [
    {"id": "chennai", "name": "Chennai", "bbox": [80.10, 12.80, 80.33, 13.24], "centre": {"lat": 13.02, "lon": 80.215}, "routing": "ready"},
    {"id": "mumbai", "name": "Mumbai", "bbox": [72.77, 18.88, 73.13, 19.32], "centre": {"lat": 19.1, "lon": 72.95}, "routing": "ready"},
], "default": "chennai"}

INDIA = {"generated_at": "2026-11-03T14:37:05Z", "cities": [
    {"id": "chennai", "name": "Chennai", "lat": 13.02, "lon": 80.215, "rain_mm_1h": 0, "rain_mm_24h": 3.3,
     "rain_at": "2026-11-03T14:21:52Z", "rain_source": "Open-Meteo", "real_reports_24h": 0,
     "cells_with_evidence": 0, "deepest_cm": None, "measured_photos": 4, "critical_sites": 862},
], "measured_photos": []}


def hexagon(lat: float, lon: float, d: float = 0.0006) -> dict:
    ring = [[lon + d, lat], [lon + d / 2, lat + d], [lon - d / 2, lat + d], [lon - d, lat], [lon - d / 2, lat - d], [lon + d / 2, lat - d], [lon + d, lat]]
    return {"type": "Polygon", "coordinates": [ring]}


def route_feature(**over) -> dict:
    props = {"advisory": True, "cells_avoided": 2, "cells_on_route": 52, "city": "chennai", "computed_at": "2026-11-03T14:37:19Z",
             "depth_limit_cm": 15.0, "distance_m": 6400.0, "eta_min": 19.0, "explanation": "Advisory only. The route avoids 2 cells.",
             "impassable_cells_crossed": 0, "max_known_depth_cm": 6.0, "max_depth_sigma_cm": 2.0, "passable": True,
             "route_kind": "water_aware", "unknown_cells_crossed": 40, "vehicle": "car"}
    props.update(over)
    base = dict(props, distance_m=5871.0, eta_min=17.6, route_kind="baseline", impassable_cells_crossed=2, explanation="Shortest route ignoring water.")
    return {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[80.23, 13.04], [80.22, 13.0]]},
            "properties": props, "baseline": {"type": "Feature", "properties": base}}


def gateway(routes: dict, key: str | None = None, seen: list | None = None) -> Gateway:
    """routes maps 'METHOD path' to (status, body, headers) or a callable(request)."""

    def handler(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(req)
        r = routes.get(f"{req.method} {req.url.path}")
        if r is None:
            return httpx.Response(404, json={"error": "not found", "code": "not_found"})
        if callable(r):
            r = r(req)
        status, body, *rest = r
        headers = rest[0] if rest else {}
        return httpx.Response(status, json=body, headers=headers)

    return Gateway(BASE, api_key=key, transport=httpx.MockTransport(handler))


SAFE_CLAIM = re.compile(r"(?<!not )(?<!no_)(?<!never )\bsafe\b", re.I)


def assert_never_safe(out: dict) -> None:
    text = json.dumps(out)
    text = text.replace("safe_route", "")
    assert not SAFE_CLAIM.search(text), SAFE_CLAIM.search(text)
    assert out["advisory"] == ADVISORY


# ---------------------------------------------------------------- list_cities

async def test_list_cities():
    out = await tools.list_cities(gateway({"GET /api/india": (200, INDIA)}))
    c = out["cities"][0]
    assert c["id"] == "chennai" and c["rain_mm_24h"] == 3.3 and c["deepest_cm"] is None
    assert c["map_url"] == BASE + "/map?city=chennai"
    assert_never_safe(out)


async def test_list_cities_engine_down():
    gw = gateway({"GET /api/india": (503, {"error": "the map engine is not reachable right now", "code": "engine_unavailable"})})
    with pytest.raises(ToolError, match="engine_unavailable"):
        await tools.list_cities(gw)


async def test_rate_limited_carries_retry_after():
    gw = gateway({"GET /api/india": (429, {"error": "Too many requests.", "code": "rate_limited", "retry_after_seconds": 7}, {"Retry-After": "7"})})
    with pytest.raises(ToolError, match="Retry after 7 s"):
        await tools.list_cities(gw)


# ------------------------------------------------------------ water_depth_near

NOW = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)


async def test_depth_no_evidence_is_unknown_never_safe():
    gw = gateway({"GET /api/cities": (200, CITIES), "GET /api/cells": (200, {"type": "FeatureCollection", "features": [], "simulated_scenario": False})})
    out = await tools.water_depth_near(gw, 13.0418, 80.2341, 500, now=NOW)
    assert out["cells"] == [] and out["cells_with_depth"] == 0
    assert "unknown, not safe" in out["summary"]
    assert out["city"] == "chennai"
    assert_never_safe(out)


async def test_depth_cells_sorted_filtered_and_unknown_marked():
    feats = [
        {"type": "Feature", "geometry": hexagon(13.0430, 80.2341), "properties": {
            "cell": "8a_near", "depth_cm": 22.0, "depth_sigma_cm": 3.0, "depth_upper_cm": 26.5, "hazard_class": "danger_some",
            "unknown": False, "expired": False, "n_readings": 3, "updated_at": "2026-11-03T14:30:00Z", "origin": "reported"}},
        {"type": "Feature", "geometry": hexagon(13.0418, 80.2345), "properties": {
            "cell": "8a_old", "depth_cm": 40.0, "depth_sigma_cm": 14.0, "hazard_class": "unknown",
            "unknown": True, "expired": False, "n_readings": 1, "updated_at": "2026-11-03T06:00:00Z", "origin": "reported"}},
        {"type": "Feature", "geometry": hexagon(13.0800, 80.2341), "properties": {
            "cell": "8a_far", "depth_cm": 60.0, "depth_sigma_cm": 3.0, "hazard_class": "danger_most", "unknown": False}},
    ]
    seen: list = []
    gw = gateway({"GET /api/cities": (200, CITIES), "GET /api/cells": (200, {"type": "FeatureCollection", "features": feats})}, seen=seen)
    out = await tools.water_depth_near(gw, 13.0418, 80.2341, 500, now=NOW)
    assert [c["cell"] for c in out["cells"]] == ["8a_old", "8a_near"]
    old, near = out["cells"]
    assert old["status"] == "unknown" and old["note"]
    assert near["status"] == "reported" and near["depth_sigma_cm"] == 3.0 and near["age_minutes"] == 30.0
    assert "22 cm (sigma 3 cm)" in out["summary"]
    cells_req = [r for r in seen if r.url.path == "/api/cells"][0]
    assert "bbox" in cells_req.url.params and "authorization" not in cells_req.headers
    assert_never_safe(out)


async def test_depth_outside_coverage():
    gw = gateway({"GET /api/cities": (200, CITIES)})
    with pytest.raises(ToolError, match="outside every city"):
        await tools.water_depth_near(gw, 51.5, -0.12, 500)


async def test_depth_radius_bounds():
    with pytest.raises(ToolError, match="radius_m"):
        await tools.water_depth_near(gateway({}), 13.04, 80.23, 10)


async def test_depth_uses_key_when_set():
    seen: list = []
    gw = gateway({"GET /api/cities": (200, CITIES), "GET /api/v1/cells": (200, {"features": [], "advisory": True})}, key="nm_live_TEST", seen=seen)
    await tools.water_depth_near(gw, 13.04, 80.23, 300, now=NOW)
    req = [r for r in seen if r.url.path == "/api/v1/cells"][0]
    assert req.headers["authorization"] == "Bearer nm_live_TEST"
    # The key never goes to the public endpoints.
    assert all("authorization" not in r.headers for r in seen if not r.url.path.startswith("/api/v1/"))


# ----------------------------------------------------------------- safe_route

async def test_route_ok_with_unknown_cells():
    seen: list = []
    gw = gateway({"GET /api/route": (200, route_feature())}, seen=seen)
    out = await tools.safe_route(gw, 13.0418, 80.2341, 13.0067, 80.2206, "walking")
    assert seen[0].url.params["vehicle"] == "pedestrian"
    assert out["advisory_flag"] is True and out["cells_avoided"] == 2 and out["unknown_cells_crossed"] == 40
    assert out["extra_distance_m"] == 529
    assert any("unknown, not safe" in n for n in out["notes"])
    assert "coordinates_lon_lat" not in out and out["points_on_route"] == 2
    assert_never_safe(out)


async def test_route_least_bad_when_not_passable():
    gw = gateway({"GET /api/route": (200, route_feature(passable=False, impassable_cells_crossed=3, unknown_cells_crossed=0))})
    out = await tools.safe_route(gw, 13.04, 80.23, 13.0, 80.22, "bus", include_geometry=True)
    assert out["passable"] is False
    assert any("least-bad" in n for n in out["notes"])
    assert out["coordinates_lon_lat"]


async def test_route_no_road():
    gw = gateway({"GET /api/route": (404, {"error": "No road connects these two points. Try points on or near a main road.", "code": "no_road"})})
    with pytest.raises(ToolError, match="No route: No road connects"):
        await tools.safe_route(gw, 13.04, 80.23, 13.0, 80.22, "car")


async def test_route_different_cities():
    gw = gateway({"GET /api/route": (400, {"error": "The start and the end are in different cities.", "code": "different_cities"})})
    with pytest.raises(ToolError, match="different_cities"):
        await tools.safe_route(gw, 13.04, 80.23, 19.1, 72.95, "car")


async def test_route_truck_points_to_sites_and_bad_vehicle():
    with pytest.raises(ToolError, match="site_status"):
        await tools.safe_route(gateway({}), 13.04, 80.23, 13.0, 80.22, "truck")
    with pytest.raises(ToolError, match="vehicle must be"):
        await tools.safe_route(gateway({}), 13.04, 80.23, 13.0, 80.22, "boat")


async def test_route_keyed():
    seen: list = []
    gw = gateway({"GET /api/v1/route": (200, dict(route_feature(), advisory=True, terms="x"))}, key="nm_live_TEST", seen=seen)
    out = await tools.safe_route(gw, 13.04, 80.23, 13.0, 80.22, "two-wheeler", city="Chennai")
    assert seen[0].url.params["city"] == "chennai" and seen[0].url.params["vehicle"] == "two_wheeler"
    assert out["vehicle"] == "two_wheeler"


# ---------------------------------------------------------------------- sites

SITE = {"id": "osm:node:1", "kind": "data_centre", "name": None, "operator": None, "lat": 12.87, "lon": 80.21, "city": "chennai",
        "source": "osm", "status": "unknown", "advisory": True, "threshold_cm": 15.0, "osm_attribution": "(c) OpenStreetMap contributors",
        "zones": [{"radius_m": 300.0, "status": "unknown", "cells": 18, "cells_reported": 0, "coverage": 0.0, "deepest": None},
                  {"radius_m": 1000.0, "status": "over", "cells": 195, "cells_reported": 4, "coverage": 0.02,
                   "deepest": {"depth_block_cm": 31.0, "depth_cm": 25.0, "depth_sigma_cm": 4.0, "age_h": 1.5, "unknown": False}}],
        "access": {"advisory": True, "computed_at": "2026-11-03T14:38:01Z", "routes": [
            {"target": "main_road", "vehicle": "truck", "status": "open", "destination": {"name": None}, "distance_m": 6198.0,
             "detour_m": 0.0, "eta_min": 24.8, "cells_on_route": 41, "unknown_cells_crossed": 41, "impassable_cells_crossed": 0,
             "max_known_depth_cm": None, "explanation": "Advisory only.", "coordinates": [[80.2, 12.8]]},
            {"target": "fuel", "vehicle": "car", "status": "unavailable", "reason": "no_target_in_range", "destination": None}]},
        "risk": {"since": "2026-11-03T13:31:51Z", "areas": [], "note": "an area with no reports is unknown, not dry."}}


async def test_site_status_public():
    seen: list = []
    gw = gateway({"GET /api/sites/osm:node:1": (200, SITE)}, seen=seen)
    out = await tools.site_status(gw, "osm:node:1")
    assert out["status"] == "unknown" and out["zones"][1]["deepest"]["depth_sigma_cm"] == 4.0
    assert [r["status"] for r in out["access_routes"]] == ["open", "unavailable"]
    assert out["access_routes"][1]["reason"] == "no_target_in_range"
    assert "coordinates_lon_lat" not in out["access_routes"][0]
    assert "authorization" not in seen[0].headers
    assert_never_safe(out)


async def test_site_status_not_found_and_operator_without_key():
    gw = gateway({"GET /api/sites/osm:node:9": (404, {"error": "No such site.", "code": "no_site"})})
    with pytest.raises(ToolError, match="No such site"):
        await tools.site_status(gw, "osm:node:9")
    with pytest.raises(ToolError, match="Operator sites need the key"):
        await tools.site_status(gateway({}), "op_0123456789abcdef")


async def test_site_status_keyed_reads_v1():
    seen: list = []
    gw = gateway({"GET /api/v1/sites/op_0123456789abcdef": (200, dict(SITE, id="op_0123456789abcdef", source="operator"))}, key="k", seen=seen)
    out = await tools.site_status(gw, "op_0123456789abcdef", include_route_points=True)
    assert out["source"] == "operator" and out["access_routes"][0]["coordinates_lon_lat"]


async def test_list_sites_needs_key():
    with pytest.raises(ToolError, match="NEERMATTAM_API_KEY"):
        await tools.list_sites(gateway({}), "chennai")


async def test_list_sites_keyed():
    body = {"advisory": True, "terms": "t", "city": "chennai", "kind": "data_centre", "total": 15, "count": 1, "offset": 0,
            "threshold_cm": 15.0, "rings_m": [300.0, 1000.0], "min_coverage": 0.5, "osm": {"attribution": "OSM"}, "sites": [SITE]}
    out = await tools.list_sites(gateway({"GET /api/v1/sites": (200, body)}, key="k"), "chennai")
    assert out["total"] == 15 and out["sites"][0]["name"] == "(unnamed in OpenStreetMap)"
    assert out["sites"][0]["zones"][0]["meaning"].endswith("unknown, not safe")
    with pytest.raises(ToolError, match="kind must be"):
        await tools.list_sites(gateway({}, key="k"), "chennai", kind="mall")


# -------------------------------------------------------------- measure_photo

REPORT = {"status": "fused", "summary": "About 30 cm of water.", "lat": 13.04, "lon": 80.23, "place": "Nandanam",
          "measurement": {"measurement_id": "m_01TEST", "usable": True, "method": "model_estimate", "confidence": 0.3,
                          "reasons": ["no wheel could be measured"], "models": {"scene": "muse-spark-open", "masks": "sam-2.1"}},
          "verification": {"accepted": True, "trust": 0.6, "rules": [{"rule": "time", "outcome": "pass", "reason": "ok"}]},
          "depth_cm": 30.0, "depth_sigma_cm": 20.0, "cell": {"cell": "8a", "depth_cm": 30.0, "depth_sigma_cm": 20.0, "unknown": False, "n_readings": 1},
          "certificate_url": "/verify/m_01TEST", "overlay_url": "/api/overlay/m_01TEST", "advisory": ADVISORY, "processed_ms": 41000}


@pytest.fixture
def photo(tmp_path):
    p = tmp_path / "street.jpg"
    p.write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
    return str(p)


async def test_measure_photo_estimate_is_labelled(photo):
    seen: list = []
    gw = gateway({"POST /api/report": (200, REPORT)}, seen=seen)
    out = await tools.measure_photo(gw, photo, 13.04, 80.23, city="chennai")
    body = seen[0].content
    assert b'name="image"; filename="street.jpg"' in body and b'name="lat"' in body and b"chennai" in body
    assert out["is_estimate"] is True and "not a measurement" in out["method_label"]
    assert out["depth_cm"] == 30.0 and out["depth_sigma_cm"] == 20.0
    assert out["verify_url"] == BASE + "/verify/m_01TEST"
    assert out["certificate_api_url"] == BASE + "/api/certificates/m_01TEST"
    assert out["status"] == "fused" and out["verification"]["refused_by"] == []


async def test_measure_photo_daily_cap(photo):
    cap = {"error": "The measurement service could not use this report: Live measurements are paused for today: this demo's daily "
                    "limit of 150 measurements has been used.", "code": "perception_refused"}
    gw = gateway({"POST /api/report": (502, cap)})
    with pytest.raises(ToolError, match="daily measurement cap has been reached, so nothing was measured"):
        await tools.measure_photo(gw, photo, 13.04, 80.23)


async def test_measure_photo_rate_limited(photo):
    gw = gateway({"POST /api/report": (429, {"error": "Too many requests. Please wait 9 seconds and try again.", "code": "rate_limited", "retry_after_seconds": 9})})
    with pytest.raises(ToolError, match="Rate limited"):
        await tools.measure_photo(gw, photo, 13.04, 80.23)


async def test_measure_photo_unusable(photo):
    unusable = {"status": "unusable", "summary": "No water could be seen.", "depth_cm": None, "depth_sigma_cm": None,
                "measurement": {"measurement_id": "m_02", "usable": False, "method": None, "reasons": ["no standing water visible"]},
                "certificate_url": "/verify/m_02", "advisory": ADVISORY}
    out = await tools.measure_photo(gateway({"POST /api/report": (200, unusable)}), photo, 13.04, 80.23)
    assert out["depth_cm"] is None and out["method"] == "not_measured" and out["reasons"] == ["no standing water visible"]


async def test_measure_photo_bad_inputs(tmp_path):
    txt = tmp_path / "notes.txt"
    txt.write_text("hi")
    with pytest.raises(ToolError, match="JPEG or PNG"):
        await tools.measure_photo(gateway({}), str(txt), 13.04, 80.23)
    with pytest.raises(ToolError, match="No file"):
        await tools.measure_photo(gateway({}), str(tmp_path / "missing.jpg"), 13.04, 80.23)


# ---------------------------------------------------------- verify_certificate

def cert(verified: str) -> tuple:
    rec = {"format": "neermattam-certificate/1", "measurement_id": "m_01ABC", "issued_at": "2026-11-01T10:00:00Z",
           "input": {"kind": "image", "sha256": "ab" * 32, "width": 1024, "height": 768},
           "models": [{"role": "scene", "name": "muse-spark-open", "version": "v"}], "prompts": [{"id": "scene@123", "sha256": "0" * 64}],
           "steps": [{"step": "scene"}], "checks": {"rules": ["schema"], "failures": []},
           "decision": {"usable": True, "method": "wheel_geometry", "depth_cm": 23.4, "depth_sigma_cm": 3.1, "confidence": 0.8, "reasons": []}}
    body = {"format": "neermattam-certificate/1", "alg": "Ed25519", "key_id": "ed25519:abc", "payload": json.dumps(rec), "signature": "sig"}
    return (200, body, {"X-Neermattam-Verified": verified})


async def test_verify_certificate_ok_from_link():
    out = await tools.verify_certificate(gateway({"GET /api/certificates/m_01ABC": cert("true")}), BASE + "/verify/m_01ABC")
    assert out["signature_verified_by_gateway"] is True
    assert out["result"]["depth_cm"] == 23.4 and out["result"]["method_label"].startswith("measured")
    assert out["verify_page"] == BASE + "/verify/m_01ABC"


async def test_verify_certificate_failed_signature():
    out = await tools.verify_certificate(gateway({"GET /api/certificates/m_01ABC": cert("false")}), "m_01ABC")
    assert out["signature_verified_by_gateway"] is False and "did NOT verify" in out["verdict"]


async def test_verify_certificate_missing_and_bad_id():
    gw = gateway({"GET /api/certificates/m_09": (404, {"error": "This gateway holds no certificate for that measurement.", "code": "no_certificate"})})
    with pytest.raises(ToolError, match="no certificate"):
        await tools.verify_certificate(gw, "m_09")
    with pytest.raises(ToolError, match="measurement id looks like"):
        await tools.verify_certificate(gw, "../../etc/passwd")


# ------------------------------------------------- through the MCP protocol

async def test_server_lists_and_calls_tools_over_mcp():
    gw = gateway({"GET /api/india": (200, INDIA), "GET /api/route": (404, {"error": "No road connects these two points.", "code": "no_road"})})
    async with Client(build_server(gw)) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert names == {"list_cities", "water_depth_near", "safe_route", "list_sites", "site_status", "measure_photo", "verify_certificate"}
        ok = await c.call_tool("list_cities", {})
        assert not ok.is_error and ok.structured_content["advisory"] == ADVISORY
        bad = await c.call_tool("safe_route", {"from_lat": 13.04, "from_lon": 80.23, "to_lat": 13.0, "to_lon": 80.22})
        assert bad.is_error and "No route" in bad.content[0].text
        prompts = (await c.list_prompts()).prompts
        assert prompts[0].name == "plan_trip_in_flooded_city"
        res = await c.read_resource("neermattam://cities")
        assert "chennai" in res.contents[0].text
