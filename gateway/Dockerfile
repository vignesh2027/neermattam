# syntax=docker/dockerfile:1
# Neermattam gateway: static Go binary on a distroless base.
FROM golang:1.26-alpine AS build
WORKDIR /src
COPY go.mod ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go test ./... \
 && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/neermattam-gateway ./cmd/neermattam-gateway

# distroless/static has CA certificates (for Open-Meteo and the Graph API),
# no shell and runs as an unprivileged user.
FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/neermattam-gateway /neermattam-gateway
ENV GATEWAY_ADDR=:8080
EXPOSE 8080
USER nonroot:nonroot
ENTRYPOINT ["/neermattam-gateway"]
