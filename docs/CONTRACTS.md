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
  "reference": {"kind": "two_wheeler_wheel", "diameter_cm": 43.4, "diameter_sigma_cm": 2.0, "source": "catalogue:90/100-10"},
  "waterline": {"method": "sam_mask_intersection", "y_px": 812.5, "agreement_cm": 1.2},
  "velocity_mps": null,
  "velocity_sigma_mps": null,
  "confidence": 0.82,
  "overlay_png_b64": "...",
  "models": {"scene": "muse-spark-1.3", "masks": "sam-3.1"},
  "timings_ms": {"scene": 1400, "masks": 900, "geometry": 12},
  "cost_usd": 0.0041
}
```
When `usable` is false, `depth_cm` is null and `reasons` explains why (no water, no reference object, blurred, ambiguous waterline).

### POST /v1/transcribe
Multipart `audio` (wav/ogg/m4a). Returns `{"text": "...", "language": "ta+en", "place_text": "Velachery main road", "depth_words": "knee deep"}`.

## 2. Engine (Rust, port 8302)

### POST /v1/readings
```json
{"reading_id": "r_...", "lat": 12.9791, "lon": 80.2209, "depth_cm": 23.4, "depth_sigma_cm": 3.1,
 "velocity_mps": 0.4, "velocity_sigma_mps": 0.2, "observed_at": "2026-10-28T10:15:00Z",
 "source": "photo|video|voice|official", "trust": 0.9}
```
Returns `{"cell": "8a61...", "depth_cm": 21.0, "depth_sigma_cm": 2.4, "hazard": {...}}`.

### GET /v1/cells?bbox=minLon,minLat,maxLon,maxLat
Returns a GeoJSON FeatureCollection of hex cells, each with these properties:
- `depth_cm`, `depth_sigma_cm`, `velocity_mps`
- `hazard_rating`, `hazard_class` (`low`, `danger_some`, `danger_most`, `danger_all`, `unknown`)
- `n_readings`, `updated_at`

### GET /v1/route?from=lat,lon&to=lat,lon&vehicle=pedestrian|two_wheeler|auto|car|bus
Returns a GeoJSON LineString Feature with these properties:
- `distance_m`, `eta_min`
- `max_depth_cm_on_route`, `cells_avoided`, `unknown_cells_crossed`
- `advisory: true`, `explanation`

The baseline shortest route (ignoring water) is returned alongside it for comparison.

### GET /v1/stream
Server-sent events. Each event is a changed cell: `event: cell`, with data the same as one feature above.

## 3. Gateway (Go, port 8080, the only public service)
- `GET /`: the live map page.
- `GET /report`: the report page.
- `POST /api/report`: multipart with image, video or audio, plus `lat` and `lon`. It calls perception, verifies the report, then posts to the engine. Returns the measurement and the cell update.
- `GET /api/cells`, `GET /api/route`, `GET /api/stream`: proxies to the engine with caching and rate limits.
- `POST /webhooks/whatsapp`: WhatsApp Cloud API webhook (signature checked with `X-Hub-Signature-256`), plus `GET` for the verify handshake.
