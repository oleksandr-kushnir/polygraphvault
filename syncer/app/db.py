from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row

SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_mappings (
  id BIGSERIAL PRIMARY KEY,
  nextcloud_path TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  path_root TEXT NOT NULL DEFAULT '/nextcloud',
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  include_extensions TEXT NOT NULL DEFAULT 'md,txt,csv,html,pdf,docx,pptx,xlsx',
  sync_hidden BOOLEAN NOT NULL DEFAULT FALSE,
  excludes TEXT NOT NULL DEFAULT '',
  min_files INTEGER NOT NULL DEFAULT 1 CHECK (min_files >= 0),
  max_delete_fraction DOUBLE PRECISION NOT NULL DEFAULT 0.25
    CHECK (max_delete_fraction >= 0 AND max_delete_fraction <= 1),
  version BIGINT NOT NULL DEFAULT 1,
  archived_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (nextcloud_path, workspace_id),
  UNIQUE (workspace_id)
);

CREATE TABLE IF NOT EXISTS sync_state (
  mapping_id BIGINT NOT NULL REFERENCES sync_mappings(id) ON DELETE CASCADE,
  rel_path TEXT NOT NULL,
  remote_etag TEXT,
  content_hash TEXT,
  doc_id TEXT,
  superseded_doc_id TEXT,
  pending_etag TEXT,
  pending_hash TEXT,
  sync_status TEXT NOT NULL DEFAULT 'synced',
  retry_count INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  pending_delete_since TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (mapping_id, rel_path)
);

CREATE TABLE IF NOT EXISTS sync_events (
  id BIGSERIAL PRIMARY KEY,
  mapping_id BIGINT REFERENCES sync_mappings(id) ON DELETE SET NULL,
  ts TIMESTAMPTZ NOT NULL DEFAULT now(),
  event_type TEXT NOT NULL,
  rel_path TEXT,
  detail JSONB
);

CREATE INDEX IF NOT EXISTS sync_events_mapping_id_id_idx
  ON sync_events (mapping_id, id DESC);
"""


class Repository:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def _connect(self):
        return psycopg.connect(self._dsn, autocommit=True, row_factory=dict_row)

    def init(self) -> None:
        with self._connect() as conn:
            conn.execute(SCHEMA)

    def list_mappings(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        where = " WHERE archived_at IS NULL"
        if enabled_only:
            where += " AND enabled"
        with self._connect() as conn:
            return list(conn.execute(f"SELECT * FROM sync_mappings{where} ORDER BY id").fetchall())

    def get_mapping(self, mapping_id: int, include_archived: bool = False) -> dict[str, Any] | None:
        archived = "" if include_archived else " AND archived_at IS NULL"
        with self._connect() as conn:
            return conn.execute(
                f"SELECT * FROM sync_mappings WHERE id = %s{archived}", (mapping_id,)
            ).fetchone()

    def create_mapping(self, values: dict[str, Any]) -> dict[str, Any]:
        with self._connect() as conn:
            return conn.execute(
                """
                INSERT INTO sync_mappings
                  (nextcloud_path, workspace_id, path_root, enabled,
                   include_extensions, sync_hidden, excludes, min_files, max_delete_fraction)
                VALUES (%(nextcloud_path)s, %(workspace_id)s, %(path_root)s, %(enabled)s,
                        %(include_extensions)s, %(sync_hidden)s, %(excludes)s, %(min_files)s,
                        %(max_delete_fraction)s)
                RETURNING *
                """,
                values,
            ).fetchone()

    def update_mapping(self, mapping_id: int, values: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {
            "nextcloud_path", "path_root", "enabled", "include_extensions",
            "sync_hidden", "excludes", "min_files", "max_delete_fraction",
        }
        fields = [key for key in values if key in allowed]
        if not fields:
            return self.get_mapping(mapping_id)
        assignments = ", ".join(f"{key} = %({key})s" for key in fields)
        params = {key: values[key] for key in fields}
        params["id"] = mapping_id
        with self._connect() as conn:
            return conn.execute(
                f"UPDATE sync_mappings SET {assignments}, version=version+1, updated_at=now() "
                "WHERE id=%(id)s RETURNING *",
                params,
            ).fetchone()

    def archive_mapping(self, mapping_id: int) -> bool:
        with self._connect() as conn:
            result = conn.execute(
                "UPDATE sync_mappings SET enabled=false, archived_at=now(), version=version+1, "
                "updated_at=now() WHERE id=%s AND archived_at IS NULL",
                (mapping_id,),
            )
            return result.rowcount > 0

    def restore_mapping(self, mapping_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            return conn.execute(
                "UPDATE sync_mappings SET archived_at=NULL, version=version+1, updated_at=now() "
                "WHERE id=%s AND archived_at IS NOT NULL RETURNING *",
                (mapping_id,),
            ).fetchone()

    def count_state(self, mapping_id: int) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT count(*) AS n FROM sync_state WHERE mapping_id=%s", (mapping_id,)
            ).fetchone()
            return int(row["n"])

    def get_state(self, mapping_id: int) -> dict[str, dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT *, extract(epoch FROM pending_delete_since) AS pending_epoch, "
                "extract(epoch FROM updated_at) AS updated_epoch "
                "FROM sync_state WHERE mapping_id=%s",
                (mapping_id,),
            ).fetchall()
            return {row["rel_path"]: row for row in rows}

    def list_state(self, mapping_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return list(conn.execute(
                "SELECT rel_path, sync_status, doc_id, content_hash, remote_etag, "
                "retry_count, last_error, pending_delete_since, updated_at "
                "FROM sync_state WHERE mapping_id=%s ORDER BY rel_path",
                (mapping_id,),
            ).fetchall())

    def list_events(self, mapping_id: int, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return list(conn.execute(
                "SELECT id, ts, event_type, rel_path, detail FROM sync_events "
                "WHERE mapping_id=%s ORDER BY id DESC LIMIT %s",
                (mapping_id, limit),
            ).fetchall())

    def upsert_state(
        self,
        mapping_id: int,
        rel_path: str,
        etag: str | None,
        content_hash: str | None,
        doc_id: str | None,
        status: str = "synced",
        error: str | None = None,
        superseded_doc_id: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sync_state
                  (mapping_id, rel_path, remote_etag, content_hash, doc_id, superseded_doc_id,
                   sync_status, retry_count, last_error, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
                ON CONFLICT (mapping_id, rel_path) DO UPDATE SET
                  remote_etag=EXCLUDED.remote_etag,
                  content_hash=EXCLUDED.content_hash,
                  doc_id=EXCLUDED.doc_id,
                  superseded_doc_id=EXCLUDED.superseded_doc_id,
                  pending_etag=NULL,
                  pending_hash=NULL,
                  sync_status=EXCLUDED.sync_status,
                  retry_count=CASE WHEN EXCLUDED.sync_status='failed'
                    THEN sync_state.retry_count + 1 ELSE 0 END,
                  last_error=EXCLUDED.last_error,
                  pending_delete_since=NULL,
                  updated_at=now()
                """,
                (
                    mapping_id, rel_path, etag, content_hash, doc_id, superseded_doc_id, status,
                    1 if status == "failed" else 0, (error or "")[:2000] or None,
                ),
            )

    def mark_failed(self, mapping_id: int, rel_path: str, error: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE sync_state SET sync_status='failed', retry_count=retry_count+1, "
                "last_error=%s, updated_at=now() WHERE mapping_id=%s AND rel_path=%s",
                (error[:2000], mapping_id, rel_path),
            )

    def mark_pending(
        self, mapping_id: int, rel_path: str, etag: str | None, content_hash: str
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sync_state
                  (mapping_id, rel_path, sync_status, pending_etag, pending_hash, updated_at)
                VALUES (%s,%s,'pending',%s,%s,now())
                ON CONFLICT (mapping_id, rel_path) DO UPDATE SET
                  sync_status='pending', pending_etag=EXCLUDED.pending_etag,
                  pending_hash=EXCLUDED.pending_hash, last_error=NULL, updated_at=now()
                """,
                (mapping_id, rel_path, etag, content_hash),
            )

    def clear_superseded(self, mapping_id: int, rel_path: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE sync_state SET superseded_doc_id=NULL, updated_at=now() "
                "WHERE mapping_id=%s AND rel_path=%s",
                (mapping_id, rel_path),
            )

    def mapping_is_current(self, mapping_id: int, version: int) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM sync_mappings WHERE id=%s AND version=%s AND enabled "
                "AND archived_at IS NULL",
                (mapping_id, version),
            ).fetchone()
            return row is not None

    @contextmanager
    def mapping_lock(self, mapping_id: int):
        conn = self._connect()
        locked = False
        try:
            row = conn.execute(
                "SELECT pg_try_advisory_lock(%s) AS locked", (mapping_id,)
            ).fetchone()
            locked = bool(row["locked"])
            yield locked
        finally:
            if locked:
                conn.execute("SELECT pg_advisory_unlock(%s)", (mapping_id,))
            conn.close()

    def set_pending_delete(self, mapping_id: int, rel_path: str, epoch: float) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE sync_state SET pending_delete_since=to_timestamp(%s), updated_at=now() "
                "WHERE mapping_id=%s AND rel_path=%s",
                (epoch, mapping_id, rel_path),
            )

    def clear_pending_delete(self, mapping_id: int, rel_path: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE sync_state SET pending_delete_since=NULL, updated_at=now() "
                "WHERE mapping_id=%s AND rel_path=%s AND pending_delete_since IS NOT NULL",
                (mapping_id, rel_path),
            )

    def clear_pending_deletes(self, mapping_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE sync_state SET pending_delete_since=NULL "
                "WHERE mapping_id=%s AND pending_delete_since IS NOT NULL",
                (mapping_id,),
            )

    def delete_state(self, mapping_id: int, rel_path: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM sync_state WHERE mapping_id=%s AND rel_path=%s",
                (mapping_id, rel_path),
            )

    def event(self, mapping_id: int, event_type: str, rel_path: str = "", **detail: Any) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sync_events (mapping_id,event_type,rel_path,detail) VALUES (%s,%s,%s,%s)",
                (mapping_id, event_type, rel_path or None, json.dumps(detail) if detail else None),
            )
