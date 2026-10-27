# Neermattam perception

Python service that measures floodwater depth (and, from a short video, surface velocity) from one photo, using a vehicle wheel of known size as the ruler. A Muse Spark model reads the scene, SAM segments the wheel, rim and water, and the geometry in `geometry.py` turns the masks into a depth with an uncertainty. The method and its accuracy are in `../docs/GEOMETRY.md`; the HTTP contract is section 1 of `../docs/CONTRACTS.md`.

There are two model paths. The hosted path uses Muse Spark 1.3 and SAM 3.1 on the Meta Model API in one segmentation request. The open-weights path uses Muse Spark Open (Meta's open-weight model, served by Baseten) and SAM 2.1 (`facebook/sam2.1-hiera-small`, run locally), and adds the verify-and-refine agent (`agent.py`): no mask is measured until Muse Spark Open has looked at it on a crop, and a wrong mask is re-prompted with corrected boxes and points, at most twice. Every step is returned in `agent_trace`. When no wheel can be measured, the scene model's own estimate is returned as `method: model_estimate` with a wide sigma. SAM 3.1 drops in by configuration (`NEERMATTAM_MASKS`) when its weights are available.

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
```

| Module | Role |
|---|---|
| `geometry.py` | ellipse and tyre-outline fitting, waterline, depth, Monte Carlo sigma, typed failures |
| `references.py` | tyre-code diameters and the vehicle catalogue, with sources |
| `velocity.py` | surface and depth-averaged velocity from debris tracks |
| `sam.py` | SAM 3.1 requests, prompt grammar, stream parsing (official `meta-sam-parser`), mask placement |
| `adapters.py` | `SceneModel` and `MaskModel`; Meta Model API adapter |
| `muse_open.py` | Muse Spark Open on Baseten: scene reading, mask checks, voice notes; JSON extraction and repair, retries, token cost |
| `sam_local.py` | SAM 2.1 run locally (loaded once, embedding reused, small objects segmented zoomed in); SAM 3.1 adapter slots |
| `agent.py` | the verify-and-refine agent and its trace |
| `pipeline.py` | image or video in, `MeasureResponse` out, with the overlay |
| `service.py` | FastAPI app: `/v1/measure`, `/v1/transcribe`, `/healthz`, `/readyz` |
| `media.py` | upload limits, EXIF orientation and capture time, metadata stripping |
| `synth.py`, `synth_eval.py` | synthetic 3D scenes and the evaluation |

Environment: `BASETEN_API_KEY`, `BASETEN_BASE_URL`, `BASETEN_MODEL` (the open-weights path; read from the repository `.env` when not set), `MODEL_API_KEY` (the hosted models), `NEERMATTAM_SCENE` (`muse-spark-open` or `muse-spark-1.3`; by default the open model when a Baseten key is present), `NEERMATTAM_MASKS` (`sam2.1-local`, `sam3.1-api` or `sam3.1-local`), `NEERMATTAM_STORE_DIR` (optional; keeps metadata-free uploads and responses), `PERCEPTION_HOST`, `PERCEPTION_PORT`. Outgoing Baseten connections are bound to IPv4, because some networks advertise IPv6 that does not route.
