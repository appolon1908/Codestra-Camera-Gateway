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

## Local groups and coverage

All `/v1` routes require the service bearer token and camera resources require
`X-Tenant-ID` asserted by Middleware. Group and coverage routes also require that tenant
header. These tables live in the gateway SQLite registry; they never call or write
FACE-ID, Odoo, or another service's database.

- `POST/GET /v1/camera-groups`, `GET/PUT/DELETE /v1/camera-groups/{group_id}`.
  Create/replace body: `{"name":"Entrance","tags":["north"]}`.
- `GET /v1/camera-groups/{group_id}/members`,
  `PUT/DELETE /v1/camera-groups/{group_id}/members/{camera_id}`. Membership changes
  are idempotent; deleting a group or camera removes its memberships.
- `POST/GET /v1/cameras/{camera_id}/coverage`,
  `GET/PUT/DELETE /v1/cameras/{camera_id}/coverage/{coverage_id}`.
  Body: `{"zone_ref":"external-zone-ref","view_name":"North door","priority":10,
  "enabled":true,"tags":["entry"]}`. `zone_ref` is an opaque external handle,
  not a gateway-owned zone. Priority is 0–1000. PUT replaces the entire metadata record.

Lists accept `limit` (1–200, default 50) and `offset`. Cross-tenant lookups return 404.

## Private PTZ contract

`GET /v1/cameras/{camera_id}/ptz` discovers ONVIF PTZ configuration, compatible
normalized absolute-position spaces, and preset tokens. Commands use
`POST /v1/internal/cameras/{camera_id}/ptz/commands`; enable explicitly with
`CAMERA_GATEWAY_ENABLE_PTZ=true`. Effectful routes refuse auth-disabled mode even
in development. Keep `/v1/internal/*` reachable only by the private Middleware worker;
never expose it through public ingress.

Example body: `{"command_ref":"move-123","action":"absolute","pan":0.2,
"tilt":-0.3,"zoom":0.5,"timeout_s":2}`. Pan/tilt are paired, in [-1, 1]; zoom is
in [0, 1]. Only the device's matching standard ONVIF position spaces are accepted.
`{"command_ref":"preset-123","action":"preset","preset":"home","timeout_s":2}`
requires an advertised preset. `{"command_ref":"stop-123","action":"stop"}`
issues Stop, including for a disabled camera whose ONVIF configuration is still enabled.

No ContinuousMove API exists. Motion windows are 0.1–5 seconds. Every attempted
absolute/preset move sends Stop in a cancellation-shielded finally block. Stop has a
separate bounded ONVIF deadline; failed move/Stop acknowledgements fail the command.
Completion means acknowledgements received, not that a physical position was verified.
Network loss, process death, or faulty hardware can prevent a Stop from reaching a motor;
use camera hardware limits and Middleware reconciliation before production activation.

Run one gateway process/worker per registry/clip volume (the supplied entrypoint does
this). A per-camera guard serializes movement; an explicit Stop bypasses it. Middleware
owns caller policy, orchestration, command ledger/outbox, and cross-system audit.
The gateway keeps durable at-most-once `command_ref` reservations scoped by tenant and
camera across PTZ and clips. Identical completed retries return the original result;
changed, in-flight, failed, or uncertain commands return 409 without repeating an effect.
A failed validation after reservation also consumes that reference. Never automatically
retry with a new reference after an uncertain outcome. Reservations intentionally survive
camera deletion and have no automatic expiry; do not prune them while callers may retry.

## Private event clips

Enable `CAMERA_GATEWAY_ENABLE_CLIP_CAPTURE=true` and call
`POST /v1/internal/cameras/{camera_id}/clips` with
`{"command_ref":"event-123","duration_s":5,"max_bytes":8388608}`.
Capture starts at request time from the registered camera stream; there is no pre-event
buffer. Video is remuxed to MPEG-TS, with audio omitted. Duration is 1–30 seconds and
bytes are 1 KiB–32 MiB; requests above configured caps are rejected. Output overflow,
malformed/empty output, timeout, and cancellation terminate/reap ffmpeg without publishing
a partial clip. The wall-clock deadline is duration + RTSP timeout + 5 seconds.

`GET /v1/cameras/{camera_id}/clips` lists metadata;
`GET/DELETE /v1/cameras/{camera_id}/clips/{clip_id}` reads metadata or removes a clip.
No route accepts source URLs or filesystem paths, returns a storage path, or serves clip
bytes. Files use generated IDs in `<database directory>/clips`, directory mode 0700 and
file mode 0600. Treat this volume as private camera data. Metadata reports requested
duration, capture timestamps, bytes, media type, and expiry; it does not claim verified
playback duration or pre-event coverage. Captures are serialized and share media admission
limits with other stream operations.

Configuration (all with `CAMERA_GATEWAY_` prefix):

| Setting | Default | Hard bound |
| --- | --- | --- |
| `CLIP_MAX_DURATION_S` | 30 | 1–30 |
| `CLIP_MAX_BYTES` | 33554432 | 1024–33554432 |
| `CLIP_MAX_FILES` | 100 | 1–1000 |
| `CLIP_RETENTION_S` | 3600 | 60–86400 |
| `FRAME_STALE_S` | 60 | 1–86400 |

The file count is global to the gateway, limiting retained storage to count × per-clip
cap (plus one in-memory capture). Expired/deleted-camera clips and orphan files are
removed at startup, every 30 seconds, and before clip reads/capture. Camera deletion also
triggers cleanup. A retry after clip expiry/deletion returns 404 and never recaptures.
An unexpected crash may leave an orphan file until restart; orphan files are not exposed.

## Health history and heuristic signals

`GET /v1/cameras/{camera_id}/health/history?limit=50` returns newest-first observations
(maximum 200 retained per camera). Camera operations record online, offline, auth failure,
protocol error, and other error signals. Existing health/media endpoints remain available.
`camera_gateway_health_signals_total{signal="..."}` exposes bounded-label Prometheus
counters; `/metrics` remains private and must never be routed by public ingress.

Staleness means no newly observed snapshot within `FRAME_STALE_S`, checked every 30
seconds and on history reads, after at least one successful snapshot. It does not prove a
stream stopped; there is no background polling of camera frames. Snapshot recovery emits
`frame_recovered`. Four identical sampled JPEGs emit `repeated_frame_heuristic`; a >50%
change in compressed JPEG size emits `scene_change_heuristic`. Both are coarse sampling
heuristics with false positives (including static scenes or compression changes), not
computer-vision tamper certainty. No image bytes are retained for these heuristics; only
an in-memory digest, size and repeat count are kept. Baselines reset on process restart.
