# syntax=docker/dockerfile:1
# Neermattam engine: release build, then the binary alone on distroless/cc.
FROM rust:1.97-bookworm AS build
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
COPY src ./src
COPY assets ./assets
COPY benches ./benches
COPY tests ./tests
RUN cargo build --release --locked --bin neermattam-engine \
 && mkdir -p /out/state \
 && cp target/release/neermattam-engine /out/

FROM gcr.io/distroless/cc-debian12:nonroot
COPY --from=build /out/neermattam-engine /neermattam-engine
# Writable snapshot directory owned by the nonroot user (uid 65532).
COPY --from=build --chown=65532:65532 /out/state /var/lib/neermattam
ENV ENGINE_BIND=0.0.0.0:8302 \
    ENGINE_OSM_DIR=/data/osm \
    ENGINE_SNAPSHOT_PATH=/var/lib/neermattam/cells.json
EXPOSE 8302
USER nonroot:nonroot
ENTRYPOINT ["/neermattam-engine"]
