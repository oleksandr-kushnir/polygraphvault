from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.main import create_app

TOKEN = "test-token-1234"


def make_config() -> Config:
    return Config(
        postgres_dsn="postgresql://unused/unused",
        nextcloud_url="http://nc",
        nextcloud_user="u",
        nextcloud_password="p",
        polygraphrag_url="http://pg",
        polygraphrag_api_token="",
        syncer_api_token=TOKEN,
        poll_interval=30.0,
        delete_grace_secs=300.0,
        max_file_bytes=1_000_000,
        ingest_timeout=1.0,
        canary_autocreate=True,
        events_retention_days=90.0,
    )


class ApiRepo:
    """In-memory stand-in for db.Repository, covering the routes' contract."""

    ALLOWED = {
        "nextcloud_path", "path_root", "enabled", "include_extensions",
        "sync_hidden", "excludes", "min_files", "max_delete_fraction",
    }

    def __init__(self):
        self.mappings: dict[int, dict] = {}
        self.state_counts: dict[int, int] = {}
        self.state_rows: dict[int, list[dict]] = {}
        self.event_rows: dict[int, list[dict]] = {}
        self._next = 1

    def init(self):
        pass

    def list_mappings(self, enabled_only=False):
        rows = [dict(m) for m in self.mappings.values() if m["archived_at"] is None]
        if enabled_only:
            rows = [m for m in rows if m["enabled"]]
        return sorted(rows, key=lambda m: m["id"])

    def get_mapping(self, mapping_id, include_archived=False):
        row = self.mappings.get(mapping_id)
        if row is None or (row["archived_at"] is not None and not include_archived):
            return None
        return dict(row)

    def create_mapping(self, values):
        if any(m["workspace_id"] == values["workspace_id"] for m in self.mappings.values()):
            raise psycopg.errors.UniqueViolation("duplicate workspace")
        now = datetime.now(UTC)
        row = {"id": self._next, "version": 1, "archived_at": None,
               "created_at": now, "updated_at": now, **values}
        self.mappings[self._next] = row
        self._next += 1
        return dict(row)

    def update_mapping(self, mapping_id, values):
        row = self.mappings.get(mapping_id)
        if row is None:
            return None
        for key, value in values.items():
            if key in self.ALLOWED:
                row[key] = value
        row["version"] += 1
        row["updated_at"] = datetime.now(UTC)
        return dict(row)

    def archive_mapping(self, mapping_id):
        row = self.mappings[mapping_id]
        row["archived_at"] = datetime.now(UTC)
        row["enabled"] = False
        row["version"] += 1
        return True

    def restore_mapping(self, mapping_id):
        row = self.mappings[mapping_id]
        row["archived_at"] = None
        row["version"] += 1
        return dict(row)

    def count_state(self, mapping_id):
        return self.state_counts.get(mapping_id, 0)

    def list_state(self, mapping_id):
        return list(self.state_rows.get(mapping_id, []))

    def list_events(self, mapping_id, limit=100):
        rows = sorted(self.event_rows.get(mapping_id, []), key=lambda r: r["id"], reverse=True)
        return rows[:limit]


class StubWebDav:
    def __init__(self, folders=("Source",)):
        self.folders = set(folders)

    def validate_folder(self, folder):
        if folder not in self.folders:
            raise FileNotFoundError(f"Nextcloud folder not found: {folder}")

    def close(self):
        pass


class StubGraph:
    def __init__(self, workspaces=(), files=None):
        self.workspaces = set(workspaces)
        self.files = files or {}

    def ensure_workspace(self, workspace_id, name, create):
        if workspace_id in self.workspaces:
            return
        if not create:
            raise KeyError(f"PolyGraphRAG workspace not found: {workspace_id}")
        self.workspaces.add(workspace_id)

    def list_files(self, workspace_id):
        return self.files.get(workspace_id, {})

    def close(self):
        pass


class StubScheduler:
    def __init__(self):
        self.requests = []
        self.alive = True

    def start(self):
        pass

    def stop(self):
        pass

    def is_alive(self):
        return self.alive

    def request(self, mapping_id):
        self.requests.append(mapping_id)


@pytest.fixture()
def api():
    repo, scheduler = ApiRepo(), StubScheduler()
    graph = StubGraph(workspaces={"busy"}, files={"busy": {"x": object()}})
    app = create_app(
        make_config(), repo=repo, webdav=StubWebDav(), graph=graph, scheduler=scheduler
    )
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        yield client, repo, scheduler


CREATE_BODY = {
    "nextcloud_path": "Source",
    "workspace_id": "ws_one",
    "create_workspace": True,
}


def test_requests_without_token_are_rejected_but_health_is_open(api):
    client, _, _ = api
    bare = TestClient(client.app)
    assert bare.get("/mappings").status_code == 401
    assert bare.get("/mappings", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert bare.get("/health").status_code == 200


def test_token_query_param_rejected_when_flag_disabled(api):
    client, _, _ = api
    bare = TestClient(client.app)
    assert bare.get(f"/mappings?token={TOKEN}").status_code == 401


def _query_param_client():
    cfg = replace(make_config(), allow_token_query_param=True)
    app = create_app(
        cfg, repo=ApiRepo(), webdav=StubWebDav(), graph=StubGraph(), scheduler=StubScheduler()
    )
    return TestClient(app)


def test_token_query_param_accepted_when_flag_enabled():
    with _query_param_client() as bare:
        assert bare.get(f"/mappings?token={TOKEN}").status_code == 200
        assert bare.get("/mappings?token=wrong").status_code == 401
        assert bare.get("/mappings").status_code == 401


def test_header_still_authenticates_when_query_param_enabled():
    with _query_param_client() as bare:
        assert bare.get(
            "/mappings", headers={"Authorization": f"Bearer {TOKEN}"}
        ).status_code == 200


def test_create_mapping_schedules_run_and_rejects_duplicates(api):
    client, _, scheduler = api
    created = client.post("/mappings", json=CREATE_BODY)
    assert created.status_code == 201
    assert created.json()["workspace_id"] == "ws_one"
    assert scheduler.requests == [created.json()["id"]]
    duplicate = client.post("/mappings", json=CREATE_BODY)
    assert duplicate.status_code == 409


def test_create_mapping_rejects_missing_folder_and_nonempty_workspace(api):
    client, _, _ = api
    missing = client.post("/mappings", json=dict(CREATE_BODY, nextcloud_path="Nope"))
    assert missing.status_code == 422
    nonempty = client.post(
        "/mappings",
        json={"nextcloud_path": "Source", "workspace_id": "busy", "create_workspace": False},
    )
    assert nonempty.status_code == 409


def test_patch_rejects_ownership_change_after_state_exists(api):
    client, repo, _ = api
    mapping_id = client.post("/mappings", json=CREATE_BODY).json()["id"]
    repo.state_counts[mapping_id] = 3
    blocked = client.patch(f"/mappings/{mapping_id}", json={"nextcloud_path": "Other"})
    assert blocked.status_code == 409
    blocked_root = client.patch(f"/mappings/{mapping_id}", json={"path_root": "/elsewhere"})
    assert blocked_root.status_code == 409
    allowed = client.patch(f"/mappings/{mapping_id}", json={"min_files": 5})
    assert allowed.status_code == 200
    assert allowed.json()["min_files"] == 5


def test_archive_restore_and_disabled_run(api):
    client, _, _ = api
    mapping_id = client.post("/mappings", json=CREATE_BODY).json()["id"]
    assert client.delete(f"/mappings/{mapping_id}").status_code == 200
    assert client.get(f"/mappings/{mapping_id}").status_code == 404
    restored = client.post(f"/mappings/{mapping_id}/restore")
    assert restored.status_code == 200
    client.post(f"/mappings/{mapping_id}/disable")
    assert client.post(f"/mappings/{mapping_id}/run").status_code == 409


def test_state_and_events_endpoints(api):
    client, repo, _ = api
    mapping_id = client.post("/mappings", json=CREATE_BODY).json()["id"]
    now = datetime.now(UTC)
    repo.state_rows[mapping_id] = [{
        "rel_path": "guide.md", "sync_status": "synced", "doc_id": "doc-1",
        "content_hash": "abc", "remote_etag": "e1", "retry_count": 0,
        "last_error": None, "pending_delete_since": None, "updated_at": now,
    }]
    repo.event_rows[mapping_id] = [
        {"id": 1, "ts": now, "event_type": "ingested", "rel_path": "guide.md", "detail": None},
        {"id": 2, "ts": now, "event_type": "deleted", "rel_path": "old.md",
         "detail": {"doc_id": "doc-0"}},
    ]
    state = client.get(f"/mappings/{mapping_id}/state")
    assert state.status_code == 200
    assert state.json()[0]["rel_path"] == "guide.md"
    events = client.get(f"/mappings/{mapping_id}/events?limit=1")
    assert events.status_code == 200
    assert [e["event_type"] for e in events.json()] == ["deleted"]
    assert client.get("/mappings/9999/events").status_code == 404


def test_health_reports_scheduler_death(api):
    client, _, scheduler = api
    assert client.get("/health").json()["scheduler"] == "ok"
    scheduler.alive = False
    degraded = client.get("/health")
    assert degraded.status_code == 503
    assert degraded.json()["scheduler"] == "dead"
