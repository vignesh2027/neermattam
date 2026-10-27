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

- `method`: how `depth_cm` was obtained. `wheel_geometry` is a measurement from a wheel of known size (docs/GEOMETRY.md). `model_estimate` means no wheel could be measured and `depth_cm` is the scene model's own visual estimate, with `depth_sigma_cm` at least 20 cm or 30 percent of the estimate, whichever is larger; the UI must label it as an estimate. Null when `usable` is false.
- `model_estimate`: the scene model's own estimate whenever it gave one (null otherwise), reported alongside a geometry measurement too. When both exist the map uses the geometry; if they differ by more than twice their combined sigma, `confidence` is lowered and `reasons` says so.
- `waterline.agreement_cm`: when the scene model's waterline and the mask waterline disagree, the difference is added in quadrature to `depth_sigma_cm`, so disagreement widens the uncertainty as well as lowering `confidence`.
- `agent_trace`: every model step that led to the answer, in order: the scene reading, each mask proposal (`segment`), each check of a mask by the scene model (`verify`, with decision `accept`, `refine with box ...` or `give up`), each geometry attempt and the estimate. `latency_ms` is the wall time of that step. On the single-request hosted path there is one `segment` step for all objects and no `verify` steps.
- `captured_at`: EXIF `DateTimeOriginal` of the photo (RFC 3339; UTC with `Z` when the camera recorded a zone offset, otherwise local time without an offset), or null. Also sent as the `X-Captured-At` response header.

### POST /v1/transcribe
Multipart `audio` (wav/ogg/m4a/mp3, at most 25 MB). Returns `{"text": "...", "language": "ta+en", "place_text": "Velachery main road", "depth_words": "knee deep", "depth_cm": 45.0, "depth_sigma_cm": 13.5, "model": "muse-spark-open", "cost_usd": 0.004}`. `depth_cm` and `depth_sigma_cm` are the model's reading of the spoken depth words (null when none were spoken), with a sigma of at least 10 cm or 30 percent of the depth. Returns 503 with a reason when the audio model is unavailable.

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
