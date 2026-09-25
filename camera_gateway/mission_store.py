"""Tenant-scoped local metadata. Tables share the camera registry's lock/connection."""

import json
import uuid

from camera_gateway.errors import CameraNotFound, Conflict
from camera_gateway.mission_models import Coverage, CoverageInput, Group, GroupInput, HealthEvent

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
