from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from app.polygraph import GraphFile
from app.sync import run_cycle
from app.webdav import SENTINEL, WebDavEntry


def entry(data: bytes, etag: str) -> WebDavEntry:
    return WebDavEntry(etag, len(data), datetime.now(UTC).isoformat())


class FakeWebDav:
    def __init__(self, files=None, *, created=False, canary=True):
        self.files = files or {}
        self.created = created
        self.canary = canary

    def ensure_sentinel(self, folder, autocreate=True):
        return self.created

    def list(self, folder):
        result = {p: entry(data, etag) for p, (data, etag) in self.files.items()}
        if self.canary:
            result[SENTINEL] = entry(b"ok", "canary")
        return result

    def read(self, folder, path):
        data, etag_value = self.files[path]
        return data, etag_value


class FakeGraph:
    def __init__(self):
        self.files = {}
        self.ingests = []
        self.deletes = []

    def list_files(self, workspace):
        return dict(self.files)

    def ingest(self, workspace, source_path, path_root, data, last_modified=""):
        digest = hashlib.sha256(data).hexdigest()
        item = GraphFile(source_path, f"doc-{digest[:8]}", digest, "done")
        self.files[source_path] = item
        self.ingests.append((workspace, source_path, path_root, data))
        return item

    def delete(self, workspace, doc_id):
        self.deletes.append((workspace, doc_id))
        self.files = {p: f for p, f in self.files.items() if f.doc_id != doc_id}


class FakeRepo:
    def __init__(self):
        self.state = {}
        self.events = []

    def get_state(self, mapping_id):
        return {path: dict(row) for path, row in self.state.items()}

    def upsert_state(
        self, mapping_id, path, etag, digest, doc_id, status="synced", error=None,
        superseded_doc_id=None,
    ):
        retries = self.state.get(path, {}).get("retry_count", 0)
        self.state[path] = {
            "rel_path": path,
            "remote_etag": etag,
            "content_hash": digest,
            "doc_id": doc_id,
            "sync_status": status,
            "retry_count": retries + (1 if status == "failed" else 0),
            "last_error": error,
            "pending_epoch": None,
            "superseded_doc_id": superseded_doc_id,
        }

    def mark_pending(self, mapping_id, path, etag, digest):
        if path not in self.state:
            self.upsert_state(mapping_id, path, None, None, None, "pending")
        self.state[path]["sync_status"] = "pending"
        self.state[path]["pending_etag"] = etag
        self.state[path]["pending_hash"] = digest

    def mark_failed(self, mapping_id, path, error):
        if path in self.state:
            self.state[path]["sync_status"] = "failed"
            self.state[path]["last_error"] = error

    def clear_superseded(self, mapping_id, path):
        self.state[path]["superseded_doc_id"] = None

    def mapping_is_current(self, mapping_id, version):
        return True

    def set_pending_delete(self, mapping_id, path, epoch):
        self.state[path]["pending_epoch"] = epoch

    def clear_pending_delete(self, mapping_id, path):
        if path in self.state:
            self.state[path]["pending_epoch"] = None

    def clear_pending_deletes(self, mapping_id):
        for row in self.state.values():
            row["pending_epoch"] = None

    def delete_state(self, mapping_id, path):
        self.state.pop(path, None)

    def event(self, mapping_id, event_type, rel_path="", **detail):
        self.events.append((event_type, rel_path, detail))


MAPPING = {
    "id": 1,
    "nextcloud_path": "Projects/Alpha",
    "workspace_id": "alpha",
    "path_root": "/nextcloud/admin",
    "include_extensions": "md,pdf",
    "sync_hidden": False,
    "excludes": "",
    "min_files": 0,
    "max_delete_fraction": 1.0,
    "version": 1,
}


def cycle(webdav, graph, repo, now=1000, grace=300):
    return run_cycle(
        MAPPING, webdav, graph, repo, now=now, delete_grace=grace, max_bytes=1_000_000
    )


def test_new_file_ingests_and_second_cycle_is_noop():
    webdav = FakeWebDav({"guide.md": (b"hello", "e1")})
    graph, repo = FakeGraph(), FakeRepo()
    first = cycle(webdav, graph, repo)
    second = cycle(webdav, graph, repo)
    assert first.ingested == 1
    assert second.ingested == second.reingested == 0
    assert graph.ingests[0][1:] == ("Projects/Alpha/guide.md", "/nextcloud/admin", b"hello")


def test_changed_file_deletes_old_doc_then_reingests():
    webdav = FakeWebDav({"guide.md": (b"old", "e1")})
    graph, repo = FakeGraph(), FakeRepo()
    cycle(webdav, graph, repo)
    old_doc = next(iter(graph.files.values())).doc_id
    webdav.files["guide.md"] = (b"new", "e2")
    result = cycle(webdav, graph, repo)
    assert result.reingested == 1
    assert graph.deletes == [("alpha", old_doc)]
    assert repo.state["guide.md"]["content_hash"] == hashlib.sha256(b"new").hexdigest()


def test_missing_file_waits_for_healthy_grace_then_deletes():
    webdav = FakeWebDav({"guide.md": (b"hello", "e1")})
    graph, repo = FakeGraph(), FakeRepo()
    cycle(webdav, graph, repo)
    doc = repo.state["guide.md"]["doc_id"]
    webdav.files.clear()
    first = cycle(webdav, graph, repo, now=1000, grace=300)
    second = cycle(webdav, graph, repo, now=1301, grace=300)
    assert first.deferred == 1 and first.deleted == 0
    assert second.deleted == 1
    assert graph.deletes[-1] == ("alpha", doc)


def test_unhealthy_canary_suppresses_delete_and_clears_clock():
    webdav = FakeWebDav({"guide.md": (b"hello", "e1")})
    graph, repo = FakeGraph(), FakeRepo()
    cycle(webdav, graph, repo)
    webdav.files.clear()
    cycle(webdav, graph, repo, now=1000)
    assert repo.state["guide.md"]["pending_epoch"] == 1000
    webdav.canary = False
    result = cycle(webdav, graph, repo, now=2000)
    assert result.health_degraded and result.deleted == 0
    assert repo.state["guide.md"]["pending_epoch"] is None


def test_scope_tightening_uses_delete_grace_and_oversize_never_deletes():
    webdav = FakeWebDav({"guide.md": (b"hello", "e1")})
    graph, repo = FakeGraph(), FakeRepo()
    cycle(webdav, graph, repo)
    scoped = dict(MAPPING, include_extensions="pdf")
    result = run_cycle(scoped, webdav, graph, repo, now=1000, delete_grace=300, max_bytes=99)
    assert result.deferred == 1
    oversize = run_cycle(MAPPING, webdav, graph, repo, now=2000, delete_grace=0, max_bytes=1)
    assert oversize.deleted == 0


def test_unowned_existing_graph_file_is_blocked():
    data = b"already indexed"
    digest = hashlib.sha256(data).hexdigest()
    webdav = FakeWebDav({"guide.md": (data, "e1")})
    graph, repo = FakeGraph(), FakeRepo()
    graph.files["Projects/Alpha/guide.md"] = GraphFile(
        "Projects/Alpha/guide.md", "doc-existing", digest, "done"
    )
    result = cycle(webdav, graph, repo)
    assert result.adopted == 0
    assert graph.ingests == []
    assert result.health_degraded
    assert any(event[0] == "ownership_conflict" for event in repo.events)


def test_failed_replacement_keeps_old_graph_document():
    class FailingGraph(FakeGraph):
        fail = False

        def ingest(self, *args, **kwargs):
            if self.fail:
                raise RuntimeError("provider unavailable")
            return super().ingest(*args, **kwargs)

    webdav = FakeWebDav({"guide.md": (b"old", "e1")})
    graph, repo = FailingGraph(), FakeRepo()
    cycle(webdav, graph, repo)
    old_doc = repo.state["guide.md"]["doc_id"]
    graph.fail = True
    webdav.files["guide.md"] = (b"replacement", "e2")
    result = cycle(webdav, graph, repo)
    assert result.errors == 1
    assert repo.state["guide.md"]["doc_id"] == old_doc
    assert old_doc in {item.doc_id for item in graph.files.values()}


def test_nested_subfolder_keeps_recursive_source_path():
    webdav = FakeWebDav({"legal/contracts/msa.md": (b"terms", "e1")})
    graph, repo = FakeGraph(), FakeRepo()
    result = cycle(webdav, graph, repo)
    assert result.ingested == 1
    assert graph.ingests[0][1] == "Projects/Alpha/legal/contracts/msa.md"


def test_min_files_floor_degrades_and_defers_deletes():
    webdav = FakeWebDav({"a.md": (b"a", "e1"), "b.md": (b"b", "e2")})
    graph, repo = FakeGraph(), FakeRepo()
    mapping = dict(MAPPING, min_files=2)
    run_cycle(mapping, webdav, graph, repo, now=0, delete_grace=0, max_bytes=1_000_000)
    del webdav.files["a.md"]
    result = run_cycle(mapping, webdav, graph, repo, now=10, delete_grace=0, max_bytes=1_000_000)
    assert result.health_degraded
    assert result.deleted == 0
    assert graph.deletes == []


def test_bulk_drop_over_max_delete_fraction_degrades():
    webdav = FakeWebDav({f"f{i}.md": (b"x", f"e{i}") for i in range(4)})
    graph, repo = FakeGraph(), FakeRepo()
    mapping = dict(MAPPING, max_delete_fraction=0.5)
    run_cycle(mapping, webdav, graph, repo, now=0, delete_grace=0, max_bytes=1_000_000)
    for name in ("f0.md", "f1.md", "f2.md"):
        del webdav.files[name]
    result = run_cycle(mapping, webdav, graph, repo, now=10, delete_grace=0, max_bytes=1_000_000)
    assert result.health_degraded
    assert result.deleted == 0
    assert graph.deletes == []


def test_stale_mapping_version_defers_delete():
    class VersionRepo(FakeRepo):
        current = True

        def mapping_is_current(self, mapping_id, version):
            return self.current

    webdav = FakeWebDav({"guide.md": (b"hello", "e1")})
    graph, repo = FakeGraph(), VersionRepo()
    cycle(webdav, graph, repo)
    repo.current = False
    webdav.files.clear()
    result = cycle(webdav, graph, repo, now=1000, grace=0)
    assert result.deleted == 0
    assert result.deferred == 1
    assert graph.deletes == []


def test_reappearing_unchanged_file_resets_delete_grace():
    webdav = FakeWebDav({"guide.md": (b"hello", "e1")})
    graph, repo = FakeGraph(), FakeRepo()
    cycle(webdav, graph, repo)
    webdav.files.clear()
    cycle(webdav, graph, repo, now=1000)
    assert repo.state["guide.md"]["pending_epoch"] == 1000
    webdav.files["guide.md"] = (b"hello", "e1")  # comes back, identical
    cycle(webdav, graph, repo, now=1100)
    assert repo.state["guide.md"]["pending_epoch"] is None
    webdav.files.clear()
    result = cycle(webdav, graph, repo, now=99999)  # disappears again much later
    assert result.deleted == 0
    assert result.deferred == 1


def test_reappearing_oversize_file_resets_delete_grace():
    webdav = FakeWebDav({"guide.md": (b"hello", "e1")})
    graph, repo = FakeGraph(), FakeRepo()
    cycle(webdav, graph, repo)
    webdav.files.clear()
    cycle(webdav, graph, repo, now=1000)
    assert repo.state["guide.md"]["pending_epoch"] == 1000
    webdav.files["guide.md"] = (b"h" * 50, "e2")  # returns above the size cap
    run_cycle(MAPPING, webdav, graph, repo, now=1100, delete_grace=300, max_bytes=10)
    assert repo.state["guide.md"]["pending_epoch"] is None


def test_foreign_prefix_graph_row_degrades_health():
    webdav = FakeWebDav({"guide.md": (b"hello", "e1")})
    graph, repo = FakeGraph(), FakeRepo()
    cycle(webdav, graph, repo)
    graph.files["Other/Place/manual.md"] = GraphFile(
        "Other/Place/manual.md", "doc-foreign", "hash", "done"
    )
    webdav.files.clear()
    result = cycle(webdav, graph, repo, now=99999, grace=0)
    assert result.health_degraded
    assert result.deleted == 0
    assert graph.deletes == []


def failed_row(pending_etag, retry_count, updated_epoch):
    return {
        "rel_path": "guide.md",
        "remote_etag": None,
        "content_hash": None,
        "doc_id": None,
        "superseded_doc_id": None,
        "sync_status": "failed",
        "retry_count": retry_count,
        "last_error": "boom",
        "pending_epoch": None,
        "pending_etag": pending_etag,
        "pending_hash": "h",
        "updated_epoch": updated_epoch,
    }


def test_failed_ingest_backs_off_then_retries():
    webdav = FakeWebDav({"guide.md": (b"data", "e9")})
    graph, repo = FakeGraph(), FakeRepo()
    repo.state["guide.md"] = failed_row("e9", retry_count=3, updated_epoch=1000.0)
    early = cycle(webdav, graph, repo, now=1100)  # window is 60 * 2**3 = 480s
    assert early.ingested == 0
    assert early.deferred == 1
    late = cycle(webdav, graph, repo, now=1000 + 481)
    assert late.ingested == 1


def test_etag_change_bypasses_backoff():
    webdav = FakeWebDav({"guide.md": (b"data", "e10")})
    graph, repo = FakeGraph(), FakeRepo()
    repo.state["guide.md"] = failed_row("e9", retry_count=6, updated_epoch=1000.0)
    result = cycle(webdav, graph, repo, now=1001)
    assert result.ingested == 1
