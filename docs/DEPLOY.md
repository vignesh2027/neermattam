# Deploying Neermattam

The public demo runs on Modal as one app, `neermattam-web`, with all three services in a single CPU container:

https://applemacbook6sep2004--neermattam-web-web-serve.us-east.modal.run

**Deployed now:** `main` as of 1 November 2026 (critical sites, seven cities, reason codes in eight languages, video speed), built from a clean worktree with no uncommitted changes. The About page shows the exact commit, read from the container's `/opt/neermattam/BUILD_INFO`.

This page explains how the image is built, how the container is configured, how to update it, and the guards that keep its running cost bounded. A Hugging Face Space recipe is kept beside it (see [Hugging Face Space](#hugging-face-space)); both build the same image.

| Path | Purpose |
|---|---|
| `deploy/hf-space/Dockerfile` | The multi-stage image recipe, shared by both hosts. Everything above the line `# ---- Hugging Face Space only below this line ----` is the common image. |
| `deploy/hf-space/launcher/` | A small Go program (standard library only, with tests) that starts, restarts and stops the three services, guards the model budget and fronts the gateway. |
| `deploy/hf-space/push.py` | Lays out the build context from the working tree. Modal reuses its `stage()` function. |
| `deploy/modal-web/app.py` | The Modal app. |
| `deploy/hf-space/DATA_LICENSE` | Licence and attribution for the road extracts and the SAM weights. |

## How the image is built

`modal deploy deploy/modal-web/app.py` stages the build context into a temporary folder (the engine, gateway and perception sources as they are in the checkout it runs from, the launcher, the Dockerfile cut at the Space-only line, `DATA_LICENSE`, `BUILD_INFO` and the seven road extracts) and has Modal build it with `Image.from_dockerfile`. Modal caches each step, so a change to one service rebuilds only the steps after it. The stages:

1. **Engine.** `rust:1.97-bookworm` runs `cargo build --release --locked` on `engine/`.
2. **Gateway, key tool and launcher.** `golang:1.26-bookworm` runs `go test ./...` and builds static binaries of the gateway, `neermattam-keys` and the launcher. A failing test fails the build. One gateway test is skipped: `TestEmbeddedFactsMatchTheDocs` compares the landing page's numbers with `docs/` and `eval/RESULTS_REAL.md`, which the build context does not include. (Its recorded run now lives in git at `gateway/internal/facts/testdata/x2.json`, so it passes on a clean checkout; in the image it would skip itself anyway, so the `-skip` in the Dockerfile is no longer needed.)
3. **Weights.** `facebook/sam2.1-hiera-small` (Apache-2.0, not gated) is downloaded at one pinned revision, keeping only the four files `transformers` reads. Nothing is downloaded at run time; `HF_HUB_OFFLINE=1` makes sure of it.
4. **Runtime.** `python:3.12-slim-bookworm`. Perception's dependencies are installed from its `uv.lock` at the locked versions, except that `torch` and `torchvision` come from the PyTorch CPU index at the same versions, without the NVIDIA libraries the PyPI builds pull in. The build ends by importing perception and checking that torch has no CUDA. The binaries, the weights and the road extracts are copied in.

`BUILD_INFO` in the image records the full source commit, its subject, whether the services or the deploy files had uncommitted changes, and when the context was staged. The gateway reads it at start (`BUILD_INFO_PATH`, default `/opt/neermattam/BUILD_INFO`) and the About page shows the commit, linked on GitHub, with a note when uncommitted changes were included.

**Always deploy from a clean checkout.** Other work may be in progress in the main working tree, and whatever is there is what gets built. Make a worktree of the commit, copy in any deploy-file changes, deploy from it and remove it:

```sh
git -C /Users/vignesh/neermattam worktree add /Users/vignesh/nm-deploy <commit>
cd /Users/vignesh/nm-deploy && modal deploy deploy/modal-web/app.py
git -C /Users/vignesh/neermattam worktree remove /Users/vignesh/nm-deploy
```

The seven road extracts, `<city>-roads.osm.pbf` for Chennai, Mumbai, Bengaluru, Hyderabad, Kolkata, Delhi and Visakhapatnam (ODbL, 54 MB together since the Chennai re-cut of 31 October 2026, checked with `push.py --stage-dir`), are not in the GitHub repository. The staging step takes them from the first of `data/osm/` and `~/neermattam-data/osm/` that holds all seven (`CITIES` in `deploy/hf-space/push.py`). The About page credits OpenStreetMap contributors under the ODbL, and `DATA_LICENSE` is in the image at `/opt/neermattam/DATA_LICENSE`.

## What runs in the container

`app.py` defines one class, `Web`, with `min_containers=1` and `max_containers=1`, so one container is always warm and visitors never wait for a cold start. It asks for 8 CPU cores (perception runs SAM 2.1 with 8 threads; at 2 cores a live measurement ran out of its 55 s budget) and 5 GiB of memory, with a hard limit of 8 GiB, and accepts up to 400 concurrent requests (`@modal.concurrent`), since every open map holds a live stream. On start it runs the launcher, which starts:

| Process | Address | Notes |
|---|---|---|
| Front proxy | `0.0.0.0:8000` | Inside the launcher. Modal's `@modal.web_server(8000)` routes the public URL here. It rewrites `X-Forwarded-For` (see below) and passes everything else, including server-sent events, straight through. |
| Gateway | `127.0.0.1:7860` | Answers `/` as soon as it starts, without waiting for perception to load SAM. |
| Engine | `127.0.0.1:8302` | Loads all seven cities' road graphs at start (`ENGINE_OSM_DIR`); ready once Chennai's is loaded. Cell snapshot and history on the Volume. |
| Perception | `127.0.0.1:8301` | SAM 2.1 small on the CPU, loaded and warmed up before it serves. Muse Spark Open on Baseten for the scene reading and the mask checks. |
| Budget guard | `127.0.0.1:8300` | Inside the launcher. The gateway's `PERCEPTION_URL` points here; it forwards to perception and counts model calls. |

The launcher restarts any process that exits (after 1 second, doubling up to 30 seconds), tags each output line with the process name, logs `service answering` and `service ready` with the milliseconds since the container started (the cold start), and logs each process's resident memory and the container's total every 2 minutes. On SIGTERM it stops the gateway first so it drains open requests, then the others, and kills anything still running after 25 seconds. Each service sees only the secrets it uses: perception the model keys, the gateway the control-room key and the Web Push private key, the engine none.

A redeploy starts the new container while the old one keeps serving, so a deploy does not take the site down.

### Durable state

The Modal Volume `neermattam-web-data` is mounted at `/data`:

- `GATEWAY_DATA_DIR=/data/gateway`: which cells hold real reports (`provenance.json`), street alerts, API keys and their daily usage;
- `ENGINE_SNAPSHOT_PATH=/data/engine/cells.json`: the engine's cell snapshot and, beside it, the depth history.

The services write these files with atomic renames; `app.py` commits the Volume every 30 seconds and once more after the services have stopped. Checked on 29 October 2026: after a redeploy the new container served the same demo API key with its usage count, and the cells of the real report made before it were still labelled `mixed` (real next to simulated). The first deploy with the replay layer (1 November 2026) starts on a snapshot that still holds the old simulated water in the live map. The gateway leaves out every cell its saved provenance record holds as simulated only, and the engine expires that evidence after 12 hours and forgets it after 48; to start clean at once, delete `engine/cells.json`, `engine/cells.history.json` and `engine/cells.ledger.json` from the Volume before deploying (real reports made before that would be lost from the map too). During a redeploy both containers run for a short while and the new one reads the Volume when it starts, so anything the old one writes in its last seconds can be lost.

## Configuration

Secrets come from the Modal Secret `neermattam-web`, created from the repository `.env` (whose values are never printed):

```sh
python3 -c 'import json; print(json.dumps({k: v for k, v in (l.split("=", 1) for l in open(".env").read().splitlines() if "=" in l and not l.startswith("#")) if v}))' > /tmp/s.json
modal secret create neermattam-web --from-json /tmp/s.json --force && rm /tmp/s.json
```

Leave `DEMO_API_KEY` out of that JSON: it is a client credential, not a server setting. (`modal secret create --from-dotenv` needs `python-dotenv` in the CLI's environment; the JSON route avoids that.) The secret holds:

- `BASETEN_API_KEY`, `BASETEN_BASE_URL`, `BASETEN_MODEL`: Muse Spark Open;
- `NEERMATTAM_SIGNING_KEY`: the Ed25519 seed perception signs measurement certificates with (docs/VERIFY.md); the launcher passes it to perception only. Added to `.env` on 30 October 2026, so the secret must be recreated from `.env` before the next deploy, or the deployed perception signs with a key that lasts only until it restarts;
- `CONTROL_ROOM_KEY`: unlocks `/control` (48 random hex characters, made with `openssl rand -hex 24`); `/control/demo` stays public;
- `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`: the Web Push key pair for street alerts (P-256, base64url), generated once and kept in `.env` so subscriptions stay valid across redeploys;
- `MODAL_SAM_URL`, `MODAL_SAM_TOKEN`: for perception's `sam2.1-modal` masks. The image sets `NEERMATTAM_MASKS=sam2.1-local`, so they are not used now.

### The demo API key

One developer key, "Hackathon demo", 120 requests a minute, was made inside the running container so it lands in the Volume, and its secret was written straight into `.env` as `DEMO_API_KEY` without being printed:

```sh
modal container exec <container-id> -- sh -c 'GATEWAY_DATA_DIR=/data/gateway /opt/neermattam/bin/neermattam-keys create -name "Hackathon demo" -rate 120'
modal container exec <container-id> -- sh -c 'GATEWAY_DATA_DIR=/data/gateway /opt/neermattam/bin/neermattam-keys list'
```

The key is printed once by `create`; capture it into `.env` rather than onto the screen. `list`, `usage`, `disable`, `enable` and `delete` work the same way, and the gateway picks up a change within 2 seconds.

Settings read by `app.py` when you run `modal deploy` (export them first to change them):

| Variable | Default | Meaning |
|---|---|---|
| `NEERMATTAM_WEB_CPU` | `8` | CPU cores for the container |
| `NEERMATTAM_WEB_MEMORY_MB`, `NEERMATTAM_WEB_MEMORY_LIMIT_MB` | `5120`, `8192` | Memory request and hard limit |
| `NEERMATTAM_WEB_PROXY_HOPS` | `1` | Trusted `X-Forwarded-For` entries, counted from the right |
| `NEERMATTAM_WEB_DAILY_CAP` | `150` | Perception calls per day across all visitors |
| `NEERMATTAM_WEB_PUBLIC_URL` | the Modal URL above | `PUBLIC_URL`: Open Graph tags, the API terms link, the VAPID contact |

Defaults baked into the image (from the shared part of the Dockerfile): `DEMO=1` (seeds the labelled simulated Michaung scenario on the engine's replay layer, which visitors switch on in Chennai and the public control room demo reads; since 1 November 2026 the default map shows real data only), `TRUST_PROXY=1`, `REPORT_RATE_PER_MIN=2`, `REPORT_BURST=3`, `MAX_INFLIGHT_REPORTS=2`, `PERCEPTION_TIMEOUT=240s`, `ENGINE_OSM_DIR=/opt/neermattam/data/osm`, `NEERMATTAM_SCENE=muse-spark-open`, `NEERMATTAM_MASKS=sam2.1-local`, `NEERMATTAM_SAM_SIZE=small`, and `OMP_NUM_THREADS=2` (the sandbox reports 18 CPUs, so torch would otherwise start far more threads than the 2 cores it is given).

### The perception timeout

The gateway gives perception 240 seconds. Its reply deadline (`REPLY_DEADLINE`, 4 minutes 30 seconds from the start of the request) leaves 30 seconds after that for the upload, verification and fusion, so a report that runs out of time gets an error message rather than a dropped connection.

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

Rechecked on 29 October 2026 after the six-city deploy, with the same result. On Modal, a plain request arrives with `X-Forwarded-For` holding just the visitor's public address, from a Modal-internal connection, and `X-Real-IP` empty, so one hop is right. This was checked on the live app: six report posts, each with a different forged `X-Forwarded-For`, got 400, 400, 400 (the burst of 3, refused only for having no file), then 429, 429, 429; a forged two-entry chain was refused with 429 too.

### Cancelling a report

When a visitor leaves, or the gateway's perception timeout expires, the gateway cancels its request. The guard sees its caller go away, closes the connection to perception and logs `caller went away; perception request cancelled`. Perception's stream endpoint then cancels the measurement task, so no further Baseten calls start. A SAM pass that is already running in a worker thread cannot be interrupted and finishes first (a few seconds on this hardware). This was checked with a stand-in perception that streams for two minutes: it saw its caller disconnect 4 seconds into the call when the visitor left after 3 seconds, and 9 seconds in when an 8 second gateway timeout expired. A cancelled call still counts against the daily cap, since part of it may already have been billed.

### Other limits

- The site is served from Modal's `us-east` region.
- Modal bills a container for the larger of what it asks for and what it uses. At the rates Modal published on 28 October 2026 ($0.0473 per core hour and $0.008 per GiB hour), 2 cores and 5 GiB held all day come to 24 x (2 x 0.0473 + 5 x 0.008) = $3.23 a day, before Baseten (the 4 GiB request used until 1 November cost $3.04). One core would bring it to about $2.10 a day, with slower SAM passes. The Volume holds a few MB, which costs nothing worth counting at $0.09 per GiB a month.
- Memory, measured in the container on 1 November 2026 with all seven graphs loaded (`/proc/<pid>/status` and the memory cgroup, through `modal container exec`): 2.1 GiB idle (2173 MB: engine 631 MB, perception 1450 MB, gateway 111 MB), and 3.0 GiB after one real photo report (3117 MB: perception 2396 MB, gateway 141 MB, engine 632 MB). On 29 October, with six graphs, it was 2.2 GiB idle and 2.6 GiB after a report; perception now grows more during a report. The gateway lets two reports run at once; if a second grew perception by as much as this one did (0.9 GiB, an estimate from the one report measured), the container would pass 4 GiB, so the request was raised from 4 GiB to 5 GiB and the hard limit from 6 GiB to 8 GiB on 1 November 2026. Baseten is billed separately; its cost per report is in each response's `cost_usd`, and the daily cap bounds it.

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
for p in / /map /report /route /about /alerts /control /control/demo /developers /readyz; do
  curl -s -o /dev/null -w "$p %{http_code}\n" "$U$p?lang=ta"; done
curl -s "$U/api/route?from=18.9398,72.8355&to=19.0544,72.8406&vehicle=car" | head -c 300   # one city of seven
curl -s -H "Authorization: Bearer $DEMO_API_KEY" "$U/api/v1/route?from=13.0827,80.2707&to=12.9791,80.2209&vehicle=car" | head -c 300
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
