"""Tenant-scoped local metadata. Tables share the camera registry's lock/connection."""

import json
import uuid

from camera_gateway.errors import CameraNotFound, Conflict
from camera_gateway.mission_models import (
    Coverage,
    CoverageInput,
    Group,
    GroupInput,
    HealthEvent,
    NormalizedCameraEvent,
    PreEventManifest,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS camera_groups (
 id TEXT PRIMARY KEY, tenant TEXT NOT NULL, body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS camera_memberships (
 group_id TEXT NOT NULL REFERENCES camera_groups(id) ON DELETE CASCADE,
 camera_id TEXT NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
 PRIMARY KEY(group_id, camera_id)
);
CREATE TABLE IF NOT EXISTS camera_coverage (
 id TEXT PRIMARY KEY, tenant TEXT NOT NULL,
 camera_id TEXT NOT NULL REFERENCES cameras(id) ON DELETE CASCADE, body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS private_commands (
 tenant TEXT NOT NULL, camera_id TEXT NOT NULL, command_ref TEXT NOT NULL,
 payload TEXT NOT NULL, result TEXT,
 PRIMARY KEY(tenant, camera_id, command_ref)
);
CREATE TABLE IF NOT EXISTS camera_clips (
 id TEXT PRIMARY KEY, tenant TEXT NOT NULL, camera_id TEXT NOT NULL, body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS health_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, camera_id TEXT NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
 body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rolling_segments (
 id TEXT PRIMARY KEY, tenant TEXT NOT NULL, camera_id TEXT NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
 started_at TEXT NOT NULL, ended_at TEXT NOT NULL, size_bytes INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS pre_event_manifests (
 id TEXT PRIMARY KEY, tenant TEXT NOT NULL, camera_id TEXT NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
 command_ref TEXT NOT NULL, body TEXT NOT NULL,
 UNIQUE(tenant, camera_id, command_ref)
);
CREATE TABLE IF NOT EXISTS normalized_camera_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, tenant TEXT NOT NULL,
 camera_id TEXT NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
 observed_at TEXT NOT NULL, body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS maintenance_state (
 tenant TEXT NOT NULL, camera_id TEXT NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
 body TEXT NOT NULL, PRIMARY KEY(tenant, camera_id)
);
"""


class MissionStore:
    def __init__(self, cameras):
        self.cameras = cameras
        self.conn = cameras._conn
        self.lock = cameras._lock
        with self.lock:
            self.conn.executescript(SCHEMA)

    def groups(self, tenant, limit, offset):
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, body FROM camera_groups WHERE tenant=? ORDER BY id LIMIT ? OFFSET ?",
                (tenant, limit, offset),
            ).fetchall()
        return [Group(id=r["id"], **json.loads(r["body"])) for r in rows]

    def group(self, tenant, group_id):
        with self.lock:
            row = self.conn.execute(
                "SELECT body FROM camera_groups WHERE tenant=? AND id=?", (tenant, group_id)
            ).fetchone()
        if row is None:
            raise CameraNotFound("Group not found")
        return Group(id=group_id, **json.loads(row["body"]))

    def save_group(self, tenant, body: GroupInput, group_id=None):
        if group_id:
            self.group(tenant, group_id)
        else:
            group_id = "grp_" + uuid.uuid4().hex
        with self.lock:
            self.conn.execute(
                "INSERT INTO camera_groups VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                (group_id, tenant, body.model_dump_json()),
            )
        return Group(id=group_id, **body.model_dump())

    def delete_group(self, tenant, group_id):
        self.group(tenant, group_id)
        with self.lock:
            self.conn.execute("DELETE FROM camera_groups WHERE id=? AND tenant=?", (group_id, tenant))

    def members(self, tenant, group_id, limit, offset):
        self.group(tenant, group_id)
        with self.lock:
            rows = self.conn.execute(
                "SELECT camera_id FROM camera_memberships WHERE group_id=? "
                "ORDER BY camera_id LIMIT ? OFFSET ?",
                (group_id, limit, offset),
            ).fetchall()
        return [dict(r) for r in rows]

    def membership(self, tenant, group_id, camera_id, add):
        self.group(tenant, group_id)
        self.cameras.get(tenant, camera_id)
        with self.lock:
            if add:
                self.conn.execute(
                    "INSERT OR IGNORE INTO camera_memberships VALUES (?, ?)", (group_id, camera_id)
                )
            else:
                self.conn.execute(
                    "DELETE FROM camera_memberships WHERE group_id=? AND camera_id=?", (group_id, camera_id)
                )

    def coverage(self, tenant, camera_id, coverage_id=None, limit=50, offset=0):
        self.cameras.get(tenant, camera_id)
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, body FROM camera_coverage WHERE tenant=? AND camera_id=? "
                "AND (? IS NULL OR id=?) ORDER BY id LIMIT ? OFFSET ?",
                (tenant, camera_id, coverage_id, coverage_id, limit, offset),
            ).fetchall()
        result = [Coverage(id=r["id"], camera_id=camera_id, **json.loads(r["body"])) for r in rows]
        if coverage_id and not result:
            raise CameraNotFound("Coverage not found")
        return result[0] if coverage_id else result

    def save_coverage(self, tenant, camera_id, body: CoverageInput, coverage_id=None):
        self.cameras.get(tenant, camera_id)
        if coverage_id:
            self.coverage(tenant, camera_id, coverage_id)
        else:
            coverage_id = "cov_" + uuid.uuid4().hex
        with self.lock:
            self.conn.execute(
                "INSERT INTO camera_coverage VALUES (?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                (coverage_id, tenant, camera_id, body.model_dump_json()),
            )
        return Coverage(id=coverage_id, camera_id=camera_id, **body.model_dump())

    def delete_coverage(self, tenant, camera_id, coverage_id):
        self.coverage(tenant, camera_id, coverage_id)
        with self.lock:
            self.conn.execute("DELETE FROM camera_coverage WHERE id=? AND tenant=?", (coverage_id, tenant))

    def reserve(self, tenant, camera_id, command_ref, payload):
        """Durable at-most-once reservation. Unknown/failed outcomes cannot be retried as new effects."""
        with self.lock:
            row = self.conn.execute(
                "SELECT payload, result FROM private_commands "
                "WHERE tenant=? AND camera_id=? AND command_ref=?",
                (tenant, camera_id, command_ref),
            ).fetchone()
            if row:
                if row["payload"] != payload or row["result"] is None:
                    raise Conflict(
                        "Command reference already used or outcome uncertain; reconcile via Middleware"
                    )
                return json.loads(row["result"])
            self.conn.execute(
                "INSERT INTO private_commands VALUES (?, ?, ?, ?, NULL)",
                (tenant, camera_id, command_ref, payload),
            )
        return None

    def complete(self, tenant, camera_id, command_ref, result):
        with self.lock:
            self.conn.execute(
                "UPDATE private_commands SET result=? WHERE tenant=? AND camera_id=? AND command_ref=?",
                (result.model_dump_json(), tenant, camera_id, command_ref),
            )

    def event(self, camera_id, event: HealthEvent):
        with self.lock:
            self.conn.execute(
                "INSERT INTO health_events(camera_id, body) SELECT ?, ? WHERE EXISTS "
                "(SELECT 1 FROM cameras WHERE id=?)",
                (camera_id, event.model_dump_json(), camera_id),
            )
            self.conn.execute(
                "DELETE FROM health_events WHERE camera_id=? AND id NOT IN "
                "(SELECT id FROM health_events WHERE camera_id=? ORDER BY id DESC LIMIT 200)",
                (camera_id, camera_id),
            )

    def history(self, tenant, camera_id, limit):
        self.cameras.get(tenant, camera_id)
        with self.lock:
            rows = self.conn.execute(
                "SELECT body FROM health_events WHERE camera_id=? ORDER BY id DESC LIMIT ?",
                (camera_id, limit),
            ).fetchall()
        return [HealthEvent.model_validate_json(r["body"]) for r in rows]

    def rolling_segments(self, tenant, camera_id, start, end):
        self.cameras.get(tenant, camera_id)
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, started_at, ended_at, size_bytes FROM rolling_segments "
                "WHERE tenant=? AND camera_id=? AND ended_at>=? AND started_at<=? ORDER BY started_at",
                (tenant, camera_id, start.isoformat(), end.isoformat()),
            ).fetchall()
        return [dict(r) for r in rows]

    def rolling_totals(self, tenant, camera_id):
        self.cameras.get(tenant, camera_id)
        with self.lock:
            row = self.conn.execute(
                "SELECT count(*) AS n, coalesce(sum(size_bytes),0) AS bytes, "
                "min(started_at) AS oldest, max(ended_at) AS newest "
                "FROM rolling_segments WHERE tenant=? AND camera_id=?",
                (tenant, camera_id),
            ).fetchone()
        return dict(row)

    def save_manifest(self, tenant, manifest: PreEventManifest):
        with self.lock:
            self.conn.execute(
                "INSERT INTO pre_event_manifests(id,tenant,camera_id,command_ref,body) VALUES(?,?,?,?,?) "
                "ON CONFLICT(tenant,camera_id,command_ref) DO UPDATE SET body=excluded.body",
                (manifest.id, tenant, manifest.camera_id, manifest.command_ref, manifest.model_dump_json()),
            )

    def manifest_by_command(self, tenant, camera_id, command_ref):
        with self.lock:
            row = self.conn.execute(
                "SELECT body FROM pre_event_manifests WHERE tenant=? AND camera_id=? AND command_ref=?",
                (tenant, camera_id, command_ref),
            ).fetchone()
        return None if row is None else PreEventManifest.model_validate_json(row["body"])

    def normalized_event(self, tenant, camera_id, event):
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO normalized_camera_events(tenant,camera_id,observed_at,body) VALUES(?,?,?,?)",
                (
                    tenant,
                    camera_id,
                    event.observed_at.isoformat(),
                    event.model_dump_json(exclude={"id", "camera_id"}),
                ),
            )
            event.id = int(cur.lastrowid)
            self.conn.execute(
                "DELETE FROM normalized_camera_events WHERE camera_id=? AND id NOT IN "
                "(SELECT id FROM normalized_camera_events WHERE camera_id=? ORDER BY id DESC LIMIT 500)",
                (camera_id, camera_id),
            )
        return event

    def normalized_events(self, tenant, camera_id, limit, offset):
        self.cameras.get(tenant, camera_id)
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, body FROM normalized_camera_events WHERE tenant=? AND camera_id=? "
                "ORDER BY id DESC LIMIT ? OFFSET ?",
                (tenant, camera_id, limit, offset),
            ).fetchall()
        result = []
        for row in rows:
            body = json.loads(row["body"])
            result.append(NormalizedCameraEvent(id=row["id"], camera_id=camera_id, **body))
        return result

    def maintenance_state(self, tenant, camera_id):
        self.cameras.get(tenant, camera_id)
        with self.lock:
            row = self.conn.execute(
                "SELECT body FROM maintenance_state WHERE tenant=? AND camera_id=?",
                (tenant, camera_id),
            ).fetchone()
        return {} if row is None else json.loads(row["body"])

    def save_maintenance_state(self, tenant, camera_id, body):
        self.cameras.get(tenant, camera_id)
        with self.lock:
            self.conn.execute(
                "INSERT INTO maintenance_state(tenant,camera_id,body) VALUES(?,?,?) "
                "ON CONFLICT(tenant,camera_id) DO UPDATE SET body=excluded.body",
                (tenant, camera_id, json.dumps(body, separators=(",", ":"), sort_keys=True)),
            )
