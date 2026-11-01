# syntax=docker/dockerfile:1
# Neermattam on a Hugging Face Docker Space: engine (Rust), gateway (Go) and
# perception (Python) in one image, run by a small Go launcher.
#
# This file is the Space's Dockerfile. push.py copies it to the root of the
# Space repository next to engine/, gateway/, perception/, launcher/ and
# data/osm/, so every COPY below is relative to that layout.

# 1. Engine: release build of the Rust service.
FROM rust:1.97-bookworm AS engine
WORKDIR /src
COPY engine/Cargo.toml engine/Cargo.lock ./
COPY engine/src ./src
COPY engine/assets ./assets
COPY engine/benches ./benches
COPY engine/tests ./tests
RUN cargo build --release --locked --bin neermattam-engine \
 && cp target/release/neermattam-engine /neermattam-engine

# 2. Gateway and launcher: static Go binaries, tests run first.
FROM golang:1.26-bookworm AS go
WORKDIR /src/gateway
COPY gateway/go.mod ./
RUN go mod download
COPY gateway/ ./
# TestEmbeddedFactsMatchTheDocs compares the landing page's embedded numbers
# with eval/out/x2.json, a recorded run that git does not track (eval/out is
# ignored), so it cannot run in a clean build context. Every other gateway
# test gates the build.
RUN CGO_ENABLED=0 go test -skip '^TestEmbeddedFactsMatchTheDocs$' ./... \
 && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/neermattam-gateway ./cmd/neermattam-gateway \
 && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/neermattam-keys ./cmd/neermattam-keys
WORKDIR /src/launcher
COPY launcher/ ./
RUN CGO_ENABLED=0 go test ./... \
 && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/neermattam-launcher .

# 3. SAM 2.1 small weights (Apache-2.0, ungated), pinned to one revision, so
# a cold start never downloads anything.
FROM python:3.12-slim-bookworm AS weights
RUN pip install --no-cache-dir "huggingface_hub>=0.34,<3"
RUN python -c "from huggingface_hub import snapshot_download; snapshot_download('facebook/sam2.1-hiera-small', revision='ee5bba1d82bb8749febdf90f45e84b687142ba03', allow_patterns=['config.json', 'model.safetensors', 'preprocessor_config.json', 'processor_config.json'], local_dir='/models/sam2.1-hiera-small')" \
 && rm -rf /models/sam2.1-hiera-small/.cache \
 && ls -la /models/sam2.1-hiera-small

# 4. Runtime: Python 3.12 with perception, CPU-only torch, and the two binaries.
FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_NO_CACHE=1 \
    UV_LINK_MODE=copy
RUN apt-get update \
 && apt-get install -y --no-install-recommends libglib2.0-0 ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir uv==0.11.14

# Perception's locked dependencies, with torch and torchvision taken from the
# PyTorch CPU index at the locked versions instead of the CUDA builds on PyPI
# (which pull in about 3 GB of NVIDIA libraries a CPU Space cannot use).
WORKDIR /opt/neermattam/perception
COPY perception/pyproject.toml perception/uv.lock perception/README.md ./
RUN uv export --frozen --no-dev --extra local --no-emit-project --no-hashes -o /tmp/locked.txt \
 && TORCH=$(sed -n 's/^torch==\([^ ;]*\).*/\1/p' /tmp/locked.txt) \
 && VISION=$(sed -n 's/^torchvision==\([^ ;]*\).*/\1/p' /tmp/locked.txt) \
 && test -n "$TORCH" && test -n "$VISION" \
 && grep -v -E '^(torch|torchvision|triton)==|^(nvidia|cuda)-' /tmp/locked.txt > /tmp/rest.txt \
 && uv venv /opt/venv --python /usr/local/bin/python3.12 \
 && uv pip install --python /opt/venv/bin/python --no-deps \
      --index-url https://download.pytorch.org/whl/cpu "torch==${TORCH}+cpu" "torchvision==${VISION}+cpu" \
 && uv pip install --python /opt/venv/bin/python --no-deps -r /tmp/rest.txt \
 && rm /tmp/locked.txt /tmp/rest.txt
COPY perception/src ./src
RUN uv pip install --python /opt/venv/bin/python --no-deps . \
 && /opt/venv/bin/python -c "import torch, transformers, neermattam_perception.service as s; assert not torch.cuda.is_available(); print('torch', torch.__version__, 'transformers', transformers.__version__)"

COPY --from=weights /models /opt/neermattam/models
COPY --from=engine /neermattam-engine /opt/neermattam/bin/neermattam-engine
COPY --from=go /out/neermattam-gateway /out/neermattam-keys /out/neermattam-launcher /opt/neermattam/bin/
# One road extract per city: chennai, mumbai, bengaluru, hyderabad, kolkata, delhi, visakhapatnam.
COPY data/osm/ /opt/neermattam/data/osm/
# BUILD_INFO names the source commit; the gateway's About page reads it
# (BUILD_INFO_PATH, internal/buildinfo).
COPY DATA_LICENSE BUILD_INFO /opt/neermattam/

# Shared configuration. Platform settings (Space variables, Modal
# secrets) of the same name override these at run time. BASETEN_API_KEY is
# a secret and never appears here.
ENV DEMO=1 \
    TRUST_PROXY=1 \
    REPORT_RATE_PER_MIN=2 \
    REPORT_BURST=3 \
    MAX_INFLIGHT_REPORTS=2 \
    PERCEPTION_DAILY_CAP=150 \
    PERCEPTION_TIMEOUT=240s \
    ENGINE_OSM_DIR=/opt/neermattam/data/osm \
    NEERMATTAM_SCENE=muse-spark-open \
    NEERMATTAM_MASKS=sam2.1-local \
    NEERMATTAM_SAM_SIZE=small \
    NEERMATTAM_MODELS_DIR=/opt/neermattam/models \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    OMP_NUM_THREADS=2 \
    MKL_NUM_THREADS=2 \
    ENGINE_LOG_FORMAT=json

# ---- Hugging Face Space only below this line ----
# deploy/modal-web/app.py builds everything above it and adds its own entry.

# Spaces run the container as uid 1000.
RUN useradd -m -u 1000 user
USER user
WORKDIR /home/user
ENV PORT=7860 \
    PUBLIC_URL=https://vigneshwar234-neermattam.hf.space
EXPOSE 7860
ENTRYPOINT ["/opt/neermattam/bin/neermattam-launcher"]
