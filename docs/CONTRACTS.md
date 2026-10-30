# Service contracts

All bodies are JSON (UTF-8). Times are RFC 3339 UTC. Coordinates are WGS84 decimal degrees. Lengths are centimetres unless a field name says otherwise. Every service exposes `GET /healthz` (200 when alive) and `GET /readyz` (200 when its dependencies are reachable, 503 with a reason otherwise).

## 1. Perception (Python, port 8301)

### POST /v1/measure
Multipart form:
- `image` (jpeg/png, required unless `video` is present)
- `video` (mp4/mov, optional; used for velocity)
- `hint_reference` (optional: `two_wheeler_wheel`, `car_wheel`, `auto_wheel`, `kerb`, `person`)
- `hint_diameter_cm` (optional float, when the reporter knows the wheel size)

Response 200:
```json
{
  "measurement_id": "m_01J...",
  "usable": true,
  "reasons": ["standing water visible", "two-wheeler wheel found"],
  "depth_cm": 23.4,
  "depth_sigma_cm": 3.1,
  "reference": {"kind": "two_wheeler_wheel", "diameter_cm": 43.4, "diameter_sigma_cm": 2.0, "source": "catalogue:90/100-10", "height_cm": null, "height_sigma_cm": null},
  "waterline": {"method": "sam_mask_intersection", "y_px": 812.5, "agreement_cm": 1.2},
  "velocity_mps": null,
  "velocity_sigma_mps": null,
  "confidence": 0.82,
  "overlay_png_b64": "...",
  "models": {"scene": "muse-spark-1.3", "masks": "sam-3.1"},
  "timings_ms": {"scene": 1400, "masks": 900, "geometry": 12},
  "cost_usd": 0.0041,
  "method": "wheel_geometry",
  "model_estimate": {"depth_cm": 30.0, "depth_sigma_cm": 20.0, "basis": "water reaches the lower half of the scooter wheels", "model": "muse-spark-open"},
  "agent_trace": [
    {"step": "segment", "target": "two wheeler wheel 1", "model": "sam-2.1-hiera-small", "input": "box [612,580,660,628] px + point [636,600]", "output": "mask 1450 px, bbox [610,578,661,626] px, score 0.91", "decision": "verify", "latency_ms": 70},
    {"step": "verify", "target": "two wheeler wheel 1", "model": "muse-spark-open", "input": "crop [520,488,752,720] px with the outline drawn", "output": "is_target=true, outline good, waterline visible", "decision": "accept", "latency_ms": 18000}
  ],
  "captured_at": "2015-12-02T07:56:12"
}
```
When `usable` is false, `depth_cm` is null and `reasons` explains why (no water, no reference object, blurred, ambiguous waterline).

- `method`: how `depth_cm` was obtained, in order of preference:
  - `wheel_geometry`: a measurement from a wheel of known size (docs/GEOMETRY.md).
  - `person_reference`: a measurement from an adult standing in the water, scaled by an adult stature prior (docs/GEOMETRY.md, section 9). Its sigma is typically 10 to 15 cm; the UI must say it is measured against a person.
  - `model_estimate`: neither could be measured (or a wheel measurement was set aside as too shallow, see below) and `depth_cm` is the scene model's own visual estimate, with `depth_sigma_cm` at least 20 cm or 30 percent of the estimate, whichever is larger; the UI must label it as an estimate.

  Null when `usable` is false. When a wheel and a person are both measured, the wheel is used; if they differ by more than twice their combined sigma, `confidence` is lowered and `reasons` says so.
- `reference`: for `wheel_geometry`, the wheel (`diameter_cm`, `diameter_sigma_cm`). For `person_reference`, `kind` is `person`, `diameter_cm` and `diameter_sigma_cm` are null, and `height_cm` and `height_sigma_cm` give the stature prior used. Null for `model_estimate`.
- `waterline.method`: `sam_mask_intersection` for a wheel; `person_mask_bottom` for a person (where the person's mask meets the water). `agreement_cm` compares it with the scene model's own waterline; for a person, with the depth at the body landmark (knee, waist, chest and so on) the scene model named at the waterline.
- Faces: every image the service returns (`overlay_png_b64`) or stores has the head of every person it found blurred (people the scene model or SAM found; a face neither finds is not covered, see docs/GEOMETRY.md section 9.6). The photo itself is still sent to the scene model and to SAM, which run inside the service.
- `model_estimate`: the scene model's own estimate whenever it gave one (null otherwise), reported alongside a geometry measurement too. When both exist the map uses the geometry; if they differ by more than twice their combined sigma, `confidence` is lowered and `reasons` says so. Shallow side: when the estimate lies above the wheel measurement's two-sigma upper bound (`depth_cm + 2 x depth_sigma_cm`) and by more than half the estimate's sigma, the measurement is not published; `method` is `model_estimate` and `reasons` gives the wheel's reading. Calling deep water shallow is the dangerous error, so this side is the cautious one.
- Time budget: the service stops starting or waiting for the scene model's checks 50 s after a request starts (a 55 s budget, 5 s kept to finish) and answers with what was verified by then; `reasons` then says the time budget ran out, and `agent_trace` has a `budget` step.
- `waterline.agreement_cm`: when the scene model's waterline and the mask waterline disagree, the difference is added in quadrature to `depth_sigma_cm`, so disagreement widens the uncertainty as well as lowering `confidence`.
- `agent_trace`: every model step that led to the answer, in the order the steps happened (checks of different objects run in parallel and interleave): the scene reading, each mask proposal (`segment`), each check of a mask by the scene model (`verify`, with decision `accept`, `refine with box ...` or `give up`), each geometry attempt, the estimate, and a `budget` step when the time budget stopped open checks. `latency_ms` is the wall time of that step. On the single-request hosted path there is one `segment` step for all objects and no `verify` steps.
- `captured_at`: EXIF `DateTimeOriginal` of the photo (RFC 3339; UTC with `Z` when the camera recorded a zone offset, otherwise local time without an offset), or null. Also sent as the `X-Captured-At` response header.

### POST /v1/measure/stream
The same request as `POST /v1/measure`, answered as server-sent events (`Content-Type: text/event-stream`) so a client can show the agent working while it works. Requests that fail validation (413, 415, 422) get the same plain HTTP errors as `/v1/measure` before any event is sent. Then:

- `event: step` once per trace step, the moment it happens; `data` is one `agent_trace` item (`step`, `target`, `model`, `input`, `output`, `decision`, `latency_ms`). Checks of different objects run in parallel, so steps of different targets interleave.
- `event: result` once at the end; `data` is exactly the `POST /v1/measure` response body. Its `agent_trace` holds the same steps in the same order. The stream then ends.
- `event: error` instead of `result` when a model is unavailable; `data` is `{"status": 503, "detail": "model unavailable: ..."}`. The stream then ends.
- A comment line (`: keep-alive`) every 15 seconds while nothing else is sent.

```
event: step
data: {"step": "scene", "target": "photo", "model": "muse-spark-open", "input": "photo 2048x1536 px", "output": "water visible; references: two_wheeler_wheel; own estimate 20 cm", "decision": "segment and verify 1 wheel and the water", "latency_ms": 9800}

event: result
data: {"measurement_id": "m_01J...", "usable": true, ...}
```

### POST /v1/transcribe
Multipart `audio` (wav/ogg/m4a/mp3, at most 25 MB). The voice note may be in any of Hindi, Tamil, Telugu, Kannada, Malayalam, Bengali, Marathi, Gujarati, Punjabi, Odia, Urdu or English, mixed freely. Returns:

```json
{"text": "वेलाचेरी मेन रोड पे घुटने तक पानी है", "language": "hi", "languages": ["hi"],
 "place_text": "Velachery Main Road", "place_spoken": "वेलाचेरी मेन रोड",
 "depth_words": "घुटने तक", "depth_words_en": "knee deep",
 "depth_cm": 45.0, "depth_sigma_cm": 13.5, "depth_low_cm": 35.0, "depth_high_cm": 55.0,
 "depth_basis": "phrase table: knee (hi)", "model": "muse-spark-open", "cost_usd": 0.004}
```

- `text` is the transcript in the scripts spoken.
- `language` is the detected language as ISO 639-1 codes (`hi`, `ta`, `te`, `kn`, `ml`, `bn`, `mr`, `gu`, `pa`, `or`, `ur`, `en`), joined with `+` for code-switching, main language first (for example `ta+en`); `languages` is the same as a list.
- `place_text` is the place normalised to the usual Latin-script spelling (for geocoding); `place_spoken` is the place as spoken. Both are empty when no place was named.
- `depth_words` are the depth words as spoken; `depth_words_en` is their English gloss.
- `depth_cm`, `depth_sigma_cm`, `depth_low_cm` and `depth_high_cm` come from the documented phrase table (`perception/src/neermattam_perception/voice.py`: ankle, shin, knee, thigh, waist, chest and neck deep in every supported language), or from a spoken number with a unit, or, when neither matches, from the model's own reading; `depth_basis` says which. The sigma is at least 10 cm or 30 percent of the depth. All four are null when no depth was spoken.

Returns 503 with a reason when the audio model is unavailable.

## 2. Engine (Rust, port 8302)

The engine covers a configured list of cities (by default `chennai`, `mumbai`, `bengaluru`, `hyderabad`, `kolkata` and `delhi`), each with a metro bounding box and its own road graph. The hex grid is shared; a city scopes queries and picks the road graph. Every error is `{"error": "..."}`: 400 for invalid input (the message names the field), 404 for an unknown cell or when no road connects the points, 413 for an oversized body, and 503 while a city's road graph is loading or has failed.

### POST /v1/readings
```json
{"reading_id": "r_...", "lat": 12.9791, "lon": 80.2209, "depth_cm": 23.4, "depth_sigma_cm": 3.1,
 "velocity_mps": 0.4, "velocity_sigma_mps": 0.2, "observed_at": "2026-10-28T10:15:00Z",
 "source": "photo|video|voice|official", "trust": 0.9}
```
Accepted when `lat`,`lon` fall inside any covered city's box. Returns `{"cell": "8a61...", "city": "chennai", "depth_cm": 21.0, "depth_sigma_cm": 2.4, "hazard": {...}}`.

### GET /v1/cells?bbox=minLon,minLat,maxLon,maxLat&city=<id>
At least one of `bbox` and `city`; with both, a cell must be in both. `bbox` selects by cell centre; `city` selects the cells on the city's map (centre in the box widened by three cell edge lengths). Returns a GeoJSON FeatureCollection of hex cells, each with these properties:
- `city` (the city holding the cell's centre, else the city whose box widened by three cell edge lengths holds it, or null)
- `depth_cm`, `depth_sigma_cm`, `velocity_mps`
- `hazard_rating`, `hazard_class` (`low`, `danger_some`, `danger_most`, `danger_all`, `unknown`)
- `n_readings`, `updated_at`

### GET /v1/route?from=lat,lon&to=lat,lon&vehicle=pedestrian|two_wheeler|auto|car|bus
Planned on the graph of a city whose box holds both ends. A request with an end outside every city, or with ends in two different cities, is a 400 saying so. Returns a GeoJSON LineString Feature with these properties:
- `city`, `distance_m`, `eta_min`
- `max_depth_cm_on_route`, `cells_avoided`, `unknown_cells_crossed`
- `advisory: true`, `explanation`

The baseline shortest route (ignoring water) is returned alongside it for comparison.

### GET /v1/stream?city=<id>
Server-sent events. Each event is a changed cell: `event: cell`, with data the same as one feature above. With `city`, only cells on that city's map are sent: those whose centre lies in its box widened by three cell edge lengths, so every cell a reading inside the city touches is included.

### GET /v1/cities
`{"cities": [{"id": "chennai", "name": "Chennai", "bbox": [80.10, 12.85, 80.33, 13.24], "centre": {"lat": 13.045, "lon": 80.215}, "cells": 120, "graph": {"status": "ready|loading|failed", "nodes": 316734, "edges": 709504, "memory_mib": 17.6, ...}}]}`. A failed graph carries `error` instead of the counts.

### GET /v1/hotspots?city=<id>&limit=<1..200>
Cells with evidence in force, ranked by a 0 to 100 score (`city` optional, `limit` default 20). Returns `{"city", "advisory": true, "score": "<formula>", "count", "hotspots": [...]}`; each hotspot has `cell`, `lat`, `lon`, `score`, `parts` (`depth`, `hazard`, `freshness`, `reports`), `depth_block_cm`, `depth_cm`, `depth_sigma_cm`, `hazard_class_upper`, `age_h`, `independent_reports`, `unknown`, `blocks` (vehicles the cell is closed to) and `reasons` (plain-English sentences, one per part plus notes). The formula is in docs/ENGINE.md.

### GET /v1/summary?city=<id>
`city` optional. Returns `cells`, `by_class` (`low`, `danger_some`, `danger_most`, `danger_all`, `unknown`), `unknown`, `stale`, `expired`, `cells_in_conflict`, `conflicting_readings`, `blocked` (cells closed to each vehicle), `needs_fresh_report` with `needs_fresh_report_cells` (stale or expired cells last seen deeper than 15 cm; at most 50, deepest first, each with `cell`, `lat`, `lon`, `last_depth_cm`, `last_reading_age_h`, `expired`), `readings`, `newest_reading_at`, and `history_points` (`total` and `max`: depth history points held over the whole map and the cap).

### GET /v1/recession?cell=<H3 index>
A cell's depth history: `history` (its latest direct readings, oldest first, each with `at`, `reading_cm`, `reading_sigma_cm`, `fused_cm`, `fused_sigma_cm`, `migrated`), `current` (the cell's properties as in `/v1/cells`), `drain` (`rate_cm_per_h`, `rate_sigma_cm_per_h`, `trend` of `falling|rising|steady`, `points_used`, `from`, `to`, `hours_to_15_cm`) or null, `drain_note` (how it was fitted, or why there is none), and `advisory: true`. 404 when no report has touched the cell. The drain rate never changes routing.

### GET /v1/export?city=<id>&format=geojson|csv
`city` required, `format` default `geojson`. GeoJSON is the `/v1/cells` collection for the city; CSV has one row per cell with the columns `cell,city,lat,lon,depth_cm,depth_sigma_cm,depth_upper_cm,depth_sigma_meas_cm,conflict_high_mean_cm,velocity_mps,velocity_sigma_mps,velocity_assumed,hazard_rating,hazard_class,hazard_class_upper,debris_factor,unknown,expired,n_readings,n_neighbour,conflicts,updated_at,last_depth_cm,last_reading_age_h` (empty for null). Sent as an attachment; the `X-Neermattam-Truncated` header says whether the cell cap cut it short and `X-Neermattam-Total-Cells` gives the city's cell count. When the cap applies, the cells kept are the highest by hotspot score, in that order. The GeoJSON also carries top-level `truncated` and `total_cells`.

### GET /readyz (engine)
200 once the primary city's road graph (`ENGINE_PRIMARY_CITY`, default chennai) is loaded, with `status` `ready`, or `degraded` when another city's graph is loading or has failed (named in `reason`). 503 with a reason while the primary graph is loading or has failed. The body lists every city's graph status under `cities`.

## 3. Gateway (Go, port 8080, the only public service)

Every page takes `?lang=` (one of `en`, `ta`, `hi`, `te`, `kn`, `ml`, `bn`, `mr`, remembered in the `nm_lang` cookie) and, where it shows a city, `?city=` (an engine city id, remembered in the `nm_city` cookie; default `chennai`). Every JSON error is `{"error": "...", "code": "..."}` with `code` stable and `error` a plain sentence. Public endpoints are rate limited per client address and answer 429 with `Retry-After`. Everything about water is advisory.

### Pages (HTML, rendered on the server)
- `GET /`: landing page with a live preview of the selected city.
- `GET /map?city=&at=lat,lon`: the live map. With `at` and no `city`, the city holding the point is chosen.
- `GET /report?city=&at=`, `POST /report`: report water. The point must lie in the selected city (or, with no city sent, in any covered city).
- `GET /route?city=&from=&to=&vehicle=`, `GET /route/plan` (JSON: `summary_html` and `route`): water-aware routes inside one city.
- `GET /alerts?city=`: "Watch my street": pick a point or the last planned route and a vehicle, then turn on notifications.
- `GET /control?city=&cell=`: the control room. Needs the `nm_ctl` cookie; without it, `GET /control` shows the key entry page. `POST /control/login` (form field `key`) sets the cookie, `POST /control/logout` clears it. When `CONTROL_ROOM_KEY` is unset the keyed control room is off and only the demo exists.
- `GET /control/demo?cell=`: the public read-only control room over the simulated Chennai scenario, labelled as such. Only with `DEMO=1`.
- `GET /control/panel?city=&cell=` (and `/control/demo/panel?cell=`): JSON `{"html": "..."}` with the live parts of the control room re-rendered, for the page's refresh glue. Same access rules as the page.
- `GET /control/export?city=&format=geojson|csv` (and `/control/demo/export?format=`): the engine's `/v1/export` passed through as an attachment, with `X-Neermattam-Truncated` and `X-Neermattam-Total-Cells`.
- `GET /developers`: the developer API documentation and terms.
- `GET /about`.
- `GET /sw.js`: the service worker that shows push notifications (served from the root so its scope covers the site).

### Public JSON API (rate limited per client address)
- `GET /api/cities`: `{"cities": [{"id", "name", "bbox", "centre", "cells", "routing": "ready|loading|failed", "simulated_scenario"}], "default": "chennai"}`.
- `GET /api/cells?city=<id>` or `?bbox=minLon,minLat,maxLon,maxLat` (default: the default city): the engine's cells for the city, compacted, each with `properties.city` and `properties.origin` (`reported`, `simulated` or `mixed`), plus top-level `city` and `simulated_scenario`. 2-second cache, `ETag`, 304.
- `GET /api/route?from=lat,lon&to=lat,lon&vehicle=pedestrian|walking|two_wheeler|auto|car|bus`: the engine's route Feature with its `baseline`. Both ends must lie in one covered city. Engine 400 and 503 answers are passed on with a plain message and `code` `outside_area`, `different_cities` or `routing_unavailable`.
- `GET /api/stream?city=<id>`: server-sent events: `hello`, `cell` (one compacted cell on that city's map), `report` (a ticker item with `city`), `resync`, and a ping comment every 20 seconds. Without `city`, every city.
- `POST /api/report`: multipart with `image`, `video` or `audio`, `lat`, `lon`, optional `city`, `hint_reference`, `hint_diameter_cm`. Runs perception, verification and the engine; returns the measurement, the verification and the cell update.
- `GET /api/overlay/{measurement_id}`: the report overlay JPEG, kept for an hour.
- `GET /api/alerts/key`: `{"vapid_public_key": "<base64url P-256 point>"}`, or 404 when push alerts are off.
- `POST /api/alerts`: JSON `{"subscription": <PushSubscription.toJSON()>, "city", "vehicle", "lang", "point": {"lat","lon"}}`, or with `"route": {"from": "lat,lon", "to": "lat,lon"}` in place of `point`: the gateway plans the water-aware route for that vehicle and watches every map cell along it (sampled every 25 m, at most 800 points). The endpoint must be https on a known push service (`PUSH_HOSTS`). Returns 201 `{"id", "token", "status": "over|under|unknown", "class", "depth_cm", "where", "message", "expires_at"}`. The `token` is needed to read or delete it. 400 `bad_endpoint`, `bad_keys` or `bad_watch`; 409 `too_many` when the endpoint already watches `ALERTS_PER_CLIENT` places; 503 `full` when the gateway holds `ALERTS_MAX`; 404 `alerts_off`.
- `GET /api/alerts/{id}` and `DELETE /api/alerts/{id}`, with header `X-Alert-Token`: read its status or stop it.

### Developer API (API key)
`Authorization: Bearer <key>` (or `X-API-Key`). Keys are created with `go run ./cmd/neermattam-keys create`; only their SHA-256 is stored. Every answer carries `X-RateLimit-Limit` and `X-RateLimit-Remaining` (requests per minute for that key). 401 `missing_key` or `invalid_key`, 403 `key_disabled`, 429 `rate_limited`.
- `GET /api/v1/route?from=lat,lon&to=lat,lon&vehicle=`: as `/api/route`, plus top-level `advisory: true` and `terms`. Counted as one route for billing.
- `GET /api/v1/cells?city=<id>` (or `bbox`): as `/api/cells`, plus `advisory` and `terms`; `ETag` and 304.
- `GET /api/v1/usage`: this key's own counts: `{"key": "<prefix>", "name", "rate_per_min", "total": {"routes", "cells"}, "days": [{"date", "routes", "cells"}]}` (last 90 UTC days).

### WhatsApp
- `GET /webhooks/whatsapp`: Meta's verify handshake. `POST /webhooks/whatsapp`: signed with `X-Hub-Signature-256` (HMAC-SHA256 of the raw body with the app secret). A photo, video or voice note plus a location is a report. The text `watch` (optionally with a vehicle, such as `watch car`) followed by a location watches that point, with the same rules as push alerts; `stop` ends every watch for the number. Only when the three WhatsApp variables are set.

### Operations
- `GET /healthz`: 200 while the process runs.
- `GET /readyz`: 200 with `status` `ready`, or `degraded` when the engine is degraded (some city's routing is loading or failed, named in `reason`); 503 when the engine or perception is not ready.
- `GET /metrics`: Prometheus text format.
