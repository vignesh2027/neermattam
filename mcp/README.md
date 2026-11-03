# Neermattam MCP server

An [MCP](https://modelcontextprotocol.io) server that lets any AI agent or assistant ask Neermattam the questions a person asks in a flood: how deep is the water here, which route is open for my vehicle, can people and fuel still reach this data centre. Every tool calls the live gateway's API ([docs/CONTRACTS.md](../docs/CONTRACTS.md), section 3) and passes on what it says, with the same rules the site keeps:

* Every answer is advisory and carries the line "Advisory only. Check local news and official alerts before you travel."
* No answer calls a road, cell, route or site safe. A place with no recent report is **unknown, not safe**.
* Every depth comes with its sigma, and a depth from the scene model is labelled an estimate, not a measurement.
* Nothing is invented: every number in an answer is read from the gateway.

Written in Python with the official [`mcp` SDK](https://pypi.org/project/mcp/) (2.x, `MCPServer`) and `httpx`. It speaks stdio, and streamable HTTP with `--transport streamable-http`.

## Install

```sh
cd mcp
uv sync
uv run neermattam-mcp            # stdio; what MCP clients launch
uv run neermattam-mcp --transport streamable-http --port 8765   # http://127.0.0.1:8765/mcp
```

| Variable | Default | Meaning |
|---|---|---|
| `NEERMATTAM_URL` | `https://applemacbook6sep2004--neermattam-web-web-serve.us-east.modal.run` | The gateway to call |
| `NEERMATTAM_API_KEY` | unset | A developer API key (`/developers`). With it, depth and routes use `/api/v1/*` and `list_sites` works. It is only ever sent to `/api/v1/*`. |

## Client configuration

Claude Desktop (`claude_desktop_config.json`), Cursor (`.cursor/mcp.json`) and most other clients take the same block. Use the absolute path to this folder:

```json
{
  "mcpServers": {
    "neermattam": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/neermattam/mcp", "neermattam-mcp"],
      "env": { "NEERMATTAM_API_KEY": "" }
    }
  }
}
```

Leave `NEERMATTAM_API_KEY` empty (or drop the `env` block) to use the public API. Claude Code: `claude mcp add neermattam -- uv run --directory /path/to/neermattam/mcp neermattam-mcp`. A client that speaks streamable HTTP can connect to `http://127.0.0.1:8765/mcp` instead.

## Tools

| Tool | Gateway endpoint | Key needed |
|---|---|---|
| `list_cities()` | `GET /api/india` | no |
| `water_depth_near(lat, lon, radius_m=500)` | `GET /api/cities`, then `GET /api/cells?bbox=` (or `/api/v1/cells`) | no |
| `safe_route(from_lat, from_lon, to_lat, to_lon, vehicle="car", city=None, include_geometry=False)` | `GET /api/route` (or `/api/v1/route`) | no |
| `list_sites(city, kind="data_centre", limit=20, offset=0)` | `GET /api/v1/sites` | **yes** |
| `site_status(site_id, include_route_points=False)` | `GET /api/sites/{id}` (or `/api/v1/sites/{id}`) | no for OpenStreetMap sites, yes for operator sites |
| `measure_photo(source, lat, lon, city=None, hint_reference=None)` | `POST /api/report` (multipart) | no |
| `verify_certificate(measurement_id)` | `GET /api/certificates/{id}` | no |

Also a resource, `neermattam://cities` (the `list_cities` answer), and a prompt, `plan_trip_in_flooded_city(city, start, destination, vehicle)`, which walks an agent through rainfall, depth at both ends and the route, and tells it to report unknown as unknown.

**`water_depth_near`** returns every map cell whose centre is within `radius_m`, nearest first, each with `depth_cm`, `depth_sigma_cm`, `depth_upper_cm`, `hazard_class`, `n_readings`, `updated_at`, `age_minutes` and `status` (`reported`, or `unknown` when the engine rates the cell unknown, its reports have expired, or it has no depth). The engine lists only cells with evidence, so a street that is not listed is unknown; the summary says so. A point outside the seven cities is an error, not an empty answer.

**`safe_route`** takes `walking`, `two_wheeler`, `auto`, `car` or `bus` (and the gateway's aliases, such as `scooter`). It returns the engine's distance, time, `cells_on_route`, `cells_avoided`, `unknown_cells_crossed`, `impassable_cells_crossed`, the deepest known water with its sigma, `passable`, the engine's own explanation, the shortest route ignoring water for comparison, and a link to the route page. When `passable` is false the answer says it is the least-bad route through water over the vehicle's limit. The public route API has no truck; truck and tanker routes exist only as a site's access routes, so `vehicle="truck"` points the agent to `site_status`.

**`site_status`** gives the site's status and its 300 m and 1 km rings (`over`, `nearby`, `under`, `unknown`, each explained; `under` means the cells with recent reports are below the threshold, never that the site is safe), the six access routes (main road and fuel station, for a tanker, a two-wheeler and a car, each `open`, `no_safe_route` or `unavailable` with a reason) and its area's flood record. **`list_sites`** needs a key because the gateway serves site lists only on the developer API; without one it says so and points to the public `/sites` page and to `site_status`.

**`measure_photo`** sends a local file or a URL (JPEG, PNG, MP4 or MOV, up to 25 MB) with its location through the same `POST /api/report` the site's API uses, and returns the gateway's answer: `status` (`fused`, `unusable`, `rejected` or `sample`), `depth_cm` and `depth_sigma_cm`, `method` with a label (`wheel_geometry` and `person_reference` are measurements, `model_estimate` is the scene model's estimate and `is_estimate` is true), the reasons, the verification outcome, the cell it updated, and the `/verify/` and certificate links. A usable real report is added to the live public map, so the tool description tells the agent to send only genuine photos of that place. When the live demo's daily cap (150 measurements, docs/DEPLOY.md) is used up, the gateway refuses the call and the tool answers with an error that says nothing was measured; it never makes up a result. The gateway's per-address report rate limit (2 a minute with a burst of 3 on the public deployment, docs/DEPLOY.md) is passed on with its wait.

**`verify_certificate`** accepts an id (`m_...`) or a `/verify/` link and reports whether the gateway found the Ed25519 signature valid against its published keys (`X-Neermattam-Verified`), with the models, prompt versions, input hash and the signed result. What the gateway then did with the measurement is on the verify page, not in the signed record, and the answer says so.

Errors come back as MCP tool errors (`isError: true`) with the gateway's own sentence and code, for example `different_cities`, `outside_area`, `no_road`, `routing_unavailable`, `rate_limited` (with the wait) or `missing_key`.

## Real output from the live site

`scripts/live_smoke.py` starts the server over stdio with the SDK's client, lists the tools and calls them against the live site without a key. It never calls `measure_photo`, so it spends none of the daily cap and adds nothing to the map. Run on 3 November 2026 at 14:43 UTC (trimmed where marked):

```text
tools over stdio: list_cities, water_depth_near, safe_route, list_sites, site_status, measure_photo, verify_certificate
prompts: plan_trip_in_flooded_city

== list_cities (is_error=False)
{
 "generated_at": "2026-11-03T14:43:50.052042929Z",
 "cities": [
  {
   "id": "chennai",
   "name": "Chennai",
   "centre": {"lat": 13.02, "lon": 80.215},
   "rain_mm_1h": 0,
   "rain_mm_24h": 3.3,
   "rain_read_at": "2026-11-03T14:41:52.749959308Z",
   "rain_source": "Open-Meteo",
   "real_reports_24h": 0,
   "cells_with_evidence": 0,
   "deepest_cm": null,
   "critical_sites": 862,
   "map_url": "https://applemacbook6sep2004--neermattam-web-web-serve.us-east.modal.run/map?city=chennai"
  },
  ... six more cities: Mumbai, Bengaluru, Hyderabad, Kolkata, Delhi, Visakhapatnam ...
 ],
 "advisory": "Advisory only. Check local news and official alerts before you travel."
}

== safe_route car, T. Nagar to Guindy (is_error=False)
{
 "city": "chennai",
 "vehicle": "car",
 "route_kind": "water_aware",
 "depth_limit_cm": 15.0,
 "advisory_flag": true,
 "distance_m": 5871.0,
 "eta_min": 17.6,
 "cells_on_route": 52,
 "cells_avoided": 0,
 "unknown_cells_crossed": 52,
 "impassable_cells_crossed": 0,
 "max_known_depth_cm": null,
 "passable": true,
 "explanation": "Advisory only. Nothing reported blocks the shortest path for a car, so this is the shortest route. None of the 52 map cells along it has a recent report, so conditions on it are unknown. Check conditions as you go and turn back if the water looks deeper than expected.",
 "baseline_shortest_route": {"distance_m": 5871.0, "eta_min": 17.6, "impassable_cells_crossed": 0, "unknown_cells_crossed": 52, ...},
 "extra_distance_m": 0,
 "notes": ["52 of the 52 map cells on this route have no recent report, so conditions there are unknown, not safe."],
 "route_page": "https://applemacbook6sep2004--neermattam-web-web-serve.us-east.modal.run/route?city=chennai&from=13.041800%2C80.234100&to=13.006700%2C80.220600&vehicle=car",
 "points_on_route": 161,
 "advisory": "Advisory only. Check local news and official alerts before you travel."
}

== water_depth_near T. Nagar, 500 m (is_error=False)
{
 "city": "chennai",
 "summary": "No reports are in force within 500 m of this point in Chennai. The water depth here is unknown, not safe.",
 "cells": [],
 "cells_with_depth": 0,
 ...
}

== site_status osm:node:12406588855 (is_error=False)
{
 "id": "osm:node:12406588855",
 "kind": "data_centre",
 "status": "unknown",
 "meaning": "too few recent reports to say; unknown, not safe",
 "zones": [
  {"radius_m": 300.0, "status": "unknown", "cells": 18, "cells_reported": 0, "coverage": 0.0, ...},
  {"radius_m": 1000.0, "status": "unknown", "cells": 195, "cells_reported": 0, "coverage": 0.0, ...}
 ],
 "access_routes": [
  {"target": "main_road", "vehicle": "truck", "status": "open", "distance_m": 6198.0, "eta_min": 24.8, "cells_on_route": 41, "unknown_cells_crossed": 41, ...},
  ... two_wheeler and car to a main road, and truck, two_wheeler and car to "Indian Oil", all open with every cell unknown ...
 ],
 "flood_record": {"since": "2026-11-03T13:31:51.645586577Z", "areas": 0, "note": "This record holds only what people have reported since 2026-11-03. It builds up over a monsoon; an area with no reports is unknown, not dry."},
 ...
}

== verify_certificate m_01M6C1RE0T43R0YKRCE6Q51DHA (is_error=False)
{
 "signature_verified_by_gateway": true,
 "key_id": "ed25519:5cadd6dd4ddca908",
 "issued_at": "2026-11-01T17:15:48Z",
 "prompts": ["scene@cf7a74eb23ee", "verify-water@2229de9c965d", "verify-wheel@1d51d73f5037"],
 "steps": 13,
 "result": {
  "usable": true,
  "depth_cm": 15.0,
  "depth_sigma_cm": 20.0,
  "method": "model_estimate",
  "method_label": "estimate: the scene model's visual estimate, not a measurement",
  "confidence": 0.23,
  ...
 },
 ...
}

== safe_route across two cities (error path) (is_error=True)
Error executing tool safe_route: The route could not be planned: The start and the end are in different cities. A route has to start and end in the same city. (HTTP 400, code different_cities) Advisory only. Check local news and official alerts before you travel.
```

At the time of the run no city had a real report in the last 24 hours, so every cell and route cell was unknown; the answers say exactly that. `list_sites` was also called once with the operator's demo key and returned Chennai's 15 OpenStreetMap data centres (all `unknown`). The same tools answered over streamable HTTP.

## Tests

```sh
uv run pytest                        # 29 tests, mocked gateway, no network
uv run python scripts/live_smoke.py  # live, not run in CI
```

The tests mock the gateway with `httpx.MockTransport` and cover every tool and its error paths: the daily cap, the rate limit with its wait, a point outside every city, no evidence (unknown, never safe), expired and unknown cells, no road, two cities, a truck, a missing key, an operator site without a key, an unknown site, a missing certificate, a failed signature and a bad id. One test checks that no answer uses the word "safe" except as "not safe", and one drives the server through the MCP protocol with the SDK's in-process client.

## Limits

* It is a client of the gateway: it is as current as the live map and as available as the public deployment, and the public API's per-address rate limits apply.
* `measure_photo` waits up to 270 seconds (the deployment gives perception 240). The scene model's own time budget is 55 seconds, and Baseten latency adds to that on slow days. Each call spends one of the demo's 150 measurements a day.
* `list_sites` and operator sites need a developer key; the gateway does not serve site lists publicly.
* No truck routes between arbitrary points; the public route API does not take `truck`.
* `verify_certificate` reports the gateway's signature check. It does not check the Ed25519 signature itself; `python -m neermattam_perception.replay` does that offline (docs/VERIFY.md).
* The streamable HTTP transport has no authentication of its own; run it on localhost or behind your own proxy. Over HTTP, `measure_photo` takes only URLs and never reads the server's own disk.
