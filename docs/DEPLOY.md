# Deploying Neermattam

The public demo runs on Modal as one app, `neermattam-web`, with all three services in a single CPU container:

https://applemacbook6sep2004--neermattam-web-web-serve.us-east.modal.run

This page explains how the image is built, how the container is configured, how to update it, and the guards that keep its running cost bounded. A Hugging Face Space recipe is kept beside it (see [Hugging Face Space](#hugging-face-space)); both build the same image.

| Path | Purpose |
|---|---|
| `deploy/hf-space/Dockerfile` | The multi-stage image recipe, shared by both hosts. Everything above the line `# ---- Hugging Face Space only below this line ----` is the common image. |
| `deploy/hf-space/launcher/` | A small Go program (standard library only, with tests) that starts, restarts and stops the three services, guards the model budget and fronts the gateway. |
| `deploy/hf-space/push.py` | Lays out the build context from the working tree. Modal reuses its `stage()` function. |
| `deploy/modal-web/app.py` | The Modal app. |
| `deploy/hf-space/DATA_LICENSE` | Licence and attribution for the Chennai roads extract and the SAM weights. |

## How the image is built

`modal deploy deploy/modal-web/app.py` stages the build context into a temporary folder (the engine, gateway and perception sources as they are in the working tree, the launcher, the Dockerfile cut at the Space-only line, `DATA_LICENSE`, `BUILD_INFO` and the roads extract) and has Modal build it with `Image.from_dockerfile`. Modal caches each step, so a change to one service rebuilds only the steps after it. The stages:

1. **Engine.** `rust:1.97-bookworm` runs `cargo build --release --locked` on `engine/`.
2. **Gateway and launcher.** `golang:1.26-bookworm` runs `go test ./...` and builds a static binary for each. A failing test fails the build. One gateway test is skipped: `TestModelEstimateIsLabelledAndCountsForLess` pins a photo capture time of 27 October 2026, 10:00 UTC, which the 24 hour freshness rule refuses from 28 October, 10:00 UTC onwards, so it fails on every later date. It should be fixed in `gateway/` and the skip removed.
3. **Weights.** `facebook/sam2.1-hiera-small` (Apache-2.0, not gated) is downloaded at one pinned revision, keeping only the four files `transformers` reads. Nothing is downloaded at run time; `HF_HUB_OFFLINE=1` makes sure of it.
4. **Runtime.** `python:3.12-slim-bookworm`. Perception's dependencies are installed from its `uv.lock` at the locked versions, except that `torch` and `torchvision` come from the PyTorch CPU index at the same versions, without the NVIDIA libraries the PyPI builds pull in. The build ends by importing perception and checking that torch has no CUDA. The binaries, the weights and the roads extract are copied in.

`BUILD_INFO` in the image records the source commit and whether uncommitted changes were included.

The roads extract, `chennai-roads.osm.pbf` (ODbL), is not in the GitHub repository. The staging step takes it from `data/osm/` or `~/neermattam-data/osm/`. The About page credits OpenStreetMap contributors under the ODbL, and `DATA_LICENSE` is in the image at `/opt/neermattam/DATA_LICENSE`.

## What runs in the container

`app.py` defines one class, `Web`, with `min_containers=1` and `max_containers=1`, so one container is always warm and visitors never wait for a cold start. It asks for 2 CPU cores and 4 GiB of memory, with a hard limit of 6 GiB, and accepts up to 400 concurrent requests (`@modal.concurrent`), since every open map holds a live stream. On start it runs the launcher, which starts:

| Process | Address | Notes |
|---|---|---|
| Front proxy | `0.0.0.0:8000` | Inside the launcher. Modal's `@modal.web_server(8000)` routes the public URL here. It rewrites `X-Forwarded-For` (see below) and passes everything else, including server-sent events, straight through. |
| Gateway | `127.0.0.1:7860` | Answers `/` as soon as it starts, without waiting for perception to load SAM. |
| Engine | `127.0.0.1:8302` | Loads the Chennai road graph at start. Snapshot in `/tmp`, which does not survive a restart. |
| Perception | `127.0.0.1:8301` | SAM 2.1 small on the CPU, loaded and warmed up before it serves. Muse Spark Open on Baseten for the scene reading and the mask checks. |
| Budget guard | `127.0.0.1:8300` | Inside the launcher. The gateway's `PERCEPTION_URL` points here; it forwards to perception and counts model calls. |

The launcher restarts any process that exits (after 1 second, doubling up to 30 seconds), tags each output line with the process name, logs `service answering` and `service ready` with the milliseconds since the container started (the cold start), and logs each process's resident memory and the container's total every 2 minutes. On SIGTERM it stops the gateway first so it drains open requests, then the others, and kills anything still running after 25 seconds. The engine and gateway never see `BASETEN_API_KEY` or `MODAL_SAM_TOKEN`.

A redeploy starts the new container while the old one keeps serving, so a deploy does not take the site down.

## Configuration

Secrets come from the Modal Secret `neermattam-web`, created from the repository `.env` (whose values are never printed):

```sh
python3 -c 'import json; print(json.dumps({k: v for k, v in (l.split("=", 1) for l in open(".env").read().splitlines() if "=" in l and not l.startswith("#")) if v}))' > /tmp/s.json
modal secret create neermattam-web --from-json /tmp/s.json --force && rm /tmp/s.json
```

(`modal secret create --from-dotenv` needs `python-dotenv` in the CLI's environment; the JSON route avoids that.) It holds `BASETEN_API_KEY`, `BASETEN_BASE_URL` and `BASETEN_MODEL`, and also `MODAL_SAM_URL` and `MODAL_SAM_TOKEN`. Those last two reach perception's environment untouched, ready for the adapter that will call the GPU SAM app `neermattam-sam`; perception does not use them yet.

Settings read by `app.py` when you run `modal deploy` (export them first to change them):

| Variable | Default | Meaning |
|---|---|---|
| `NEERMATTAM_WEB_CPU` | `2` | CPU cores for the container |
| `NEERMATTAM_WEB_MEMORY_MB`, `NEERMATTAM_WEB_MEMORY_LIMIT_MB` | `4096`, `6144` | Memory request and hard limit |
| `NEERMATTAM_WEB_PROXY_HOPS` | `1` | Trusted `X-Forwarded-For` entries, counted from the right |
| `NEERMATTAM_WEB_DAILY_CAP` | `150` | Perception calls per day across all visitors |
| `NEERMATTAM_WEB_PUBLIC_URL` | empty | Used in WhatsApp replies, which are not configured here |

Defaults baked into the image (from the shared part of the Dockerfile): `DEMO=1` (the labelled simulated Michaung scenario keeps the map alive; real reports are fused next to it and outlined), `TRUST_PROXY=1`, `REPORT_RATE_PER_MIN=2`, `REPORT_BURST=3`, `MAX_INFLIGHT_REPORTS=2`, `PERCEPTION_TIMEOUT=230s`, `NEERMATTAM_SCENE=muse-spark-open`, `NEERMATTAM_MASKS=sam2.1-local`, `NEERMATTAM_SAM_SIZE=small`, and `OMP_NUM_THREADS=2` (the sandbox reports 18 CPUs, so torch would otherwise start far more threads than the 2 cores it is given).

### Why the perception timeout is 230 seconds

The gateway gives perception 230 seconds, up from the 180 second default. It is not 240 because the gateway also sets a fixed write deadline of 4 minutes from the start of every non-streaming request (`gateway/internal/app/middleware.go`). A report that took the full 240 seconds in perception would reach that deadline before its reply was written, and the visitor would see a dropped connection instead of an error message. That leaves 10 seconds for the upload, verification and fusion; a large photo sent over a slow connection can still run into the deadline. Going to 240 needs a one-line change in the gateway to make that deadline longer than the perception timeout.

Modal does not cut long requests on this endpoint: a live map stream held open for 260 seconds stayed connected.

## Cost guards and limits

The only per-use cost is Muse Spark Open on Baseten. Each photo report is one streamed call to perception, which makes several Baseten requests inside it (the scene reading, then a check of each mask). The limits, in order:

1. **Per visitor.** The gateway allows 2 reports a minute per client address with a burst of 3, and answers 429 with `Retry-After` beyond that. Uploads are size-limited, and exact or near duplicate photos are refused for 24 hours.
2. **At once.** At most 2 reports are measured at the same time.
3. **Per day, for everyone.** The guard counts every `POST` to `/v1/measure`, `/v1/measure/stream` and `/v1/transcribe` and refuses the call once the daily cap is reached. The report page then says: "Live measurements are paused for today: this demo's daily limit of 150 measurements has been used. The live map and water-aware routes still work. Measurements resume at midnight India time." The map, routes, live stream and the simulated scenario never touch Baseten and keep working.

### The daily count survives restarts

The guard writes today's count to `/tmp/neermattam/budget.json` after every call. `app.py` copies that file to the Modal Dict `neermattam-web-state` (key `perception_budget`) every 5 seconds and once more on shutdown, and writes it back before the launcher starts. A redeploy or a container restart therefore carries the count on; at most the calls of the last 5 seconds before a crash can be lost. To read or reset it:

```sh
modal dict get neermattam-web-state perception_budget
modal dict clear neermattam-web-state
```

### The per-IP limit cannot be dodged with a forged header

The gateway rate-limits by the first address in `X-Forwarded-For` when `TRUST_PROXY=1`, so on its own a visitor could send a made-up header and get a fresh allowance each time. The front proxy closes that hole. Each proxy appends the address that connected to it to `X-Forwarded-For`, so only the entries added by the hosting platform can be trusted. The front proxy takes the entry `TRUSTED_PROXY_HOPS` places from the right (one, for Modal), ignores everything a visitor put to the left of it, and sends the gateway a header holding only that address.

On Modal, a plain request arrives with `X-Forwarded-For` holding just the visitor's public address, from a Modal-internal connection, and `X-Real-IP` empty, so one hop is right. This was checked on the live app: six report posts, each with a different forged `X-Forwarded-For`, got 400, 400, 400 (the burst of 3, refused only for having no file), then 429, 429, 429; a forged two-entry chain was refused with 429 too.

### Cancelling a report

When a visitor leaves, or the gateway's perception timeout expires, the gateway cancels its request. The guard sees its caller go away, closes the connection to perception and logs `caller went away; perception request cancelled`. Perception's stream endpoint then cancels the measurement task, so no further Baseten calls start. A SAM pass that is already running in a worker thread cannot be interrupted and finishes first (a few seconds on this hardware). This was checked with a stand-in perception that streams for two minutes: it saw its caller disconnect 4 seconds into the call when the visitor left after 3 seconds, and 9 seconds in when an 8 second gateway timeout expired. A cancelled call still counts against the daily cap, since part of it may already have been billed.

### Other limits

- The engine snapshot is in `/tmp`, so real reports do not survive a restart. Restoring it would be wrong in demo mode anyway: the gateway labels every cell present at start as simulated.
- The site is served from Modal's `us-east` region.
- Modal bills a container for the larger of what it asks for and what it uses. At the rates Modal published on 28 October 2026 ($0.0473 per core hour and $0.008 per GiB hour), 2 cores and 4 GiB held all day come to 24 x (2 x 0.0473 + 4 x 0.008) = $3.04 a day, before Baseten. One core would bring it to about $1.90 a day, with slower SAM passes. Baseten is billed separately; its cost per report is in each response's `cost_usd`, and the daily cap bounds it.

## Updating

```sh
modal deploy deploy/modal-web/app.py
```

Watching it:

```sh
modal app logs neermattam-web
modal container list
modal container exec <container-id> -- cat /opt/neermattam/BUILD_INFO
modal billing summary
```

Checking a deployment:

```sh
U=https://applemacbook6sep2004--neermattam-web-web-serve.us-east.modal.run
for p in / /route /report /about /api/cells /readyz; do curl -s -o /dev/null -w "$p %{http_code}\n" $U$p; done
curl -s -F image=@photo.jpg -F lat=12.9791 -F lon=80.2209 $U/api/report
```

The report API has no way to override a photo's capture time. A photo whose EXIF says it was taken more than 24 hours ago is measured (spending its model calls) and then refused by the freshness rule; a photo with no EXIF date is accepted at a small trust penalty and dated when it arrived.

To take the site down: `modal app stop neermattam-web`.

## Hugging Face Space

`deploy/hf-space/` can also publish the same image as a private Docker Space:

```sh
uv run --with huggingface_hub python deploy/hf-space/push.py --hf-env /path/to/file/with/HF_TOKEN
```

`push.py` writes only to the `vigneshwar234` namespace, keeps the Space private, sets `BASETEN_API_KEY` as a Space secret and uploads the build context as one commit. It was not used: on 28 October 2026 Hugging Face refused to create the Space with `402 Payment Required`, because Docker Spaces on free CPU hardware now need a PRO subscription. In that image the gateway listens on port 7860 directly, without the front proxy, so its per-IP limit there would depend on how the Hugging Face proxy builds `X-Forwarded-For`; set `FRONT_ADDR` (and move `PORT`) to put the front proxy in front, as on Modal. The daily count is held only in memory there.
