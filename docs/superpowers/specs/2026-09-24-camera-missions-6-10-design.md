# Camera Gateway Missions 6–10 design

Status: draft for review; implementation has not started.

## Baseline and authority

The prerequisite was verified before making this draft: commit
`0dfa3c0c31c213a5d9518d0cbdc24eb11513455d` contains Missions 1–5, and
`git status --porcelain` was empty. Continue on
`mission/face-id-middleware-integration-20260924`.

The gateway remains a standalone camera-connectivity service. Middleware V3
owns cross-system policy, authorization, command governance, and reconciliation.
New private commands require service authentication, tenant scope, camera scope,
and the existing durable command-reference discipline. Public contracts never
accept media paths, camera URLs, or secret values. Do not merge the branch.

## Approach

Recommended: extend the existing SQLite registry and private media adapter with
separate recording and observation modules. Reuse media admission limits,
ONVIF transport, secret resolution, structured errors, and bounded metrics.
Avoid growing the existing Missions class into the owner of every subsystem.

A metadata-only implementation would be smaller but could not demonstrate actual
pre-event availability. An external recorder would supply richer recording but
introduce a new deployment dependency. Use a small opt-in local segment recorder
with explicit coverage limitations instead.

## 6. Bounded rolling buffer and pre-event requests

Add a global disabled-by-default buffer flag and private per-camera start/stop
commands. Enabling the global flag alone does not start recording. A bounded
background task captures finite MPEG-TS segments from the registered stream,
using the existing RTSP adapter and admission controls. Stop tasks on shutdown,
camera disable/deletion, connection configuration changes, or global disablement.
Do not automatically resume recording after process restart; report stopped
state and reconcile through Middleware.

Use operator-configured limits for global bytes, segment bytes, segment duration,
per-camera rolling duration, retention age, segment count, and recorder count.
Reserve capacity before capture, including in-progress writes, and evict oldest
eligible segments before admitting work. Reject work when protected data or
in-flight reservations leave insufficient capacity. Separate rolling storage
from existing event clips; document the combined storage upper bound.

Use service-generated IDs and private directories with restrictive permissions.
Do not expose filenames or serve bytes through these routes. Atomic completion,
startup reconciliation, periodic cleanup, and deletion cleanup remove expired,
orphaned, and partial files and reconcile database metadata. Cleanup and clip
selection share a storage lock. File operations reject symlink substitution.

Read-only buffer metadata reports state, configured limits, stored/reserved bytes,
segment count, oldest/newest capture times, last error code, and coverage gaps.
Capture wall-clock intervals are observations, not verified playback duration.

A private pre-event request contains command_ref, an aware event timestamp,
bounded pre-event duration, and bounded byte budget. Its result is a retained
manifest of opaque segment IDs, requested interval, observed covered intervals,
gaps, and expiry. It does not claim a precisely trimmed playable clip. Retention
of selected segments counts against the same quota; manifest expiry releases
protection. Empty coverage is an explicit unavailable result, never fabricated
recording. Retries return the original manifest or an expired/not-found response,
and do not capture new footage. Post-event recording remains the existing clip
capture operation in this increment.

## 7. Motion and scene events

Implement the permitted simple frame-delta path first, using a bounded grayscale
thumbnail decoded from observed snapshots. Compare only compatible frames from
the same camera configuration. Do not treat compressed file size as pixel motion.
Sampling is opportunistic on successful snapshots; the event feed does not imply
continuous surveillance. Camera-native subscription support is deferred.

Normalize events into a separate schema with monotonic event ID, camera ID,
observed time, kind (motion/scene_change), source (frame_delta), heuristic=true,
normalized change score, confidence_kind=heuristic, and algorithm version.
The score is a signal strength, not a calibrated probability or person detection.
Apply bounded sampling/debounce and configurable thresholds. Bound persisted
events by age and count per camera, and clear frame baselines on configuration
changes, disablement, and deletion.

Expose an authenticated, tenant-scoped read-only feed with bounded limit and
cursor pagination. Use a stable sequence boundary so concurrent insertion does
not shift pages. Expired history is explicitly distinguishable from a complete
history. No public event injection endpoint.

## 8. Inventory and firmware metadata

Reuse GetDeviceInformation and add an allowlisted capability summary from
GetCapabilities. Return manufacturer, model, firmware version, optional serial
and hardware ID, and bounded capability flags. Do not return raw SOAP, arbitrary
vendor extension fields, credential-bearing addresses, or service URLs.

Persist the last successful inventory independently of media profiles, so a
missing media service does not erase useful device information. Expose a
read-only inventory endpoint and private refresh operation. Include last attempt,
last successful refresh, stale flag, configured freshness interval, and sanitized
last error. Unknown and unsupported capabilities remain distinct from false.
Failed refresh preserves the last good result and accurately reports its age.

## 9. Credential rotation readiness

Extend the resolver boundary with optional non-secret metadata: version,
lease expiration, renewable flag when known, and next rotation due. The file
resolver cannot infer an OpenBao lease or version from file mtime. It reports
unknown unless an explicitly documented, bounded agent-rendered metadata sidecar
supplies these allowlisted fields. Never return lease IDs, tokens, secret bytes,
hashes of secret bytes, raw sidecar contents, or filesystem locations.

Read-only credential status reports configured/unconfigured state, metadata
availability, the version associated with the last successful authentication,
last authentication attempt/result, and rotation due/unknown status. Last success
is protocol-specific; ONVIF success does not prove RTSP authentication succeeds.
Metadata lookup alone must not resolve or authenticate with a secret.

A private reload/re-auth command resolves references afresh and performs a bounded
read-only authentication check against each configured protocol. Return per-protocol
outcomes; partial success is explicit. Resolution and metadata must represent a
consistent rendered version, or version is reported unknown. Do not label a
concurrently changed reference as authenticated. Invalidate stale connection
observations and record new results only for the checked configuration.

The operation does not rotate credentials in OpenBao or mutate camera accounts.
There is no gateway secret cache to refresh today. Existing long-running media
operations finish with their original credentials; subsequent operations resolve
the current reference. Bound concurrent reload operations and preserve sanitized
errors on failures.

## 10. Clock and stale-time health

Expose an explicit read-only ONVIF clock sample from GetSystemDateAndTime. Record
camera UTC, gateway request midpoint UTC, round-trip duration, signed drift
(camera minus gateway), and measurement uncertainty. Use monotonic elapsed time
to detect gateway clock discontinuities during a request. Do not substitute
gateway receipt time for missing camera time.

Persist last attempt and last valid sample. Derive unknown/healthy/drifted/stale
health from configurable drift and freshness thresholds; preserve unsupported,
malformed, and failed sample distinctions. Expose a read-only status endpoint
and private sample operation. Clock sample success does not count as successful
credential authentication when the camera permits unauthenticated time reads.

The existing sync_clock method adjusts only the client's WS-Security timestamp.
Refactor it to reuse clock parsing without changing that behavior. Never issue
SetSystemDateAndTime or any other device clock mutation.

## Persistence, concurrency, and observability

Use additive SQLite tables and retain compatibility with existing camera, clip,
PTZ, and health contracts. Scope new observations to the camera configuration
that produced them. Updates invalidate affected observations; late operations
cannot repopulate state for changed or deleted cameras. Recording remains subject
to the existing single-process-per-registry/volume deployment constraint.

Add aggregate recording bytes/reservations/segments/active-recorder gauges,
eviction and rejection counters, normalized-event counters, inventory and
credential-check outcomes, and clock sample/drift observations. Labels use
bounded enums only: no camera/tenant IDs, references, serials, paths, or secrets.

## Verification and delivery

Add contract, adapter, persistence, and lifecycle tests for each mission. Cover
tenant isolation, disabled defaults, unknown fields, invalid ranges/timestamps,
credential redaction, malformed device data, stale results, and restart behavior.
Exercise recording quotas including concurrent admission, protected segments,
retention, cancellation, disk failures, orphan cleanup, and camera deletion.
Verify feed pagination during insertion/retention, heuristic labeling, reference
changes during re-authentication, and that no clock mutation request is sent.
Use deterministic fake clocks and existing ONVIF/process fakes where possible.
Exercise real FFmpeg recording and decoding with synthetic media when available.

Update README, ARCHITECTURE, settings/Compose examples, capabilities, metrics,
and generated OpenAPI. Keep all recording flags false in default deployment.
Run ruff, pytest, OpenAPI parity, Docker build, Compose config and a local smoke
test, and git diff --check. Review the final diff, commit and push on the existing
branch, and verify a clean worktree and remote commit. Report any environmental
verification limitation explicitly. No merge.
