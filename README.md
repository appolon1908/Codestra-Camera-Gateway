# Codestra Camera Gateway

Standalone ONVIF / RTSP camera connectivity service for Codestra.

## Authority

This repository owns camera discovery, registration, secret references, ONVIF metadata, RTSP connectivity, snapshots, stream tests, and bounded operator preview. It does **not** own identity decisions, liveness, access-control policy, CRM data, or cross-system orchestration.

Canonical cross-system path:

```
Caddy -> Kong -> Middleware V3 :8095 -> Codestra Camera Gateway API
```

The service remains independently runnable and testable.

## API

Operational endpoints:

- `GET /healthz`
- `GET /readyz`
- `GET /health/ready` — Middleware V3 readiness alias
- `GET /metrics`
- `GET /v1/capabilities`

Camera endpoints include:

- vendor-neutral camera CRUD with secret references only
- ONVIF discovery and profile probe
- RTSP stream test
- JPEG snapshot
- bounded MJPEG operator preview
- camera health/status and media metadata

See `openapi.yaml` for the exact contract. CI tests require the committed specification to match the runtime-generated OpenAPI.

## Credential boundary

Plaintext camera passwords are rejected by the API contract and must never be returned by the service. Camera records contain an OpenBao-compatible secret reference; the runtime resolves that reference privately.

Do not put credentials in URLs, Git, logs, metrics, exception bodies, or API responses.

## EZVIZ and vendor support

EZVIZ H8c conventions are supported through the vendor profile layer, while the runtime remains based on ONVIF/RTSP abstractions so additional camera vendors can be added without changing callers.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pip install -e .
.venv/bin/python -m ruff check .
.venv/bin/python -m pytest -q
.venv/bin/python -m scripts.export_openapi --check
```

The integration suite includes real ffmpeg/ffprobe failure classification using local fake RTSP endpoints.

## Container

```bash
docker build -t codestra/camera-gateway:dev .
docker compose -f docker-compose.dev.yml config
```

The production container runs as a non-root user and stores only service-local camera metadata. Public ingress must never expose credentials, `/metrics`, or unrestricted preview streams.

## Promotion

```
development -> testing -> staging -> production
```

Cross-system activation still requires Middleware V3 connector conformance, private ingress, workload identity, and environment-specific release evidence.
