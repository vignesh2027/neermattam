# Neermattam gateway

The gateway is the Go service at `gateway/` (port 8080) and the only service the public can reach. It serves the four pages, takes reports from the web and WhatsApp, checks them, hands them to perception and the engine, and fans the engine's live cell stream out to browsers. It is written with the standard library only: `net/http` with method patterns, `log/slog`, `html/template` and `embed`. There are no third-party Go modules.

![Live map](img/gateway-map.png)

## Pages

| Path | What it is |
|---|---|
| `/` | The live map. Hex cells from `/api/cells`, updated over `/api/stream`. Legend for depth bands and hazard classes, a popup per cell (depth plus or minus sigma, upper estimate, velocity, hazard, reports, age, origin), a live report ticker, and the advisory note. |
| `/report` | Upload a photo, a short video or a voice note, with a location from a map tap, the device or typed coordinates. Shows the real pipeline stages as they happen and ends with the result card. |
| `/route` | Pick start and end on the map or from presets (Velachery station, Guindy, T. Nagar, Tambaram, Chennai Central) and a vehicle. Draws the water-aware route and the shortest route in two styles, with the engine's summary and explanation. |
| `/about` | How it works, the hazard formula and vehicle limits, checks, credits and limitations. |

Pages are server-rendered. The only script is the vendored MapLibre GL JS 5.6.0 (`gateway/internal/web/static/vendor/maplibre-gl-5.6.0/`, BSD licence) and short inline glue. Every inline script carries a per-request nonce, and the Content-Security-Policy allows only those scripts, the gateway's own files and OpenFreeMap (style, tiles, glyphs, sprites). Without script, the report form and the route form still work: they post or get to the same paths and the server renders the result.

Base map: OpenFreeMap's Positron style, recoloured toward the palette at load time, with OpenStreetMap and OpenMapTiles attribution kept in the map corner. The Inter typeface is vendored (OFL).

| Desktop | Mobile |
|---|---|
| ![Report result](img/gateway-report-result.png) | ![Report result on a phone](img/gateway-report-result-mobile.png) |
| ![Route](img/gateway-route.png) | ![Route on a phone](img/gateway-route-mobile.png) |

The result-card screenshots were taken against a fixture perception server, because the perception service was still being built. The fixture returns the true depth of a synthetic scene rendered by `perception/src/neermattam_perception/synth.py`; the overlay says "TEST FIXTURE" and the card names the models as `fixture-scene` and `fixture-masks`. They show the interface, not a measurement.

## Request path

```
browser or WhatsApp
  |
  gateway
  |  1. per-IP token bucket
  |  2. stream the multipart body, holding at most the largest allowed file
  |  3. sniff the bytes (never the client's Content-Type), apply size and length limits
  |  4. SHA-256 and perceptual hash: refuse a file already reported
  |  5. POST /v1/measure or /v1/transcribe to perception
  |  6. verification rules, giving trust in (0, 1]
  |  7. POST /v1/readings to the engine
  |  8. label the cell as real, tell the live map, reply
```

The same `report.Pipeline` runs for `POST /api/report` (JSON), `POST /report` (the page) and WhatsApp.

**Limits.** Photos up to 12 MB and 50 megapixels (checked from the header before decoding), videos up to 40 MB and 30 seconds (read from the MP4 or QuickTime `mvhd` box), voice notes up to 8 MB. Accepted types are found from magic bytes: JPEG, PNG, MP4, MOV, Ogg, WAV and M4A. HEIC and WebP get a message asking for a JPEG or PNG. The whole body is capped with `http.MaxBytesReader`.

**Rate limits.** A token bucket per client IP: reports 6 per minute with bursts of 4, API reads 120 per minute with bursts of 40. A refused request gets 429 with `Retry-After`. `X-Forwarded-For` is honoured only with `TRUST_PROXY=1`.

**Duplicates.** The exact bytes (SHA-256) and the 64-bit difference hash of each photo are kept for 24 hours. An exact copy, or a photo within 5 bits of an earlier one (a re-encode, resize or screenshot), is refused with 409. Fusing the same water twice would make the engine overconfident. The reading id sent to the engine is derived from the SHA-256, so a retry is also ignored by the engine.

**Progress.** `POST /report` with `Accept: application/x-ndjson` streams one line per stage as the pipeline reaches it (`received`, `scene`, `verify`, `fuse`) and ends with the result card rendered on the server. The page shows measured stage times; it never animates progress it does not have.

**Concurrency.** At most 8 reports are measured at once (`MAX_INFLIGHT_REPORTS`); the next gets 503 with a message to retry.

## Verification

Package `internal/verify`. Each rule returns an outcome (`pass`, `lower`, `reject` or `skip`), a factor and a reason a person can read, and is logged and counted in `/metrics`. Trust is the product of the factors, clamped to 0.05 to 1, and is sent to the engine, which divides the reading's variance by it (`R = sigma^2 / trust`, see ENGINE.md). Only two rules can reject. Everything else lowers trust, because an unusual report can be the first sign of real water.

| Rule | Outcome | Rationale |
|---|---|---|
| **Confidence floor** | Reject below 0.35. Between 0.35 and 0.6, trust is scaled by confidence / 0.6. | A measurement the model itself doubts should not move the map. Voice notes have no model confidence and skip this rule. |
| **Photo freshness** | From EXIF `DateTimeOriginal` (with `OffsetTimeOriginal` when present, otherwise read as IST). Up to 2 h old: pass. 2 to 6 h: 0.8. 6 to 24 h: 0.5. Over 24 h: reject. Clock more than 15 minutes ahead: 0.6 and the arrival time is used. No EXIF: 0.9 and the arrival time is used. | Water changes within hours. An old photo is fused at the time it was taken, so the engine's decay ages it correctly. WhatsApp strips EXIF, so its absence is only a small penalty. The engine refuses readings over 24 hours old anyway. |
| **Source** | Voice: 0.6. Photo and video: pass. | A spoken "knee deep" is a rough estimate. |
| **Neighbour agreement** | Known engine cells within 300 m, excluding any cell touched by simulated readings, are combined by inverse variance, with 5 cm of extra spread per 100 m of distance. More than 3 standard deviations away: 0.4. 2 to 3: 0.75. Otherwise pass. No cells: skip. | Strong disagreement is suspicious but not proof: a subway can hold a metre of water 100 m from a dry road. Lowering trust lets the filter weigh it without discarding a true first report. |
| **Rainfall plausibility** | Modelled hourly precipitation for the 48 hours before the observation, from the Open-Meteo forecast API (which serves the past days too). Depth over 10 cm with under 1 mm of rain: 0.6. Otherwise pass. API unavailable: skip. | Deep water on a dry day is unusual, but reservoir releases, burst mains and tides do flood Chennai streets, so it is never a rejection. |

The thresholds are engineering judgements, not values fitted to data; they live in `verify.DefaultThresholds`. Open-Meteo is called without a key under its free non-commercial terms (fewer than 10,000 calls a day, CC BY 4.0 attribution on the About page). Each fetch covers a 0.05 degree square for 30 minutes, so a busy evening costs a handful of calls.

Voice notes go to perception's `/v1/transcribe`. Words such as "ankle", "knee" or "waist" (and a few Tamil equivalents), or an explicit "30 cm" or "2 feet", become a depth with deliberately wide uncertainty (for example knee: 45 cm plus or minus 8, from assumed adult proportions). No depth words means the report is unusable.

## Live map and the cell proxy

`GET /api/cells` proxies `/v1/cells` for the whole service area (or a `bbox`), caches each answer for 2 seconds and serves it gzipped, so a crowd opening the map costs the engine one query every 2 seconds.

`GET /api/stream` is served from one upstream connection to the engine's `/v1/stream`, reconnected with capped backoff. Up to 200 browsers can subscribe (`MAX_SSE_CLIENTS`); the next gets 503 with `Retry-After`. Each browser has a 256-event buffer; one that falls behind is disconnected, and its `EventSource` reconnects and refetches. Events:

- `cell`: a changed cell, as the engine sends it, plus `properties.origin`.
- `report`: a new report for the ticker.
- `resync`: refetch `/api/cells` (after an engine reconnect or when the engine says it dropped events).
- `hello`, and a comment ping every 20 seconds.

`GET /api/route` proxies `/v1/route`, accepting `walking` as an alias for `pedestrian`. The route and its `baseline` member pass through unchanged, with `simulated_scenario: true` added when the map holds simulated water.

## Demo mode

`DEMO=1` seeds the engine with a simulated flood: twelve pools of water at low points that flooded during Cyclone Michaung in December 2023 (Ram Nagar and Vijayanagar in Velachery, Velachery Main Road, Pallikaranai, the Madley Road subway, Usman Road, West Mambalam, Saidapet by the Adyar, Madipakkam, Ashok Nagar, Perungudi), a few dry streets, and a few vague reports that the engine rates unknown. About 850 readings go in at start. The depths are invented. A new simulated reading arrives every 6 seconds with the water slowly rising and falling, so the live map visibly changes, and the whole scenario is refreshed every 30 minutes so it does not decay away.

Simulated and real evidence are never mixed unlabelled:

- Simulated readings have ids starting with `sim_` and no `source`.
- The gateway labels every cell it serves as `simulated`, `reported` or `mixed`. The engine streams a reading's seven changed cells before the gateway sees its response, so before each post the gateway registers a 160 m zone around the point for 15 seconds, and a streamed cell whose centre falls inside is labelled from it.
- The map shows a "Simulated scenario" banner, outlines real cells, tags the ticker, and the popup, result card and route summary all say when simulated water is involved.
- Verification never compares a real report with simulated cells.

Every cell already in the engine when demo mode starts is labelled simulated, which errs on the safe side. Point a demo gateway at an engine with its own snapshot file, not a production one.

## WhatsApp

`/webhooks/whatsapp` is enabled only when `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_APP_SECRET` and `WHATSAPP_TOKEN` are all set; otherwise both methods answer 404 and the start-up log names the missing variables (never their values).

- `GET` answers Meta's handshake when `hub.verify_token` matches (constant-time compare).
- `POST` checks `X-Hub-Signature-256` as HMAC-SHA256 of the raw body with the app secret, compared with `hmac.Equal`, before parsing anything. A bad or missing signature gets 401.
- Image, video, audio and location messages are read. A media message waits up to 10 minutes for a location from the same sender, or the reverse. Redelivered message ids are ignored.
- Media is fetched from the Graph API (`/{media-id}` for the URL, then the file, both with the bearer token and the same size limits), run through the same pipeline, and answered with a plain-text result such as "Neermattam measured about 34 cm of water near Velachery (plus or minus 4 cm), using a two wheeler wheel as the ruler. Confidence 0.82. ... Advisory only."
- The webhook answers 200 at once and works in the background, at most 4 reports at a time.

## Operations

- `GET /healthz`: 200 while the process runs.
- `GET /readyz`: 200 only when the engine and perception both answer their `/readyz`; otherwise 503 with each one's state, plus whether the engine stream is connected and whether WhatsApp is enabled.
- `GET /metrics`: Prometheus text format from a small built-in exporter (`internal/metrics`; the official client would add a dozen modules for counters, gauges and histograms). Requests by route and status, request and upstream latency, report outcomes, verification rule outcomes, trust, rate-limit refusals, cache hits, live clients and dropped clients.
- Logs are JSON from `log/slog`: one line per request (method, route pattern, status, duration, bytes, never client addresses), one per verification rule and one per report. Tokens and secrets are never logged.
- Timeouts: engine 5 s, routes 20 s, perception 90 s, Open-Meteo 4 s, Graph API 30 s, request headers 10 s, request body 3 minutes, response write 3 minutes (except the live stream).
- Shutdown on SIGINT or SIGTERM closes the live streams, drains requests for up to 15 seconds and lets WhatsApp replies finish.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `GATEWAY_ADDR` | `:8080` | Listen address |
| `PUBLIC_URL` | `http://localhost:8080` | Used in WhatsApp replies |
| `ENGINE_URL` | `http://127.0.0.1:8302` | Engine |
| `PERCEPTION_URL` | `http://127.0.0.1:8301` | Perception |
| `OPEN_METEO_URL` | `https://api.open-meteo.com` | Rainfall API |
| `ENGINE_TIMEOUT`, `ROUTE_TIMEOUT`, `PERCEPTION_TIMEOUT`, `WEATHER_TIMEOUT` | `5s`, `20s`, `90s`, `4s` | Upstream timeouts |
| `GATEWAY_BBOX` | `80.10,12.85,80.33,13.24` | Service area, same as the engine |
| `MAX_IMAGE_BYTES`, `MAX_VIDEO_BYTES`, `MAX_AUDIO_BYTES` | 12 MiB, 40 MiB, 8 MiB | Upload sizes |
| `MAX_VIDEO_SECONDS` | `30` | Video length |
| `MAX_IMAGE_PIXELS` | `50000000` | Decompression bomb guard |
| `REPORT_RATE_PER_MIN`, `REPORT_BURST` | `6`, `4` | Report rate limit per IP |
| `API_RATE_PER_MIN`, `API_BURST` | `120`, `40` | Read API rate limit per IP |
| `TRUST_PROXY` | `false` | Honour `X-Forwarded-For` |
| `MAX_INFLIGHT_REPORTS` | `8` | Reports measured at once |
| `MAX_SSE_CLIENTS` | `200` | Live map connections |
| `CELLS_CACHE_TTL` | `2s` | Cell proxy cache |
| `DUPLICATE_WINDOW`, `NEAR_DUPLICATE_BITS` | `24h`, `5` | Duplicate detection |
| `DEMO` | `false` | Simulated scenario |
| `DEMO_DRIP_INTERVAL`, `DEMO_RESEED_INTERVAL` | `6s`, `30m` | Demo pacing |
| `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_APP_SECRET`, `WHATSAPP_TOKEN` | empty | All three enable the webhook |
| `WHATSAPP_PHONE_NUMBER_ID` | empty | Sender for replies (taken from the webhook when empty) |
| `WHATSAPP_GRAPH_URL` | `https://graph.facebook.com/v23.0` | Graph API base |
| `LOG_LEVEL` | `info` | `debug`, `info`, `warn`, `error` |

## Running

Locally, next to the engine:

```sh
cd engine && ENGINE_SNAPSHOT_PATH=/tmp/neermattam-demo-cells.json cargo run --release &
cd gateway && DEMO=1 go run ./cmd/neermattam-gateway
# open http://localhost:8080
```

With Docker (the OSM extract must be at `data/osm/chennai-roads.osm.pbf`; see ENGINE.md):

```sh
DEMO=1 docker compose up --build
```

`docker-compose.yml` builds all three services. Only the gateway publishes a port; the engine sits on an internal network with no internet access, the extract is mounted read-only, and the engine's snapshot lives in a named volume. `gateway/Dockerfile` runs the tests, builds a static binary and ships it on `distroless/static` as a non-root user. `engine/Dockerfile` builds the release binary and ships it on `distroless/cc`. Perception is built from an inline Dockerfile that copies only its project files and sources.

## Tests

```sh
cd gateway
go test ./...
go vet ./...
gofmt -l .
```

The tests use `httptest` fakes for the engine (including its SSE stream) and perception, a fake Graph API and a fake rainfall source. They cover upload limits (size, video length, missing header, disguised files, location, area), rate limiting and `Retry-After`, exact and near duplicates, the full report path with the reading the engine receives, the NDJSON stages, every verification rule, Open-Meteo summing and caching, WhatsApp signatures (good, tampered, missing, wrong secret), the handshake, pairing and the pairing window, template rendering of every page (no rejected values, no em dashes or arrows, every inline script carrying the CSP nonce), the cell cache and origin labels, the SSE fan-out with its client limit and resync, readiness, metrics format, gzip of static files, MP4 duration parsing, EXIF capture times and the demo scenario's validity.
