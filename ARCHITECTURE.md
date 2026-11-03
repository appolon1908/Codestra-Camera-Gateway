# Codestra-Camera-Gateway Architecture

## Authority

This service stands on its own and owns only its own runtime, data, and API contract.

Middleware V3 is the sole cross-system integration authority.

Canonical path:

    Caller -> Caddy -> Kong -> Middleware V3 -> Codestra Camera Gateway API

Rules:
- no shared database writes across repositories
- no direct app-to-app provider calls
- no duplicated business authority
- fail closed for production effects
- idempotent commands for effectful operations
- health, readiness, metrics, audit, and OpenAPI are first-class
- secrets are OpenBao references and are never committed
- public exposure is denied unless explicitly routed through Caddy/Kong/Middleware

## API boundary

Service-local API prefix: /v1/cameras

Required operational endpoints:
- GET /healthz
- GET /readyz
- GET /health/ready (Middleware V3 readiness alias)
- GET /metrics
- GET /v1/capabilities

Middleware owns authentication normalization, caller policy, orchestration, command ledger/outbox, retries, reconciliation, and cross-system audit.

## Service-local camera missions

`mission_models.py`, `mission_store.py`, `missions.py`, and `api/missions.py` extend
camera connectivity with tenant-scoped groups/memberships, external-zone coverage
metadata, private PTZ/clip commands, and health observations. Metadata uses the registry's
SQLite connection and lock; camera/group foreign keys cascade memberships and coverage.
There is no FACE-ID zone ownership, Odoo synchronization, or provider call from these tables.
The authoritative API remains the exact generated `openapi.yaml`.

ONVIF discovers PTZ profiles/configuration options/presets and reduces device-advertised
endpoints to paths on the registered host. Only bounded absolute normalized positions,
advertised presets, and Stop are supported. A per-camera in-process motion guard and
cancellation-shielded Stop provide bounded best-effort control, not a hardware safety
certification. Stop failures are visible as failed commands; no automatic retry creates
new effects. Middleware must reconcile uncertain hardware state.

PTZ and clip capture require explicit enablement and token auth, and live under
`/v1/internal`. Private ingress must restrict these routes to Middleware's worker.
A durable local reservation makes each tenant/camera/command_ref at-most-once across
restarts; it complements, but does not replace, Middleware's command ledger. Pending or
failed reservations are intentionally not reclaimed automatically. Run one process per
registry/volume; in-process admission and motion guards are not distributed locks.

Clips use registered RTSP targets only. The adapter streams MPEG-TS into a bounded process
output buffer, enforces a hard wall deadline, then writes a generated private file and
metadata. Cleanup shares the registry lock with file publication. The API has no file
serving or arbitrary source/destination parameters. Startup and periodic cleanup remove
expired and orphaned media. File count, per-file bytes, retention, and media concurrency
bound storage/runtime use. Clip bytes use the existing non-root persistent volume.

Health history is bounded to 200 observations per camera and counters use fixed signal
labels. Frame staleness reflects observation age; compressed-size change and repeated
JPEG digests are explicitly heuristic. No identity, liveness, or certain tamper decision
is produced. Existing OpenBao resolution, redacted upstream errors, private metrics and
non-root container boundaries remain in force.
