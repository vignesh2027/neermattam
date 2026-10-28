# SAM on Modal

Perception's local SAM 2.1 hiera-small runs on the CPU or Apple MPS. It is slow and its masks are the weakest link in the depth measurement. `deploy/modal-sam/` moves mask prediction to a GPU on Modal and serves the larger SAM 2.1 hiera-large checkpoint over a small authenticated HTTP API. Everything here is Python and uses Meta models only.

## Status on 28 October 2026

* **The GPU deploy is blocked.** `modal deploy` refuses both L4 and T4 functions with "Please add a payment method to use L4 GPU functions." The workspace `applemacbook6sep2004` has no card on file, and the hackathon credit does not lift that requirement (or had not been applied yet). Once a payment method is added in the Modal dashboard, `modal deploy app.py` deploys the L4 version as written. Nothing else needs to change.
* **What is live now** is the same app deployed with `NEERMATTAM_SAM_GPU=cpu`: hiera-large on 4 CPU cores. The API, the token check, the client, RLE and the overlays are all verified end to end against it, and its masks are the same model's masks in fp32. It is slow (about 9 seconds of embedding per photo), so treat it as a working stub until the GPU deploy goes through.
* GPU latency and GPU cost per photo have **not** been measured. The table below gives only measured CPU numbers.

## Files

| File | Purpose |
|---|---|
| `deploy/modal-sam/app.py` | The Modal app `neermattam-sam`: image, GPU class, web front end |
| `deploy/modal-sam/sam_core.py` | Prompt validation, RLE encoding and the model runner. No Modal or FastAPI imports, so the same code runs on a laptop |
| `deploy/modal-sam/client.py` | Python client: image and prompts in, numpy masks out (numpy and the standard library only) |
| `deploy/modal-sam/measure.py` | Latency and overlay measurement on the probe photos, hosted or local |
| `deploy/modal-sam/tests/` | Tests for prompts, RLE and the client (no network, no model) |

## What runs where

The app has two functions.

1. **`web`** is a CPU container (a quarter of a core, 512 MB) running FastAPI. It checks the bearer token and forwards the request to the GPU class. A request without the right token is refused here, so a stranger hitting the URL never starts a GPU. It stays up for 5 minutes after its last request.
2. **`Segmenter`** is the GPU class. Its image has torch 2.14.1 and transformers 5.21.0 (the versions perception locks) and the three SAM 2.1 checkpoints (small, base-plus, large) baked in at build time, so a cold start reads weights from the image instead of downloading them. On start it loads hiera-large. Base-plus and small load on the first request that asks for them. Each request decodes the image once, computes one image embedding, and runs SAM's prompt decoder once per prompt against that embedding, so many objects in one photo cost one embedding.

The GPU is an NVIDIA L4 by default. Modal charges $0.80 an hour for an L4 and $0.59 for a T4, but the L4 is a newer generation (Ada, with TF32 and bfloat16 tensor cores, which the T4 lacks), so it should finish each photo fast enough to cost less per photo. That has not been measured yet; run `measure.py` on both once GPUs are enabled and keep the cheaper one. Set `NEERMATTAM_SAM_GPU=T4` at deploy time to switch. `NEERMATTAM_SAM_GPU=cpu` deploys the same code on 4 CPU cores and 8 GB, which needs no payment method and is only useful for checking the plumbing.

The Mac and the gateway never talk to the GPU directly. They call `web` over HTTPS.

## Cost guards

* `min_containers=0`: nothing runs, and nothing is billed, when there are no requests.
* `scaledown_window=120`: a GPU container stops 2 minutes after its last request. A single isolated photo therefore costs about 2 minutes of GPU time plus the cold start. Change it with `NEERMATTAM_SAM_SCALEDOWN_S` at deploy time.
* `max_containers=2`: at most two GPUs at once, whatever the traffic (`NEERMATTAM_SAM_MAX_CONTAINERS`).
* `timeout=180` seconds per GPU call.
* Unauthenticated requests are answered by the CPU front end and never reach a GPU.
* Requests are capped at 20 MB, 8192 px on the longer side and 32 prompts.
* Check spend with `modal billing report --for today` (per app) and `modal billing summary`.

## Secrets

| Modal secret | Keys | Used by |
|---|---|---|
| `neermattam-sam` | `NEERMATTAM_SAM_TOKEN` | `web`, to check the bearer token |
| `neermattam-hf` | `HF_TOKEN` (optional) | `Segmenter`, to download the gated SAM 3 checkpoints |

The local copies are `MODAL_SAM_URL` and `MODAL_SAM_TOKEN` in the repository's `.env`, which is gitignored. To rotate the token, generate a new one, run `modal secret create neermattam-sam NEERMATTAM_SAM_TOKEN=<new> --force` and update `.env`. Running containers keep the old value until they stop.

## API

Every request needs `Authorization: Bearer <MODAL_SAM_TOKEN>`. A missing or wrong token gets 401.

### GET /health

Starts a model container if none is running and returns what it loaded. This is the real answer from the CPU deployment on 28 October 2026:

```json
{"status": "ok", "default_model": "sam-2.1-hiera-large", "loaded_models": ["sam-2.1-hiera-large"],
 "available_models": ["base-plus", "large", "sam3", "sam3.1", "small"], "gated_access_token": false,
 "device": "cpu", "gpu": null, "default_precision": "fp32", "container_start_ms": 12288}
```

### POST /segment

JSON body:

```json
{
  "image_b64": "<base64 of a JPEG or PNG>",
  "prompts": [
    {"id": "scooters", "box": [560, 580, 700, 660]},
    {"id": "water", "positive_points": [[330, 650]], "negative_points": [[300, 600]]}
  ],
  "model": "large",
  "max_masks": 3,
  "precision": "fp32"
}
```

Or `multipart/form-data` with the file in `image`, the prompt list as a JSON string in `prompts`, and the optional `model`, `max_masks` and `precision` fields.

* Coordinates are pixels of the uploaded image after its EXIF orientation is applied. A box is `[x1, y1, x2, y2]`; points are `[x, y]`. Each prompt needs a box, at least one positive point, or both. Coordinates outside the image are clamped.
* `id` is any string or integer, unique within the request, and is echoed back.
* `model` is `large` (the default), `base-plus`, `small`, or the gated `sam3.1` and `sam3` (see below).
* `max_masks` is 1 to 3. SAM proposes three masks per prompt; they come back sorted by SAM's predicted IoU, best first.
* `precision` is `fp32` (the default, with TF32 matrix multiplies on the GPU) or `bf16`. bfloat16 applies on CUDA only. It is opt-in until it has been measured on the GPU; on the Mac's CPU, bf16 autocast gave masks with an IoU of 0.96 to 0.999 against fp32 on c3.

Response (the c3 scooters box on the CPU deployment, RLE counts shortened):

```json
{
  "model": "sam-2.1-hiera-large", "device": "cpu", "gpu": null, "precision": "fp32",
  "image_size": [900, 675],
  "results": [
    {"id": "scooters", "candidates": [
      {"index": 2, "score": 0.6612, "bbox": [564, 584, 707, 650], "area": 6089,
       "rle": {"size": [675, 900], "counts": ["..."]}}
    ]}
  ],
  "timings_ms": {"embed": 9013.4, "decode": 112.5, "decode_image": 12.5, "encode_rle": 5.7,
                 "gpu_call": 9144.1, "server_total": 10631.2}
}
```

`gpu_call` is the time inside the model container, whatever its hardware; `server_total` adds the front end and the hop between the two containers.

* `bbox` is `[x1, y1, x2, y2]` with `x2` and `y2` exclusive; `area` is the mask's pixel count.
* `rle` is COCO-style uncompressed run-length encoding: `size` is `[height, width]` and `counts` are run lengths over the mask read column by column (Fortran order), starting with a run of zeros (which may be 0). `pycocotools.mask.frPyObjects` reads it directly, and `client.rle_decode` decodes it with numpy.
* Masks are SAM's raw output at full image size. Perception's local adapter keeps only the largest connected component and scores candidates against the prompt box; an adapter over this service should do the same on its side.
* Errors: 401 wrong token, 413 image too large, 415 image cannot be decoded, 422 malformed prompts or options, 503 the requested model cannot be loaded.

### Python client

```python
import sys; sys.path.insert(0, "deploy/modal-sam")
from client import SamPrompt, segment

res = segment("photo.jpg", [SamPrompt("scooters", box=(560, 580, 700, 660)),
                            SamPrompt("water", positive_points=((330, 650),))])
best = res.masks["scooters"][0]   # SamMask: mask (bool H x W), score, bbox, area, index
res.timings_ms, res.round_trip_ms
```

`segment` reads `MODAL_SAM_URL` and `MODAL_SAM_TOKEN` from the environment unless `url` and `token` are passed. It resolves the host over IPv4 only by default, because this Mac's IPv6 does not route (`ipv4=False` turns that off). It is synchronous; an async adapter can call it through `asyncio.to_thread`. A numpy or PIL image is sent as PNG so the pixels match the prompt coordinates exactly.

## SAM 3.1

`facebook/sam3.1` is gated, and the account's Hugging Face token did not have access on 28 October 2026 (downloads answer 403). The code path is in place: `model: "sam3.1"` loads it with `transformers.Sam3TrackerModel` and `Sam3TrackerProcessor`, which take the same box and point prompts as SAM 2.1, using `HF_TOKEN` from the `neermattam-hf` secret, and caches the download in the `neermattam-hf-cache` Modal Volume. Without a token it answers 503 saying so.

One caveat that is visible from the repository's file list: `facebook/sam3.1` currently ships only the original-format checkpoint `sam3.1_multiplex.pt`, not a `model.safetensors`, so `transformers` will most likely refuse it and the service will answer 503 with the loader's message. `facebook/sam3` (SAM 3.0, also gated) does ship `model.safetensors` and is selectable as `model: "sam3"`. Loading `sam3.1_multiplex.pt` would need Meta's `sam3` package in the image; that is the next step once access is granted.

To enable it once access is granted:

```bash
modal secret create neermattam-hf HF_TOKEN=hf_... --force
```

No redeploy is needed; new containers pick up the secret. The first `sam3.1` request downloads about 3.5 GB into the volume.

## Deploying and redeploying

From the repository root, with the Modal CLI authenticated (`~/.local/bin/modal`):

```bash
cd deploy/modal-sam
modal deploy app.py                            # L4, the default
NEERMATTAM_SAM_GPU=T4 modal deploy app.py      # or a T4
```

The first deploy builds the image (torch and the three checkpoints, a few minutes); later deploys reuse it unless the pinned packages change. Editing `sam_core.py` or `app.py` does not rebuild it. The URL stays `https://applemacbook6sep2004--neermattam-sam.us-east.modal.run`. To stop everything, `modal app stop neermattam-sam`.

Tests:

```bash
cd deploy/modal-sam && python -m pytest -q tests
```

Measurement (writes overlays next to the probe photos and prints JSON):

```bash
python deploy/modal-sam/measure.py                  # hosted, hiera-large
python deploy/modal-sam/measure.py --local small    # the same prompts locally
```

## Measured results, 28 October 2026

These figures come from `measure.py` and the CPU deployment. They are not GPU figures.

| | Hosted hiera-large, 4 CPU cores | Local hiera-small, Mac MPS |
|---|---|---|
| Cold first request (c3, 1 prompt), round trip | 38.9 s (12.3 s of it loading the model) | not applicable (model load 10.1 s) |
| Warm, 1 prompt, round trip | 11.6 to 17.3 s | 1.3 to 1.8 s |
| Warm, 6 prompts, round trip | 12.3 to 18.1 s | 1.3 to 3.1 s |
| Image embedding | about 9.0 s | 0.07 to 0.4 s |
| Prompt decoding, 6 prompts | 0.5 to 0.7 s | 0.9 to 1.2 s |

The Mac was heavily loaded by other jobs during these runs, so the local numbers swing a lot; treat them as rough. Six prompts cost barely more than one on both, because the embedding is shared.

**Cost on the CPU deployment.** A warm request uses about 9.5 s of a container with 4 cores and 8 GB, which at Modal's rates ($0.0473 per core hour, $0.008 per GB hour) is about $0.00067, or about $0.07 per 100 photos sent back to back. Each idle tail after the last request adds up to 2 minutes of that container (about $0.008). GPU cost per photo is not measured yet.

**Spend.** Building, the CPU deployment and all the tests above came to $0.14 of Modal usage on 28 October.

**Mask quality** (overlays in `~/neermattam-data/probe/`):

* `modal_cpu_c2.png`, `modal_cpu_c1.png`: hiera-large gives clean masks on the rider, covered scooter, parked bike, car, people and the tree, and one point puts a water mask over the whole street.
* `modal_cpu_c3.png` against `local_small_c3.png`: large's water mask runs the full width of the street to x 886 under the scooters; small's stops at x 785.
* `modal_cpu_c3_scooters.png` against `local_small_c3_scooters.png`: with the loose box [560,580,700,660] alone, small's best candidate is the wall behind the scooters. Large's best (score 0.66) is scooters plus a blob of wall, and its third candidate traces the scooters. Neither is usable from that box alone.
* `modal_cpu_c3_scooters_points.png`: adding two positive points on the scooters and one negative point on the wall gives large a clean two-scooter mask (score 0.77, area 2759 px) that stops at the waterline. The perception adapter should send a point with every box.
* `modal_cpu_c4.png` against `local_small_c4.png`: on the rooftop crowd (one box over about twenty people) large's best scores only 0.11 and is sparse, while small covers the crowd at 0.59. A box over many separate objects is a bad prompt for either model.
