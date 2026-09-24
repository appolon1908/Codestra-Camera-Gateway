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
