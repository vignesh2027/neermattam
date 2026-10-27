# Neermattam perception

Python service that measures floodwater depth (and, from a short video, surface velocity) from one photo, using a vehicle wheel of known size as the ruler. Muse Spark 1.3 reads the scene, SAM 3.1 segments the wheel, rim and water, and the geometry in `geometry.py` turns the masks into a depth with an uncertainty. The method and its accuracy are in `../docs/GEOMETRY.md`; the HTTP contract is section 1 of `../docs/CONTRACTS.md`.

```bash
uv sync
uv run pytest                      # all tests; no network
uv run ruff check src tests && uv run mypy src
uv run neermattam-perception       # serves on 127.0.0.1:8301 (MODEL_API_KEY from the environment)
uv run python -m neermattam_perception.synth_eval            # 500-case synthetic evaluation
uv run python -m neermattam_perception.synth_eval --calibrate  # refit the model-error table
```

| Module | Role |
|---|---|
| `geometry.py` | ellipse and tyre-outline fitting, waterline, depth, Monte Carlo sigma, typed failures |
| `references.py` | tyre-code diameters and the vehicle catalogue, with sources |
| `velocity.py` | surface and depth-averaged velocity from debris tracks |
| `sam.py` | SAM 3.1 requests, prompt grammar, stream parsing (official `meta-sam-parser`), mask placement |
| `adapters.py` | `SceneModel` and `MaskModel`; Meta Model API adapter; open-weights stub |
| `pipeline.py` | image or video in, `MeasureResponse` out, with the overlay |
| `service.py` | FastAPI app: `/v1/measure`, `/healthz`, `/readyz` |
| `media.py` | upload limits, EXIF orientation and capture time, metadata stripping |
| `synth.py`, `synth_eval.py` | synthetic 3D scenes and the evaluation |

Environment: `MODEL_API_KEY` (required for the hosted models), `NEERMATTAM_STORE_DIR` (optional; keeps metadata-free uploads and responses), `PERCEPTION_HOST`, `PERCEPTION_PORT`.
