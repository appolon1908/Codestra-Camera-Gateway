"""SQLite-backed camera registry.

Persists camera configuration, observed health and ONVIF probe metadata. Only secret
*references* are stored; a defensive check refuses to persist anything that looks like a
plaintext credential.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from camera_gateway.errors import CameraNotFound, Conflict, InvalidRequest
from camera_gateway.models import (
    Camera,
    CameraCreate,
    CameraHealth,
    CameraUpdate,
    HealthStatus,
    OnvifProbeResult,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cameras (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    camera_ref TEXT NOT NULL,
    config_json TEXT NOT NULL,
    health_json TEXT NOT NULL,
    onvif_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (tenant_id, camera_ref)
);
CREATE INDEX IF NOT EXISTS cameras_tenant ON cameras (tenant_id, created_at);
"""

_FORBIDDEN_KEYS = {"password", "passwd", "secret", "token", "verification_code"}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _assert_no_plaintext(obj: Any) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key.lower() in _FORBIDDEN_KEYS:
                raise InvalidRequest("Refusing to persist a plaintext credential field")
            _assert_no_plaintext(value)
    elif isinstance(obj, list):
        for value in obj:
            _assert_no_plaintext(value)


class CameraStore:
    def __init__(self, path: Path | str) -> None:
        path = str(path)
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def ping(self) -> bool:
        try:
            with self._lock:
                self._conn.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    # -- mapping -------------------------------------------------------------------

    @staticmethod
    def _row_to_camera(row: sqlite3.Row) -> Camera:
        config = json.loads(row["config_json"])
        return Camera(
            id=row["id"],
            tenant_id=row["tenant_id"],
            camera_ref=row["camera_ref"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            health=CameraHealth.model_validate_json(row["health_json"]),
            onvif_probe=(
                OnvifProbeResult.model_validate_json(row["onvif_json"]) if row["onvif_json"] else None
            ),
            **config,
        )

    @staticmethod
    def _config_json(data: CameraCreate | dict[str, Any]) -> str:
        payload = data.model_dump(mode="json") if isinstance(data, CameraCreate) else data
        payload = {k: v for k, v in payload.items() if k != "camera_ref"}
        _assert_no_plaintext(payload)
        return json.dumps(payload, sort_keys=True)

    # -- CRUD ----------------------------------------------------------------------

    def create(self, tenant_id: str, data: CameraCreate) -> Camera:
        now = _utcnow().isoformat()
        camera_id = f"cam_{uuid.uuid4().hex}"
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO cameras (id, tenant_id, camera_ref, config_json, health_json,"
                    " onvif_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, NULL, ?, ?)",
                    (
                        camera_id,
                        tenant_id,
                        data.camera_ref,
                        self._config_json(data),
                        CameraHealth().model_dump_json(),
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError:
            raise Conflict("camera_ref already registered for this tenant") from None
        return self.get(tenant_id, camera_id)

    def get(self, tenant_id: str, camera_id: str) -> Camera:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM cameras WHERE id = ? AND tenant_id = ?", (camera_id, tenant_id)
            ).fetchone()
        if row is None:
            raise CameraNotFound()
        return self._row_to_camera(row)

    def list(self, tenant_id: str, limit: int, offset: int) -> tuple[list[Camera], int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM cameras WHERE tenant_id = ? ORDER BY created_at, id LIMIT ? OFFSET ?",
                (tenant_id, limit, offset),
            ).fetchall()
            total = self._conn.execute(
                "SELECT COUNT(*) FROM cameras WHERE tenant_id = ?", (tenant_id,)
            ).fetchone()[0]
        return [self._row_to_camera(r) for r in rows], total

    def update(self, tenant_id: str, camera_id: str, patch: CameraUpdate) -> Camera:
        current = self.get(tenant_id, camera_id)
        merged = current.model_dump(
            mode="json",
            include={"name", "vendor", "model", "host", "rtsp", "onvif", "credentials", "enabled", "labels"},
        )
        merged.update(patch.model_dump(mode="json", exclude_unset=True))
        # Re-validate the whole resulting configuration with the create contract.
        validated = CameraCreate.model_validate({**merged, "camera_ref": current.camera_ref})
        # Connection-relevant changes invalidate cached health/probe observations.
        reset = any(k in patch.model_fields_set for k in ("host", "rtsp", "onvif", "credentials"))
        with self._lock:
            self._conn.execute(
                "UPDATE cameras SET config_json = ?, updated_at = ? WHERE id = ? AND tenant_id = ?",
                (self._config_json(validated), _utcnow().isoformat(), camera_id, tenant_id),
            )
            if reset:
                self._conn.execute(
                    "UPDATE cameras SET health_json = ?, onvif_json = NULL WHERE id = ? AND tenant_id = ?",
                    (CameraHealth().model_dump_json(), camera_id, tenant_id),
                )
        return self.get(tenant_id, camera_id)

    def delete(self, tenant_id: str, camera_id: str) -> None:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM cameras WHERE id = ? AND tenant_id = ?", (camera_id, tenant_id)
            )
        if cur.rowcount == 0:
            raise CameraNotFound()

    # -- observations ----------------------------------------------------------------

    def save_health(self, tenant_id: str, camera_id: str, health: CameraHealth) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE cameras SET health_json = ? WHERE id = ? AND tenant_id = ?",
                (health.model_dump_json(), camera_id, tenant_id),
            )

    def save_probe(self, tenant_id: str, camera_id: str, probe: OnvifProbeResult) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE cameras SET onvif_json = ? WHERE id = ? AND tenant_id = ?",
                (probe.model_dump_json(), camera_id, tenant_id),
            )

    def status_counts(self, tenant_id: str) -> tuple[int, int, dict[HealthStatus, int]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT config_json, health_json FROM cameras WHERE tenant_id = ?", (tenant_id,)
            ).fetchall()
        counts = {s: 0 for s in HealthStatus}
        enabled = 0
        for row in rows:
            counts[CameraHealth.model_validate_json(row["health_json"]).status] += 1
            enabled += bool(json.loads(row["config_json"]).get("enabled"))
        return len(rows), enabled, counts

    def global_status_counts(self) -> dict[HealthStatus, int]:
        with self._lock:
            rows = self._conn.execute("SELECT health_json FROM cameras").fetchall()
        counts = {s: 0 for s in HealthStatus}
        for row in rows:
            counts[CameraHealth.model_validate_json(row["health_json"]).status] += 1
        return counts
