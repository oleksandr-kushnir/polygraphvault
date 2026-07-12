from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from app.filters import in_scope
from app.polygraph import TERMINAL
from app.webdav import SENTINEL

log = logging.getLogger("nc-rag-sync")

RETRY_BACKOFF_BASE_SECS = 60.0
RETRY_BACKOFF_CAP_SECS = 3600.0


@dataclass
class CycleResult:
    scanned: int = 0
    included: int = 0
    skipped: int = 0
    ingested: int = 0
    reingested: int = 0
    deleted: int = 0
    adopted: int = 0
    deferred: int = 0
    errors: int = 0
    health_degraded: bool = False


def source_path(mapping: dict[str, Any], rel_path: str) -> str:
    return f"{mapping['nextcloud_path'].strip('/')}/{rel_path.strip('/')}"


def graph_files_for_mapping(mapping: dict[str, Any], files: dict) -> dict[str, Any]:
    prefix = mapping["nextcloud_path"].strip("/") + "/"
    return {path[len(prefix) :]: item for path, item in files.items() if path.startswith(prefix)}


def run_cycle(
    mapping, webdav, graph, repo, *, now: float, delete_grace: float,
    max_bytes: int, canary_autocreate: bool = True,
):
    result = CycleResult()
    mapping_id = int(mapping["id"])
    created_canary = webdav.ensure_sentinel(
        mapping["nextcloud_path"], autocreate=canary_autocreate
    )
    listed = webdav.list(mapping["nextcloud_path"])
    result.scanned = max(0, len(listed) - (1 if SENTINEL in listed else 0))

    canary_ok = SENTINEL in listed
    scoped = {}
    oversize = set()
    for path, entry in listed.items():
        if path == SENTINEL:
            continue
        if not in_scope(
            path,
            mapping["include_extensions"],
            bool(mapping["sync_hidden"]),
            mapping["excludes"],
        ):
            result.skipped += 1
            continue
        if max_bytes > 0 and entry.size > max_bytes:
            oversize.add(path)
            result.skipped += 1
            continue
        scoped[path] = entry
    result.included = len(scoped)

    state = repo.get_state(mapping_id)
    all_graph_files = graph.list_files(mapping["workspace_id"])
    graph_index = graph_files_for_mapping(mapping, all_graph_files)
    owned = {path for path, row in state.items() if row.get("doc_id")}
    has_indexed_files = bool(owned)
    below_floor = has_indexed_files and len(scoped) < int(mapping["min_files"])
    missing_owned = {path for path in owned if path not in scoped and path not in oversize}
    missing_fraction = len(missing_owned) / len(owned) if owned else 0.0
    bulk_drop = missing_fraction > float(mapping.get("max_delete_fraction", 0.25))
    prefix = mapping["nextcloud_path"].strip("/") + "/"
    owned_sources = {prefix + rel for rel in state}
    unowned = {sp for sp in all_graph_files if sp not in owned_sources}
    result.health_degraded = created_canary or not canary_ok or below_floor or bulk_drop or bool(unowned)
    if result.health_degraded:
        repo.clear_pending_deletes(mapping_id)
        repo.event(
            mapping_id,
            "health_degraded",
            canary_created=created_canary,
            canary_present=canary_ok,
            included=len(scoped),
            min_files=int(mapping["min_files"]),
            missing_fraction=missing_fraction,
            max_delete_fraction=float(mapping.get("max_delete_fraction", 0.25)),
            unowned_paths=sorted(unowned)[:20],
        )

    paths = sorted(set(scoped) | set(state) | set(graph_index))
    for path in paths:
        remote = scoped.get(path)
        cached = state.get(path)
        indexed = graph_index.get(path)

        # A file that is present again (in scope or merely oversize) is not
        # missing: its delete-grace clock must restart from zero if it
        # disappears later.
        present = remote is not None or path in oversize
        if present and cached and cached.get("pending_epoch") is not None:
            repo.clear_pending_delete(mapping_id, path)
            cached["pending_epoch"] = None
        if path in oversize:
            continue

        if indexed and not cached:
            repo.event(
                mapping_id, "ownership_conflict", path, doc_id=indexed.doc_id,
                reason="PolyGraphRAG row has no sync_state owner",
            )
            result.errors += 1
            continue

        if cached and cached.get("superseded_doc_id"):
            if not repo.mapping_is_current(mapping_id, int(mapping["version"])):
                result.deferred += 1
                continue
            try:
                graph.delete(mapping["workspace_id"], cached["superseded_doc_id"])
                repo.clear_superseded(mapping_id, path)
                cached["superseded_doc_id"] = None
            except Exception as exc:  # noqa: BLE001
                repo.event(mapping_id, "cleanup_failed", path, error=str(exc))
                result.errors += 1
                continue

        if remote is None:
            doc_id = cached.get("doc_id") if cached else None
            if not doc_id:
                if cached:
                    repo.delete_state(mapping_id, path)
                continue
            if result.health_degraded:
                result.deferred += 1
                continue
            pending = cached.get("pending_epoch")
            if pending is None and delete_grace > 0:
                repo.set_pending_delete(mapping_id, path, now)
                result.deferred += 1
                continue
            if pending is not None and now - float(pending) < delete_grace:
                result.deferred += 1
                continue
            try:
                if not repo.mapping_is_current(mapping_id, int(mapping["version"])):
                    result.deferred += 1
                    continue
                graph.delete(mapping["workspace_id"], doc_id)
                repo.delete_state(mapping_id, path)
                repo.event(mapping_id, "deleted", path, doc_id=doc_id)
                result.deleted += 1
            except Exception as exc:  # noqa: BLE001
                repo.event(mapping_id, "delete_failed", path, error=str(exc), doc_id=doc_id)
                result.errors += 1
            continue

        if (
            cached
            and indexed
            and cached.get("sync_status") == "synced"
            and cached.get("remote_etag") == remote.etag
            and cached.get("doc_id") == indexed.doc_id
            and cached.get("content_hash") == indexed.content_hash
        ):
            continue

        if (
            cached
            and cached.get("sync_status") == "failed"
            and cached.get("pending_etag") == remote.etag
            and cached.get("updated_epoch") is not None
        ):
            wait = min(
                RETRY_BACKOFF_CAP_SECS,
                RETRY_BACKOFF_BASE_SECS * (2 ** min(int(cached.get("retry_count") or 0), 6)),
            )
            if now - float(cached["updated_epoch"]) < wait:
                result.deferred += 1
                continue

        try:
            data, read_etag = webdav.read(mapping["nextcloud_path"], path)
            digest = hashlib.sha256(data).hexdigest()
            etag = read_etag or remote.etag
            if indexed and indexed.status == "done" and indexed.doc_id and indexed.content_hash == digest:
                old_doc_id = cached.get("doc_id") if cached else None
                superseded = old_doc_id if old_doc_id and old_doc_id != indexed.doc_id else None
                repo.upsert_state(
                    mapping_id, path, etag, digest, indexed.doc_id, "synced",
                    superseded_doc_id=superseded,
                )
                if superseded and repo.mapping_is_current(mapping_id, int(mapping["version"])):
                    graph.delete(mapping["workspace_id"], superseded)
                    repo.clear_superseded(mapping_id, path)
                result.adopted += 1
                continue

            if (
                indexed
                and indexed.status not in TERMINAL
                and cached
                and cached.get("pending_hash") == digest
            ):
                # A previous attempt's ingest job is still running server-side
                # for these exact bytes. Re-uploading would create an orphaned
                # duplicate document; wait for the job to reach a terminal
                # state instead (done -> adopted, failed -> re-ingested).
                result.deferred += 1
                continue

            old_doc_id = cached.get("doc_id") if cached else None
            repo.mark_pending(mapping_id, path, etag, digest)
            ingested = graph.ingest(
                mapping["workspace_id"],
                source_path(mapping, path),
                mapping["path_root"],
                data,
                remote.last_modified,
            )
            if not ingested.doc_id or not ingested.content_hash:
                raise RuntimeError("PolyGraphRAG completed without doc_id/content_hash")
            repo.upsert_state(
                mapping_id, path, etag, ingested.content_hash, ingested.doc_id, "synced",
                superseded_doc_id=old_doc_id,
            )
            if old_doc_id and repo.mapping_is_current(mapping_id, int(mapping["version"])):
                graph.delete(mapping["workspace_id"], old_doc_id)
                repo.clear_superseded(mapping_id, path)
            event = "reingested" if old_doc_id else "ingested"
            repo.event(mapping_id, event, path, doc_id=ingested.doc_id)
            if old_doc_id:
                result.reingested += 1
            else:
                result.ingested += 1
        except Exception as exc:  # noqa: BLE001
            repo.mark_failed(mapping_id, path, str(exc))
            repo.event(mapping_id, "sync_failed", path, error=str(exc))
            result.errors += 1
    return result


class Scheduler:
    PRUNE_INTERVAL_SECS = 24 * 3600.0

    def __init__(self, config, repo, webdav, graph) -> None:
        self._config = config
        self._repo = repo
        self._webdav = webdav
        self._graph = graph
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._pending: set[int] = set()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._last_prune = 0.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="sync-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=10)

    def request(self, mapping_id: int) -> None:
        with self._lock:
            self._pending.add(mapping_id)
        self._wake.set()

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _maybe_prune_events(self) -> None:
        days = float(self._config.events_retention_days)
        if days <= 0:
            return
        now = time.monotonic()
        if self._last_prune and now - self._last_prune < self.PRUNE_INTERVAL_SECS:
            return
        self._last_prune = now
        try:
            removed = self._repo.prune_events(days)
            if removed:
                log.info("pruned %s sync events older than %s days", removed, days)
        except Exception as exc:  # noqa: BLE001
            log.warning("event pruning failed; will retry next interval: %s", exc)

    def _take_mappings(self) -> list[dict[str, Any]]:
        with self._lock:
            pending = set(self._pending)
            self._pending.clear()
        if not pending:
            return self._repo.list_mappings(enabled_only=True)
        return [
            mapping
            for mapping_id in sorted(pending)
            if (mapping := self._repo.get_mapping(mapping_id)) and mapping["enabled"]
        ]

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._maybe_prune_events()
            try:
                mappings = self._take_mappings()
            except Exception as exc:  # noqa: BLE001
                log.warning("scheduler could not list mappings; retrying next tick: %s", exc)
                mappings = []
            for mapping in mappings:
                if self._stop.is_set():
                    return
                try:
                    with self._repo.mapping_lock(mapping["id"]) as locked:
                        if not locked:
                            log.info("mapping %s already locked; skipping", mapping["id"])
                            continue
                        mapping = self._repo.get_mapping(mapping["id"])
                        if not mapping or not mapping["enabled"]:
                            continue
                        result = run_cycle(
                            mapping,
                            self._webdav,
                            self._graph,
                            self._repo,
                            now=time.time(),
                            delete_grace=self._config.delete_grace_secs,
                            max_bytes=self._config.max_file_bytes,
                            canary_autocreate=self._config.canary_autocreate,
                        )
                    log.info(
                        "cycle mapping=%s folder=%s workspace=%s scanned=%s included=%s "
                        "ingested=%s reingested=%s deleted=%s adopted=%s deferred=%s errors=%s degraded=%s",
                        mapping["id"], mapping["nextcloud_path"], mapping["workspace_id"],
                        result.scanned, result.included, result.ingested, result.reingested,
                        result.deleted, result.adopted, result.deferred, result.errors,
                        result.health_degraded,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("mapping %s cycle failed: %s", mapping["id"], exc)
                    try:
                        self._repo.clear_pending_deletes(mapping["id"])
                        self._repo.event(mapping["id"], "cycle_failed", error=str(exc))
                    except Exception:  # noqa: BLE001
                        log.exception("failed to record cycle error")
            self._wake.wait(self._config.poll_interval)
            self._wake.clear()
