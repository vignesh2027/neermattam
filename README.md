<div align="center">

<img src="gateway/internal/web/static/brand/mark-128.png" width="88" alt="Neermattam logo">

# Neermattam

### How deep is the water on your street? Meta's open models measure it from one phone photo, and a whole city acts on the answer.

**Street-level flood depth for Indian cities.** Two Meta open models check each other to turn an ordinary phone photo into a measured depth with an honest error. A Rust engine fuses every measurement into a live street map, routes each vehicle around water it cannot cross, and keeps data centres, hospitals and telephone exchanges reachable.

[![Live site](https://img.shields.io/badge/Live_site-open-2563EB?style=for-the-badge)](https://applemacbook6sep2004--neermattam-web-web-serve.us-east.modal.run)
[![Demo video](https://img.shields.io/badge/Demo_video-2:54-9F1239?style=for-the-badge&logo=youtube&logoColor=white)](https://youtu.be/cDX91WGaFUQ)
[![MCP server](https://img.shields.io/badge/MCP_server-for_AI_assistants-4C1D95?style=for-the-badge)](mcp/README.md)

![Muse Spark Open](https://img.shields.io/badge/Meta-Muse_Spark_Open-4C1D95)
![SAM 2.1](https://img.shields.io/badge/Meta-SAM_2.1-4C1D95)
![Rust](https://img.shields.io/badge/engine-Rust-B7410E?logo=rust)
![Go](https://img.shields.io/badge/gateway-Go_stdlib_only-00ADD8?logo=go&logoColor=white)
![Python](https://img.shields.io/badge/perception-Python-3776AB?logo=python&logoColor=white)
![Languages](https://img.shields.io/badge/UI-8_Indian_languages-2563EB)
![Cities](https://img.shields.io/badge/cities-7_live-2563EB)
![License](https://img.shields.io/badge/license-MIT-0B1B33)

Built solo for the **Meta Global AI Developer Hackathon 2026**, Indie track. Every line written during the event.

</div>

![Neermattam landing page: the headline "How deep is the water on your street?", links to every part of the system, and a recorded run of a flooded Chennai street measured at 46 cm plus or minus 13 against a person in the water, with Muse Spark Open's verdicts on each outline](docs/img/gateway-home.png)

## Why this matters

Every northeast monsoon, a rider in Chennai stops at the edge of a flooded street and has to guess. Ankle deep, the scooter gets through. Knee deep, the engine drowns and the rider is stuck in moving water. The same guess is made by an ambulance driver, a delivery fleet, a parent walking a child home, and a data centre deciding whether a diesel tanker can still reach its generators.

* The Chennai floods of December 2015 killed 289 people and caused at least USD 2 billion in losses ([Swiss Re sigma 1/2016](https://www.swissre.com/dam/jcr:b4a158fc-2669-4b70-b817-96db6a8705e9/sigma1_2016_en.pdf)).
* India's own urban flood guidelines call the lack of real-time data over cities "a very critical gap" ([NDMA, 2010](https://sdma.goa.gov.in/sites/default/files/2022-05/management_urban_flooding.pdf)).
* Mumbai and Chennai hold about 73% of India's data centre capacity, and both flood ([CBRE via ANI, 2025](https://www.tribuneindia.com/news/business/mumbai-leads-indias-data-centre-capacity-with-53-share-ytd-report)).
* Worldwide, 1.81 billion people live with serious flood risk, 390 million of them in India ([Rentschler et al., Nature Communications, 2022](https://www.nature.com/articles/s41467-022-30727-4)).

Forecasts say where a city will flood. Gauges measure rain at fixed points. Nothing tells you how deep the water is on this street, right now. Neermattam (Tamil for "water level") answers with a measurement, and turns each measurement into decisions for everyone who has to cross the water.

## One photo, a whole city's flood response

![The idea: one photo serves riders, families, control rooms and critical sites, and can run in any flood-prone city](docs/img/landing-idea.jpg)

| | What it does today | Where |
|---|---|---|
| **Measure** | Depth in centimetres with its error from one photo; every agent step and the model's own words shown; a labelled estimate when nothing can be measured | [/report](https://applemacbook6sep2004--neermattam-web-web-serve.us-east.modal.run/report) |
| **Voice** | Depth words such as "knee deep" parsed in 12 languages (tested on 36 written phrases) | /report |
| **Live map** | Seven cities with live Open-Meteo rainfall; a street nobody reported on is unknown, never safe | [/map](https://applemacbook6sep2004--neermattam-web-web-serve.us-east.modal.run/map) |
| **Routes by vehicle** | Walking, two-wheeler, auto, car and bus, each kept out of water it should not cross | [/route](https://applemacbook6sep2004--neermattam-web-web-serve.us-east.modal.run/route) |
| **Street alerts** | Watch a street or the way home and hear when the water crosses your limit | [/alerts](https://applemacbook6sep2004--neermattam-web-web-serve.us-east.modal.run/alerts) |
| **Control room** | Hotspots, freshness of every reading, streets that need a new report, CSV and GeoJSON export | [/control/demo](https://applemacbook6sep2004--neermattam-web-web-serve.us-east.modal.run/control/demo) |
| **Critical sites** | Data centres, hospitals, telephone exchanges: water at 300 m and 1 km, tanker and staff access routes, site alerts, flood record for siting | [/sites](https://applemacbook6sep2004--neermattam-web-web-serve.us-east.modal.run/sites) |
| **Signed results** | Ed25519 certificate over the photo hash, models, prompts, every step and number; replayable offline | /verify |
| **Developer API and MCP** | Keyed API for fleets and apps; an MCP server so any AI assistant can ask for depth, routes and site status | [/developers](https://applemacbook6sep2004--neermattam-web-web-serve.us-east.modal.run/developers), [mcp/](mcp/README.md) |
| **Eight languages** | English, Tamil, Hindi, Telugu, Kannada, Malayalam, Bengali, Marathi on every page | everywhere |

<table>
<tr>
<td width="50%"><img src="docs/img/route-replay.jpg" alt="A two-wheeler routed around flooded hexagons in a labelled replay of Cyclone Michaung"><br><sub>Routes by vehicle, shown on the labelled replay of Cyclone Michaung (simulated).</sub></td>
<td width="50%"><img src="docs/img/landing-critical-sites.jpg" alt="Critical sites: water watch and access routes for a data centre"><br><sub>Critical sites: water watch and tanker access for a Chennai data centre.</sub></td>
</tr>
</table>

## Architecture

![Architecture: photo, voice or API into the Go gateway, the Python perception agent with Muse Spark Open and SAM 2.1, the Rust fusion and routing engine, and six outputs](docs/img/architecture.png)

**Why three languages, each for one job:**

* **Rust, the engine** (about 15,000 lines, 139 tests). Fusion and routing sit on the hot path of every map tile and route, so they are written for speed and memory safety. H3 hexagons about 66 m across, a per-cell Kalman filter that never assumes water drains away, hazard rating with uncertainty, and vehicle-aware routing on seven OpenStreetMap city graphs (9.5 million directed edges, loaded in parallel). Fusing one reading takes about 1 µs; a 3.5 km car route takes 0.28 ms on a dry map.
* **Go, the gateway** (standard library only, 189 test functions). One small static binary serves every page server-side in eight languages, checks uploads, scores trust, streams the live map, runs alerts and the control room, signs and verifies certificates, and serves the developer API. No frontend framework, no Node toolchain.
* **Python, the perception agent** (336 test functions across perception and MCP). This is where the models live: Muse Spark Open and SAM 2.1 in a verify-and-refine loop, the wheel and person geometry, the safety guards and the Ed25519 certificates, plus the MCP server.

One Modal container runs all three behind a Go launcher with a model budget guard and a daily measurement cap. Contracts between services are written first in [docs/CONTRACTS.md](docs/CONTRACTS.md).

## What happens to one photo

1. **Muse Spark Open reads the scene.** Is there standing water? Where are the vehicles, wheels, people and faces? It also gives its own visual estimate of the depth.
2. **SAM 2.1 outlines the wheel or person and the water.** Proposals are ranked by wheel shape, tyre darkness and plausible size.
3. **The agent verifies and refines.** The best two outlines go back to Muse Spark Open on a close crop. It picks one, rejects both, or sends SAM a corrected box, at most twice. Numeric checks can overrule its "good": a dark ring outside the outline means it traced the rim. The whole request has a 55 s budget.
4. **Geometry gives the depth and its sigma.** The waterline across a wheel is a chord of a circle of known diameter, so the depth is a fraction of that diameter and camera distance cancels out ([docs/GEOMETRY.md](docs/GEOMETRY.md)). Failing a wheel, an adult standing upright is measured against an Indian stature prior. Failing both, the scene model's estimate is returned, labelled as an estimate, with a sigma of at least 20 cm.
5. **A signed certificate is issued** over the input hash, models, prompt versions, every agent step and every number.
6. **Fusion and routing.** The gateway scores trust, and the engine fuses the reading into an H3 hex cell (edges of about 66 m) with a per-cell Kalman filter, then routes each vehicle on the OpenStreetMap road graph within its depth limit.

![The agent trace from a recorded run on a real Chennai photo: SAM segments a wheel, Muse Spark Open rejects the outline and asks for a refined box](docs/img/gateway-report-trace-live.png)

```mermaid
flowchart LR
  subgraph svc["One Modal container (or three local processes)"]
    gw["<b>Gateway</b> (Go)<br/>:8080 locally, :7860 on Modal<br/>pages, upload checks, trust,<br/>live map stream, certificate pages"]
    per["<b>Perception</b> (Python)<br/>:8301<br/>verify-and-refine agent, geometry,<br/>Ed25519 certificates"]
    eng["<b>Engine</b> (Rust)<br/>:8302<br/>H3 fusion, hazard rating,<br/>OSM routing by vehicle"]
    sam["SAM 2.1 hiera-small<br/>Meta open weights, in process"]
  end
  user["Phone browser or WhatsApp"] ---|"photo, video, voice note"| gw
  gw ---|"POST /v1/measure/stream"| per
  gw ---|"POST /v1/readings, GET /v1/route, /v1/stream"| eng
  per --- sam
  per ---|"HTTPS"| muse["Muse Spark Open<br/>Meta open weights on Baseten"]
  per -.-|"written, not running"| api["Meta Model API<br/>Muse Spark 1.3, SAM 3.1"]
```

On Modal a Go launcher adds a front proxy (:8000) and a model budget guard (:8300) ([docs/DEPLOY.md](docs/DEPLOY.md)). Contracts: [docs/CONTRACTS.md](docs/CONTRACTS.md).

## How I use Meta's models

| Model | Where it runs | What it does | Status |
|---|---|---|---|
| Muse Spark Open | Baseten (`meta-models/muse-spark-open-1026`) | Scene reading, the second waterline, verification of every wheel and water mask, voice notes | Running |
| SAM 2.1 hiera-small | CPU, inside the container | Wheel, person and water masks; its video predictor tracks floating debris for surface speed | Running (video speed is experimental) |
| SAM 3.1 | Meta Model API adapter; loader in the Modal SAM service | Masks, and tracking debris by noun phrase instead of by box | Written behind the same interface, not running |
| Muse Spark 1.3 | Meta Model API adapter | Scene reading on the hosted path | Written behind the same interface, not running |

Muse Spark Open is the only reasoning model in the system, and it works as a judge as much as a reader: shown two candidate outlines, it has to say which follows the tyre, and why. Its voice prompt accepts notes in twelve languages, mixed or not (Hindi, Tamil, Telugu, Kannada, Malayalam, Bengali, Marathi, Gujarati, Punjabi, Odia, Urdu and English), and [`voice.py`](perception/src/neermattam_perception/voice.py) turns words like "knee deep" into a depth range. That parser is tested on 36 written phrases, three in each language (`perception/tests/test_voice.py`); recognising the speech itself has not been evaluated per language, and in one recorded test a Bengali note came back transcribed in Devanagari and labelled Hindi (the depth word still mapped).

Why the rest is not running: SAM 3.1's weights are gated on Hugging Face and my access has not been approved (downloads answer 403, [docs/MODAL.md](docs/MODAL.md)), and the hosted Meta Model API account cannot make calls until a credit card is added. The adapters are in [`adapters.py`](perception/src/neermattam_perception/adapters.py) and [`sam_local.py`](perception/src/neermattam_perception/sam_local.py) and switch on with `NEERMATTAM_SCENE` and `NEERMATTAM_MASKS`. Muse Voice Transcribe on the hosted API is not written yet. Velocity from video runs on SAM 2.1: Muse Spark Open boxes the floating objects on a key frame and SAM 2.1's video predictor follows each box ([eval/RESULTS_VIDEO.md](eval/RESULTS_VIDEO.md)); with SAM 3.1 the noun phrase would be tracked instead.

## Safety by design

The worst thing this system could do is call a flooded road safe. The rules against that are written down and tested ([docs/ENGINE.md, Safety model](docs/ENGINE.md#safety-model)):

* **Unknown is never safe.** A cell with no report, a stale report or too much uncertainty is shown as unknown, in grey hatching, never as clear.
* **Uncertainty is carried everywhere.** A road closes to a vehicle when depth plus 1.5 sigma, plus growth with age, exceeds its limit: 15 cm for cars, autos and two-wheelers, 30 cm for buses. Old reports never drain away on their own, and conflicting reports widen the estimate instead of averaging out.
* **Routes are advisory**, and every route and alert says so.
* **Shallow-side errors fall back to caution.** A wheel reading shallower than the scene model's estimate beyond its own two-sigma bound is not published; the estimate is returned with the reason. In one run on the real photos (30 October, `eval/out/final3`, described in [eval/RESULTS_REAL.md](eval/RESULTS_REAL.md)) it set aside two readings, one clearly wrong and one possibly right; in the run recorded at the end of that file no wheel outline reached the geometry, so it had nothing to set aside. That is the price of never publishing a falsely shallow depth.
* **Faces are blurred** in the overlay, the verifier's crops and the stored photo, and GPS metadata is stripped.

## Verify any measurement

Each report links a "Verify this measurement" page (`/verify/{measurement_id}`, in all eight languages) that checks the Ed25519 signature and whether the input hash matches the uploaded file. The public keys are at [`/.well-known/neermattam-keys.json`](https://applemacbook6sep2004--neermattam-web-web-serve.us-east.modal.run/.well-known/neermattam-keys.json). The signed record itself is served at `/api/certificates/{measurement_id}`. Anyone can recompute the published depth and sigma from it with the service's own geometry and no model calls:

```sh
cd perception
uv run python -m neermattam_perception.replay cert.json \
  --keys https://applemacbook6sep2004--neermattam-web-web-serve.us-east.modal.run/.well-known/neermattam-keys.json
```

Exit status 0 is a match and 1 a mismatch; a bad signature stops at 2. A live run on a Chennai photo replayed to a match on every recorded number ([docs/VERIFY.md](docs/VERIFY.md)).

## Results

Every number below is copied from the linked file and comes from a script anyone can rerun.

**Synthetic wheels** ([docs/GEOMETRY.md](docs/GEOMETRY.md), section 7): 500 rendered 3D scenes with noisy masks; 78% measured, the rest declined with a reason. Mean absolute error 3.34 cm, median 1.25 cm; 85% of errors within one stated sigma, 95% within two. The engine blocks at depth plus 1.5 sigma, and 7.9% of true depths lay above that line, against an ideal 6.7%.

**Synthetic people** (same file, section 9.7): 394 of 400 figures measured, mean absolute error 6.5 cm, 4.4 cm low on average, 87% within one sigma. The figures use the proportions the method assumes, so this checks the algorithm, not real bodies.

**Eval gate** ([eval/baseline.json](eval/baseline.json)): a smaller fixed-seed run that fails if accuracy, coverage or calibration slips. Wheels 80.5% measured, 3.90 cm mean error, 83.9% within one sigma; people 98.5%, 6.50 cm, 86.5%.

**Real photos** ([eval/RESULTS_REAL.md](eval/RESULTS_REAL.md)): there is no ground truth. Nobody measured the water in these eleven street photos, so nothing here is accuracy. In the run of 30 October two photos were answered by a person measurement and nine by the labelled model estimate, with no wheel measurement. On 1 November 22 more photos chosen for the wheel method were run: one (a motorcycle in Kanchipuram) measured 4.5 cm from its rear wheel with the outline on the tyre, the same in 6 of 6 runs that reused one scene reading but in none of 4 with a fresh one; two more published from outlines that do not follow the tyre, and the shallow-side guard set aside three readings that were too shallow. The file shows every agent step and its reasons.

**Photos across India** ([eval/RESULTS_INDIA.md](eval/RESULTS_INDIA.md), list in [eval/india_photos.json](eval/india_photos.json)): fourteen archival street flood photos from Wikimedia Commons, from 2006 to 2024, in Chennai (4), Mumbai (2), Bengaluru (3), Hyderabad (2) and Kolkata (3), each placed by its camera geotag or a named locality, measured once on 1 November 2026 by the perception service in its deployed configuration. All fourteen were answered by the labelled model estimate (8 to 45 cm, sigma 20 to 30 cm); none produced a wheel or person measurement, and most runs reached the 55 s time budget on this Mac. There is no ground truth. Model cost for the run: USD 0.41. No suitable photo with a known place was found for Delhi or Visakhapatnam. The map shows these as a separate "Archival photos" layer, labelled "Photo from <year>, run through Neermattam on <date>", each with its signed certificate; they never enter the live map.

**Latency and cost** (the run in eval/RESULTS_REAL.md): 7.2 to 50.2 s per photo, median 31.3 s, of which the scene reading took 7.2 to 14.2 s. Baseten cost USD 0.003 to 0.040 per photo.

**Engine** ([docs/ENGINE.md](docs/ENGINE.md), Benchmarks), on an 8 GB M1 MacBook Air: fusing one reading into a cell and its six neighbours takes 1.11 µs, or 46 µs through the full HTTP router. A 3.5 km car route from Velachery to Guindy takes 0.28 ms on a dry map, 1.3 ms with 200 readings and 29 ms with 2,000. These were measured at commit f8e8fb6 and not refreshed since, as the doc explains. All seven city road graphs (9,513,890 directed edges, 254.1 MiB) load in parallel; the largest, Delhi's, parses in 1.1 s and builds in 0.8 s (docs/ENGINE.md, Graph sizes, 31 October).

## Run it yourself

You need Rust, Go, [uv](https://docs.astral.sh/uv/), `osmium` for the road extract, and a Baseten key for Muse Spark Open. Copy [`.env.example`](.env.example) to `.env` and fill in `BASETEN_API_KEY`, `BASETEN_BASE_URL` and `BASETEN_MODEL`.

The road extract is not in git. From the repository root, cut Chennai's with the recipe in [docs/ENGINE.md](docs/ENGINE.md) and Chennai's documented box:

```sh
mkdir -p data/osm
curl -4 -LO https://download.geofabrik.de/asia/india/southern-zone-latest.osm.pbf
osmium extract -s complete_ways -b 80.06,12.79,80.34,13.28 southern-zone-latest.osm.pbf -o chennai-bbox.osm.pbf
osmium tags-filter chennai-bbox.osm.pbf w/highway -o data/osm/chennai-roads.osm.pbf
```

Then start the three services, each in its own terminal:

```sh
cd engine && ENGINE_CITIES=chennai cargo run --release              # 127.0.0.1:8302
cd perception && uv sync --extra local && uv run --extra local neermattam-perception   # 127.0.0.1:8301
cd gateway && DEMO=1 go run ./cmd/neermattam-gateway                 # http://localhost:8080
```

The map opens on real data only: live reports, Open-Meteo rainfall for every city and the measured archival photos. `DEMO=1` also seeds a simulated Cyclone Michaung scenario onto the engine's separate replay layer, which a visitor switches on in Chennai ("Replay a past flood: Cyclone Michaung, December 2023, simulated") and which the public control room demo uses. It never reaches the live map, its numbers or `/api/india`. Tests and the eval gate:

```sh
cd engine && cargo test
cd gateway && go test ./...
cd perception && uv run pytest && uv run python ../eval/gate.py
cd mcp && uv run pytest
```

The public deployment is one command, `modal deploy deploy/modal-web/app.py`; [docs/DEPLOY.md](docs/DEPLOY.md) covers secrets, the daily cap of 150 measurements and the running cost.

## MCP server

[`mcp/`](mcp/README.md) is a Model Context Protocol server, so any AI agent or assistant (Claude Desktop, Cursor, Claude Code or any MCP client) can ask the live site how deep the water is near a point, which route is open for a vehicle, what state a data centre's rings and access routes are in, measure a photo and check a measurement's certificate. It calls the public API (and the developer API when given a key), keeps the same rules (advisory, unknown is never safe, every depth with its sigma) and ships with mocked tests and a live smoke script whose real output is in its README.

```sh
cd mcp && uv sync && uv run neermattam-mcp
```

## Built during the hackathon

The hackathon runs from 26 October to 4 November 2026. This repository was started on 26 October, the first day of the event, and everything in it was written during the event. The first commit is `136b667`, dated 2026-10-26, "Build rules, layout and service contracts"; `git log --reverse` shows the rest.

Third-party pieces I did not write:

| Piece | How it is used | Licence |
|---|---|---|
| MapLibre GL JS 5.6.0 | Vendored in `gateway/internal/web/static/vendor/` | BSD 3-Clause ([LICENSE.txt](gateway/internal/web/static/vendor/maplibre-gl-5.6.0/LICENSE.txt)) |
| Inter typeface | Vendored | SIL Open Font License 1.1 ([licence](gateway/internal/web/static/fonts/LICENSE-Inter.txt)) |
| Plus Jakarta Sans typeface, for headings | Vendored | SIL Open Font License 1.1 ([licence](gateway/internal/web/static/fonts/LICENSE-PlusJakartaSans.txt)) |
| Noto Sans for Indic scripts | Loaded from Google Fonts | SIL Open Font License 1.1 |
| OpenStreetMap road extracts, seven cities | Cut from Geofabrik extracts, not in git | ODbL 1.0 |
| OpenFreeMap base map tiles | Loaded by the map | OpenStreetMap and OpenMapTiles attribution |
| Open-Meteo rainfall | Called at run time | CC BY 4.0 |
| SAM 2.1 hiera-small weights | Downloaded at build time | Apache 2.0 |
| Muse Spark Open | Called through Baseten; no weights in this repository | Meta's terms for the model |
| Evaluation photos | Downloaded by `eval/real_photos.py`, not in git | CC0, CC BY 2.0, CC BY 3.0, CC BY-SA 4.0 and Unsplash License, credited per photo |
| Sample and showcase photos on the site | Four evaluation photos, faces and plates blurred, in `gateway/internal/samples` and `static/showcase` | CC0 1.0, CC BY 2.0, CC BY 3.0 and Unsplash License, credited on the page and in `samples.json` |

Library dependencies are pinned in `engine/Cargo.lock`, `perception/uv.lock` and `mcp/uv.lock`. The gateway uses only Go's standard library.

## Languages and cities

Every page, message and result card is available in English, Tamil, Hindi, Telugu, Kannada, Malayalam, Bengali and Marathi ([docs/GATEWAY.md](docs/GATEWAY.md)). The engine covers seven cities by default (`ENGINE_CITIES`): Chennai, Mumbai, Bengaluru, Hyderabad, Kolkata, Delhi and Visakhapatnam, each with its own road graph. Another city needs only configuration.

The live map opens on real data in every city. Each city shows its modelled rainfall for the last hour and the last 24 hours from Open-Meteo, read every 10 minutes and shown with the time it was read (or "rain data unavailable", never zero), and a strip of all seven cities with their real reports, rainfall and measured photos, one tap apart. A city with no reports says so and points to the cities that have reports or rain. `GET /api/india` gives the same numbers as JSON ([docs/CONTRACTS.md](docs/CONTRACTS.md), section 3).

## Limits and what's next

* No field accuracy yet. The next step is real photos with a measured depth beside them.
* The wheel method rarely measures a real photo yet. Of 33 real photos run so far, one gave a wheel depth from an outline on the tyre, and it does not reproduce with a fresh scene reading. SAM keeps the mudguard with a two-wheeler's tyre and the arch with a car's, and the verifier accepts some of those outlines at low reasoning effort ([eval/RESULTS_REAL.md](eval/RESULTS_REAL.md), first section). The labelled estimate and the person reference answer the rest.
* The scene model's own estimates are uncalibrated, and moved by up to 60 cm between runs on one photo.
* Latency depends on Baseten; on slow days it was two to six times longer.
* No published source was found for the auto, two-wheeler or bus depth limits. The first two borrow the car figure.
* Video speed is experimental: on five real clips none had both a measured reference and tracks good enough for a speed in metres per second ([eval/RESULTS_VIDEO.md](eval/RESULTS_VIDEO.md)).
* Next: SAM 3.1 once access is approved, and one ward pilot with a city control room ([docs/IMPACT.md](docs/IMPACT.md)).

## Licence and data credits

The code is under the [MIT License](LICENSE). Road data is © OpenStreetMap contributors under the ODbL; the attribution for the road extracts, the SAM 2.1 weights and Open-Meteo is in [deploy/hf-space/DATA_LICENSE](deploy/hf-space/DATA_LICENSE), and the About page on the live site credits them too.
