# Neermattam perception

Python service that measures floodwater depth (and, from a short video, surface velocity) from one photo, using a vehicle wheel of known size as the ruler. A Muse Spark model reads the scene, SAM segments the wheel, rim and water, and the geometry in `geometry.py` turns the masks into a depth with an uncertainty. The method and its accuracy are in `../docs/GEOMETRY.md`; the HTTP contract is section 1 of `../docs/CONTRACTS.md`.

There are two model paths. The hosted path uses Muse Spark 1.3 and SAM 3.1 on the Meta Model API in one segmentation request. The open-weights path uses Muse Spark Open (Meta's open-weight model, served by Baseten) and SAM 2.1, and adds the verify-and-refine agent (`agent.py`):

* the scene model gives each wheel a box, 3 or 4 points on the tyre rubber around its arc, and negative points on the hub and on what touches the tyre (exhaust, mudguard, wall); the box always goes to SAM with those points;
* SAM's proposals (for the box with the points, and for the points alone) are closed into solid wheel shapes and ranked by how well their outline fits one ellipse (`wheel_shape.py`), a judge that costs milliseconds;
* the best two are shown side by side to Muse Spark Open, which picks one and judges it, or returns corrected boxes and points (at most two corrections); an outline is measured only when the model calls it good on a close crop, its shape score passes, and its box agrees with the tyre box the model draws.

Every step is returned in `agent_trace`, and `POST /v1/measure/stream` sends each step as it happens. `POST /v1/transcribe` reads voice notes in the major Indian languages, mixed or not: it detects the languages, normalises the place for geocoding, and turns the depth words into a range with the phrase table in `voice.py` (a spoken number first, the model's own reading last). When no wheel can be measured, the scene model's own estimate is returned as `method: model_estimate` with a wide sigma.

SAM 2.1 runs on the Modal service (`deploy/modal-sam`, hiera-large; the default when `MODAL_SAM_URL` is set, with local hiera-small as the fallback when Modal fails) or locally (`small`, `base-plus` or `large`, fp32 or fp16). SAM 3.1 drops in by configuration (`NEERMATTAM_MASKS`) when its weights are available. The comparison of the sizes on real photos is in `../eval/RESULTS_SAM_AB.md`.

```bash
uv sync --extra local              # the local extra adds torch, torchvision, transformers (SAM 2.1)
uv run pytest                      # all tests; no network (live tests are skipped)
NEERMATTAM_LIVE=1 uv run --extra local pytest tests/test_live.py   # a few cents of Baseten credit
uv run ruff check src tests && uv run mypy src
uv run --extra local neermattam-perception   # serves on 127.0.0.1:8301
uv run python ../eval/synthetic.py --cases 500 --seed 2026 --draws 100   # synthetic evaluation
uv run python ../eval/synthetic.py --calibrate                          # refit the model tables
uv run python ../eval/sigma_methods.py --cases 40 --workers 1           # batched sigma against Monte Carlo
uv run --extra local python ../eval/real_photos.py                      # real Commons photos, live
uv run --extra local python ../eval/sam_ab.py                           # SAM sizes on the real photos, live
```

| Module | Role |
|---|---|
| `geometry.py` | ellipse and tyre-outline fitting, waterline, depth, Monte Carlo sigma, typed failures |
| `references.py` | tyre-code diameters and the vehicle catalogue, with sources |
| `velocity.py` | surface and depth-averaged velocity from debris tracks |
| `sam.py` | SAM 3.1 requests, prompt grammar, stream parsing (official `meta-sam-parser`), mask placement |
| `adapters.py` | `SceneModel` and `MaskModel`; Meta Model API adapter |
| `muse_open.py` | Muse Spark Open on Baseten: scene reading, mask checks, voice notes; JSON extraction and repair, retries, token cost |
| `sam_local.py` | SAM 2.1 run locally (loaded once, embedding reused, small objects segmented zoomed in, all proposals per prompt); SAM 3.1 adapter slots |
| `modal_sam.py` | SAM 2.1 on the Modal service, and the fallback to the local model |
| `voice.py` | depth words in Hindi, Tamil, Telugu, Kannada, Malayalam, Bengali, Marathi, Gujarati, Punjabi, Odia, Urdu and English, mapped to depth ranges (the documented phrase table) |
| `wheel_shape.py` | the cheap judge: closes tyre rings into solid wheels and scores how well an outline fits a wheel |
| `agent.py` | the verify-and-refine agent and its trace |
| `pipeline.py` | image or video in, `MeasureResponse` out, with the overlay |
| `service.py` | FastAPI app: `/v1/measure`, `/v1/measure/stream` (server-sent events), `/v1/transcribe`, `/healthz`, `/readyz` |
| `media.py` | upload limits, EXIF orientation and capture time, metadata stripping |
| `synth.py`, `synth_eval.py` | synthetic 3D scenes and the evaluation |

Environment: `BASETEN_API_KEY`, `BASETEN_BASE_URL`, `BASETEN_MODEL` (the open-weights path; read from the repository `.env` when not set), `MODEL_API_KEY` (the hosted models), `NEERMATTAM_SCENE` (`muse-spark-open` or `muse-spark-1.3`; by default the open model when a Baseten key is present), `NEERMATTAM_MASKS` (`sam2.1-modal`, `sam2.1-local`, `sam2.1-local:large`, `sam2.1-local:large:fp16`, `sam3.1-api` or `sam3.1-local`), `MODAL_SAM_URL` and `MODAL_SAM_TOKEN` (the Modal SAM service), `NEERMATTAM_MODELS_DIR` (local SAM checkpoints outside the Hugging Face cache, default `~/neermattam-data/models`), `NEERMATTAM_STORE_DIR` (optional; keeps metadata-free uploads and responses), `PERCEPTION_HOST`, `PERCEPTION_PORT`. Outgoing Baseten connections are bound to IPv4, because some networks advertise IPv6 that does not route.
