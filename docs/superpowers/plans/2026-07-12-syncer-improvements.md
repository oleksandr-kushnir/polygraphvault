# Syncer Hardening & Gap-Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix two reconciliation-safety bugs, close the promised-but-missing API test layer, add failure backoff and introspection endpoints, make every failure path self-healing (outages recover on the next run, nothing accumulates unboundedly), and bring the repo up to baseline engineering hygiene (git history, lint/type tooling, CI, non-root container).

**Architecture:** All behavior changes live in the `syncer/` FastAPI app (`app/sync.py` reconciliation core, `app/db.py` repository, `app/main.py` routes). Reconciliation changes are TDD'd against the existing in-memory fakes in `tests/test_sync.py`; a new `tests/test_api.py` harness gets dependency injection into `create_app` so routes/auth are testable without Postgres/Nextcloud. Infra tasks (git, ruff/mypy, CI, Dockerfile) are additive and must not change runtime behavior.

**Tech Stack:** Python 3.11, FastAPI, psycopg 3 (raw SQL, no ORM), httpx, pytest 8, ruff, mypy, GitHub Actions, Docker Compose.

**Context for the executor (read first):**

- Read `CLAUDE.md`, `docs/concept.md` §4–§5 before touching `syncer/app/sync.py`. The delete guards described there are safety-critical invariants; every task below preserves them.
- Tests run locally from `syncer/` with `python -m pytest -q` (deps: `pip install -r requirements-dev.txt`). Baseline before this plan: **16 passed**.
- The repository's `.git` directory exists but is **empty** — there is no history. Task 0 creates the baseline commit; do it first or none of the per-task commits will work.
- Never read or print `.env` — it holds live credentials. `.env.example` is the reference.
- `run_cycle` is deliberately one large pure-ish function with injected I/O. Do not split it into modules in this plan; make the minimal surgical edits shown below.

## Findings this plan addresses (why each task exists)

| # | Finding | Severity | Task |
|---|---------|----------|------|
| F1 | No git history (`.git` is an empty dir); junk empty dirs `syncer;C` and `.agents` in repo root | infra | 0 |
| F2 | Delete-grace bug: a file that disappears (grace clock starts), then reappears unchanged (no-op branch) or reappears oversize, never clears `pending_delete_since`. When it later disappears again, it is deleted immediately with **no fresh grace period** — violates the documented "must remain missing through grace of healthy scans" guarantee | **bug, safety** | 2 |
| F3 | Unowned-row health guard only sees graph rows whose `source_path` starts with the mapping's folder prefix (`graph_files_for_mapping` filters first). A row uploaded into the workspace with any other `source_path` is invisible to the `unowned` check, so the "workspace holds a row the syncer doesn't own → degraded" guarantee silently doesn't hold | **bug, safety** | 3 |
| F4 | The `min_files` floor, `max_delete_fraction` bulk-drop, and stale-`version` deferral guards have **zero test coverage** despite being the core safety features | test gap | 1 |
| F5 | A permanently failing file (corrupt/unsupported) is re-downloaded and re-uploaded to PolyGraphRAG **every poll cycle (30 s) forever** — each retry re-runs LLM ingestion, burning provider cost. `retry_count` is stored but never used | reliability, cost | 4 |
| F6 | concept.md §8 layer 4 promises "API/database tests for runtime mapping CRUD and validation" — none exist. `create_app` hard-constructs its dependencies so routes/auth middleware are untestable | test gap | 5 |
| F7 | `sync_events` audit trail and `sync_state` are written but unreadable except via psql — no API introspection; `sync_events` also has no index and grows unbounded | usability | 6 |
| F8 | `update_mapping` in `main.py` contains a duplicated dead check: the `ownership_change` guard already raises 409 for `nextcloud_path` changes with state, then an inner identical `count_state` check repeats it | cleanup | 7 |
| F9 | No lint/format/type tooling; violates the repo owner's global engineering baseline (ruff + mypy via pyproject) | infra | 8 |
| F10 | No CI at all (no `.github/`) | infra | 9 |
| F11 | Syncer container runs as root | hardening | 10 |
| F12 | Doc drift: README claims "syncer API requires Bearer" but an empty `SYNCER_API_TOKEN` (the `.env.example` default) disables auth entirely; concept.md's "overlap rejection" phrasing is ambiguous (only *workspace* overlap is rejected — the same Nextcloud folder may feed two workspaces); new endpoints/behavior need documenting | docs | 11 |
| F13 | **Scheduler thread dies permanently on a DB outage**: `Scheduler._loop` calls `_take_mappings()` outside any try/except; one Postgres hiccup at that moment kills the daemon thread. The HTTP API keeps serving and `/health` reports "ok" once the DB recovers, but syncing never resumes until a manual container restart — a silent, permanent outage | **bug, self-healing** | 12 |
| F14 | **Crash/timeout during ingest re-uploads duplicates that accumulate**: if the poll times out (`SYNC_INGEST_TIMEOUT`) or the process dies while a PolyGraphRAG job is still running, the next cycle uploads the same bytes again. The first job's finished document becomes an orphan the syncer never owns or deletes — and `list_files` dedupes by `source_path`, so orphans are invisible even to the unowned-row guard. Each duplicate also re-runs LLM ingestion (cost). The recovery path that *does* exist (adopt a completed job by content hash) has no test | **bug, self-healing** | 13 |
| F15 | Unbounded accumulation: `sync_events` has no retention; Docker's default json-file logging never rotates, so container logs eventually fill the VPS disk | reliability | 14, 15 |

Deferred items (out of scope, listed at the end): Caddy blocklist→allowlist, DB connection pooling, scheduler lock-contention re-queue, streaming large files, resuming in-flight ingest jobs by stored `job_id`, public rate limiting.

**Self-healing inventory (verified during analysis — already correct, preserve these):** WebDAV or Postgres failure mid-cycle aborts the cycle, clears pending-delete clocks, emits `cycle_failed`, and the next tick retries. A crashed replacement leaves the old document queryable (upload-first). A crashed superseded-cleanup is retried at the start of every later cycle. A crash after an upload completed is recovered by adopting the finished job via content hash (no re-upload). Postgres advisory locks are session-scoped, so a dead process can never leave a mapping permanently locked. `restart: unless-stopped` restarts a crashed container. Tasks 12–15 close the remaining self-healing gaps (F13–F15).

---

## File Structure

| File | Change |
|---|---|
| `.gitignore` | Modify — add `.mypy_cache/`, `.agents/` |
| `syncer/app/sync.py` | Modify — presence clears pending-delete clock; workspace-wide unowned check; retry backoff; scheduler outage resilience + `is_alive`; in-flight-job deferral; event pruning |
| `syncer/app/db.py` | Modify — `clear_pending_delete`, `list_state`, `list_events`, `prune_events`, `updated_epoch` in `get_state`, events index in `SCHEMA` |
| `syncer/app/main.py` | Modify — DI parameters on `create_app`; two GET endpoints; remove duplicated PATCH check; scheduler liveness in `/health` |
| `syncer/app/config.py` | Modify — `events_retention_days` |
| `syncer/app/models.py` | Modify — `FileStateView`, `EventView` |
| `syncer/tests/test_sync.py` | Modify — new guard/bug/backoff tests; `FakeRepo.clear_pending_delete` |
| `syncer/tests/test_api.py` | Create — API/auth test harness with in-memory stubs |
| `syncer/pyproject.toml` | Create — ruff, mypy, pytest config |
| `syncer/pytest.ini` | Delete — config moves to pyproject.toml |
| `syncer/requirements-dev.txt` | Modify — add ruff, mypy |
| `syncer/Dockerfile` | Modify — non-root user |
| `.github/workflows/ci.yml` | Create — lint + type-check + tests |
| `docker-compose.yml`, `docker-compose.vps.yml` | Modify — `SYNC_EVENTS_RETENTION_DAYS` passthrough; log rotation |
| `.env.example`, `.env.vps.example` | Modify — `SYNC_EVENTS_RETENTION_DAYS` |
| `README.md`, `docs/concept.md`, `CLAUDE.md` | Modify — doc alignment |

---

### Task 0: Git baseline and housekeeping

**Files:**
- Modify: `.gitignore`
- Delete: `syncer;C/` (empty junk directory, artifact of a mangled `docker run -v` command), `.agents/` (empty)

- [ ] **Step 1: Verify the junk directories are empty, then remove them**

Run (PowerShell, from repo root):
```powershell
(Get-ChildItem -Recurse -Force ".\syncer;C" | Measure-Object).Count
(Get-ChildItem -Recurse -Force ".\.agents" | Measure-Object).Count
```
Expected: `0` for both. **If either is non-empty, stop and report the contents instead of deleting.**
```powershell
Remove-Item -Recurse -Force ".\syncer;C"
Remove-Item -Recurse -Force ".\.agents"
```

- [ ] **Step 2: Extend .gitignore**

Replace the full contents of `.gitignore` with:
```gitignore
.env
.pytest_cache/
__pycache__/
*.py[cod]
.coverage
htmlcov/
.upstream-polygraphrag/
.mypy_cache/
.agents/
```

- [ ] **Step 3: Initialize the repository and create the baseline commit**

`.git` exists but is empty, so `git init` is safe and non-destructive:
```powershell
git init -b main
git add -A
git status
```
Verify `git status` does **not** list `.env`, `.mypy_cache/`, or `.upstream-polygraphrag/`. If `.env` appears, stop — do not commit it.
```powershell
git commit -m "chore: baseline commit of polygraphrag+nextcloud syncer stack"
```

- [ ] **Step 4: Verify tests still pass (baseline)**

Run from `syncer/`: `python -m pytest -q`
Expected: `16 passed`

---

### Task 1: Test-gap fill for existing safety guards (tests only, no behavior change)

The `min_files` floor, `max_delete_fraction` bulk-drop guard, and stale-`version` deferral in `run_cycle` are currently untested. These tests must pass **against current code** — if any fails, stop and report; that's a latent bug, not a test mistake.

**Files:**
- Modify: `syncer/tests/test_sync.py` (append at end of file)

- [ ] **Step 1: Add the three guard tests**

Append to `syncer/tests/test_sync.py`:

```python
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
```

- [ ] **Step 2: Run the suite**

Run from `syncer/`: `python -m pytest tests/test_sync.py -q`
Expected: `19 passed` (all new tests pass against the current implementation).

- [ ] **Step 3: Commit**

```powershell
git add syncer/tests/test_sync.py
git commit -m "test: cover min_files floor, bulk-drop fraction, and stale-version guards"
```

---

### Task 2: Bug fix — a reappearing file must reset its delete-grace clock

**Bug:** `run_cycle` starts a `pending_delete_since` clock when a file goes missing, but only clears it via `upsert_state` (re-ingest) or a degraded cycle. A file that reappears **unchanged** hits the no-op branch and keeps its old clock; a file that reappears **oversize** is skipped before any state update. If the file later disappears again, `now - pending >= grace` already holds and the graph doc is deleted immediately — no fresh grace.

**Files:**
- Modify: `syncer/app/db.py` (add `clear_pending_delete`)
- Modify: `syncer/app/sync.py:100-102` (per-path loop head)
- Test: `syncer/tests/test_sync.py` (new tests + `FakeRepo.clear_pending_delete`)

- [ ] **Step 1: Add `clear_pending_delete` to `FakeRepo`**

In `syncer/tests/test_sync.py`, inside `class FakeRepo`, directly after the existing `set_pending_delete` method, add:

```python
    def clear_pending_delete(self, mapping_id, path):
        if path in self.state:
            self.state[path]["pending_epoch"] = None
```

- [ ] **Step 2: Write the failing tests**

Append to `syncer/tests/test_sync.py`:

```python
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_sync.py -q`
Expected: both new tests FAIL — first asserts `pending_epoch is None` gets `1000`, and the follow-up delete assertion sees `deleted == 1`.

- [ ] **Step 4: Add the repository method**

In `syncer/app/db.py`, directly after the existing `set_pending_delete` method, add:

```python
    def clear_pending_delete(self, mapping_id: int, rel_path: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE sync_state SET pending_delete_since=NULL, updated_at=now() "
                "WHERE mapping_id=%s AND rel_path=%s AND pending_delete_since IS NOT NULL",
                (mapping_id, rel_path),
            )
```

- [ ] **Step 5: Rework the per-path loop head in `run_cycle`**

In `syncer/app/sync.py`, replace this block:

```python
    for path in paths:
        if path in oversize:
            continue
        remote = scoped.get(path)
        cached = state.get(path)
        indexed = graph_index.get(path)
```

with:

```python
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
```

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest -q`
Expected: `21 passed` (19 prior + 2 new; no regressions).

- [ ] **Step 7: Commit**

```powershell
git add syncer/app/sync.py syncer/app/db.py syncer/tests/test_sync.py
git commit -m "fix: reset delete-grace clock when a missing file reappears"
```

---

### Task 3: Bug fix — unowned-row detection must cover the whole workspace

**Bug:** `run_cycle` computes `unowned = set(graph_index) - set(state)`, but `graph_index` was already filtered by `graph_files_for_mapping` to rows whose `source_path` starts with the mapping's folder prefix. A row uploaded into the workspace with any other `source_path` (e.g. a manual internal upload) never appears in `graph_index`, so the workspace is treated as healthy even though the syncer does not exclusively own it — contradicting the documented guarantee.

**Files:**
- Modify: `syncer/app/sync.py:74-83`
- Test: `syncer/tests/test_sync.py`

- [ ] **Step 1: Write the failing test**

Append to `syncer/tests/test_sync.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_sync.py::test_foreign_prefix_graph_row_degrades_health -q`
Expected: FAIL — `health_degraded` is `False` and the owned doc gets deleted (`graph.deletes` non-empty).

- [ ] **Step 3: Compute `unowned` from the full workspace index**

In `syncer/app/sync.py`, replace:

```python
    state = repo.get_state(mapping_id)
    graph_index = graph_files_for_mapping(
        mapping, graph.list_files(mapping["workspace_id"])
    )
```

with:

```python
    state = repo.get_state(mapping_id)
    all_graph_files = graph.list_files(mapping["workspace_id"])
    graph_index = graph_files_for_mapping(mapping, all_graph_files)
```

and replace:

```python
    unowned = set(graph_index) - set(state)
```

with:

```python
    prefix = mapping["nextcloud_path"].strip("/") + "/"
    owned_sources = {prefix + rel for rel in state}
    unowned = {sp for sp in all_graph_files if sp not in owned_sources}
```

Note: `unowned_paths` in the `health_degraded` event now carries full `source_path` values instead of mapping-relative paths. That is intentional — a foreign row has no meaningful relative path.

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest -q`
Expected: `22 passed`. Pay attention to `test_unowned_existing_graph_file_is_blocked` — it must still pass (an in-prefix unowned row is still unowned under the new definition).

- [ ] **Step 5: Commit**

```powershell
git add syncer/app/sync.py syncer/tests/test_sync.py
git commit -m "fix: detect unowned graph rows across the whole workspace, not just the mapping prefix"
```

---

### Task 4: Exponential retry backoff for failed ingests

**Problem:** A file whose ingest permanently fails is retried on every cycle (default 30 s): full re-download from Nextcloud plus full re-ingest in PolyGraphRAG, which re-runs LLM processing and burns provider cost indefinitely. `sync_state.retry_count` is tracked but unused.

**Design:** Before re-attempting a `failed` row whose remote etag is unchanged since the failed attempt (`pending_etag == remote.etag`), require `now - updated_epoch >= min(3600, 60 * 2**min(retry_count, 6))`. An etag change (user fixed the file) bypasses the backoff immediately. Rows without `updated_epoch` (older DB rows, minimal fakes) retry immediately — fail-open keeps behavior backward compatible.

**Files:**
- Modify: `syncer/app/db.py` (`get_state` gains `updated_epoch`)
- Modify: `syncer/app/sync.py` (constants + backoff check)
- Test: `syncer/tests/test_sync.py`

- [ ] **Step 1: Write the failing tests**

Append to `syncer/tests/test_sync.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_sync.py -q -k backoff or -k bypasses` — simpler: `python -m pytest tests/test_sync.py -q`
Expected: `test_failed_ingest_backs_off_then_retries` FAILS (`early.ingested == 1`, no deferral). `test_etag_change_bypasses_backoff` may already pass — that's fine, it pins the bypass behavior.

- [ ] **Step 3: Implement the backoff**

In `syncer/app/sync.py`, add module constants below `log = logging.getLogger(...)`:

```python
RETRY_BACKOFF_BASE_SECS = 60.0
RETRY_BACKOFF_CAP_SECS = 3600.0
```

Then, inside the per-path loop, directly **after** the synced/no-op block:

```python
        if (
            cached
            and indexed
            and cached.get("sync_status") == "synced"
            and cached.get("remote_etag") == remote.etag
            and cached.get("doc_id") == indexed.doc_id
            and cached.get("content_hash") == indexed.content_hash
        ):
            continue
```

and **before** the `try:` that reads/ingests, insert:

```python
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
```

- [ ] **Step 4: Expose `updated_epoch` from the real repository**

In `syncer/app/db.py`, in `get_state`, replace the SELECT with:

```python
            rows = conn.execute(
                "SELECT *, extract(epoch FROM pending_delete_since) AS pending_epoch, "
                "extract(epoch FROM updated_at) AS updated_epoch "
                "FROM sync_state WHERE mapping_id=%s",
                (mapping_id,),
            ).fetchall()
```

(`mark_failed` already sets `updated_at=now()`, so the failure timestamp is the backoff anchor; no schema change needed.)

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q`
Expected: `24 passed`. In particular `test_failed_replacement_keeps_old_graph_document` must still pass — its `FakeRepo.mark_failed` leaves `updated_epoch` unset, so the fail-open path retries immediately.

- [ ] **Step 6: Commit**

```powershell
git add syncer/app/sync.py syncer/app/db.py syncer/tests/test_sync.py
git commit -m "feat: exponential backoff for failed ingest retries (cap 1h, etag change bypasses)"
```

---

### Task 5: Dependency injection for `create_app` + API test harness

**Problem:** `create_app` hard-constructs `Repository`, `WebDavClient`, `PolyGraphClient`, and `Scheduler`, so the auth middleware and route logic (validation flows, immutability, 409s) have zero tests — despite concept.md §8 layer 4 promising them.

**Files:**
- Modify: `syncer/app/main.py:23-34` (signature + construction)
- Create: `syncer/tests/test_api.py`

- [ ] **Step 1: Add injection parameters to `create_app`**

In `syncer/app/main.py`, replace:

```python
def create_app(config: Config | None = None) -> FastAPI:
    cfg = config or Config.from_env()
    repo = Repository(cfg.postgres_dsn)
    webdav = WebDavClient(
        cfg.nextcloud_url, cfg.nextcloud_user, cfg.nextcloud_password
    )
    graph = PolyGraphClient(
        cfg.polygraphrag_url,
        cfg.polygraphrag_api_token,
        ingest_timeout=cfg.ingest_timeout,
    )
    scheduler = Scheduler(cfg, repo, webdav, graph)
```

with:

```python
def create_app(
    config: Config | None = None,
    *,
    repo: Repository | None = None,
    webdav: WebDavClient | None = None,
    graph: PolyGraphClient | None = None,
    scheduler: Scheduler | None = None,
) -> FastAPI:
    cfg = config or Config.from_env()
    repo = repo or Repository(cfg.postgres_dsn)
    webdav = webdav or WebDavClient(
        cfg.nextcloud_url, cfg.nextcloud_user, cfg.nextcloud_password
    )
    graph = graph or PolyGraphClient(
        cfg.polygraphrag_url,
        cfg.polygraphrag_api_token,
        ingest_timeout=cfg.ingest_timeout,
    )
    scheduler = scheduler or Scheduler(cfg, repo, webdav, graph)
```

Nothing else in the function changes. The module-level `app = create_app()` stays.

- [ ] **Step 2: Create the API test harness with failing tests**

Create `syncer/tests/test_api.py`:

```python
from __future__ import annotations

from datetime import datetime, timezone

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
        now = datetime.now(timezone.utc)
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
        row["updated_at"] = datetime.now(timezone.utc)
        return dict(row)

    def archive_mapping(self, mapping_id):
        row = self.mappings[mapping_id]
        row["archived_at"] = datetime.now(timezone.utc)
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

    def start(self):
        pass

    def stop(self):
        pass

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
```

- [ ] **Step 3: Run the new tests**

Run: `python -m pytest tests/test_api.py -q`
Expected: all 5 PASS once Step 1's DI change is in (they exercise existing route logic). If any fails, debug the route/stub mismatch before continuing — do not weaken assertions.

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest -q`
Expected: `29 passed`

- [ ] **Step 5: Commit**

```powershell
git add syncer/app/main.py syncer/tests/test_api.py
git commit -m "test: add API-layer test harness via create_app dependency injection"
```

---

### Task 6: Introspection endpoints — `GET /mappings/{id}/state` and `GET /mappings/{id}/events`

**Problem:** `sync_events` is the designated audit trail and `sync_state` records per-file status, but neither is readable through the API — operators need raw psql. Also `sync_events` has no index for per-mapping reads.

**Files:**
- Modify: `syncer/app/db.py` (`SCHEMA` index, `list_state`, `list_events`)
- Modify: `syncer/app/models.py` (`FileStateView`, `EventView`)
- Modify: `syncer/app/main.py` (two routes)
- Test: `syncer/tests/test_api.py`

- [ ] **Step 1: Write the failing test**

Append to `syncer/tests/test_api.py`. First extend `ApiRepo` — add to `__init__`:

```python
        self.state_rows: dict[int, list[dict]] = {}
        self.event_rows: dict[int, list[dict]] = {}
```

and add methods to `ApiRepo`:

```python
    def list_state(self, mapping_id):
        return list(self.state_rows.get(mapping_id, []))

    def list_events(self, mapping_id, limit=100):
        rows = sorted(self.event_rows.get(mapping_id, []), key=lambda r: r["id"], reverse=True)
        return rows[:limit]
```

Then append the test:

```python
def test_state_and_events_endpoints(api):
    client, repo, _ = api
    mapping_id = client.post("/mappings", json=CREATE_BODY).json()["id"]
    now = datetime.now(timezone.utc)
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_api.py::test_state_and_events_endpoints -q`
Expected: FAIL with 404 on `/mappings/{id}/state` (route does not exist).

- [ ] **Step 3: Add repository methods and the events index**

In `syncer/app/db.py`, append to the `SCHEMA` string (before the closing `"""`):

```sql
CREATE INDEX IF NOT EXISTS sync_events_mapping_id_id_idx
  ON sync_events (mapping_id, id DESC);
```

Add to `Repository` (after `get_state`):

```python
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
```

- [ ] **Step 4: Add response models**

In `syncer/app/models.py`, append:

```python
class FileStateView(BaseModel):
    rel_path: str
    sync_status: str
    doc_id: Optional[str] = None
    content_hash: Optional[str] = None
    remote_etag: Optional[str] = None
    retry_count: int = 0
    last_error: Optional[str] = None
    pending_delete_since: Optional[datetime] = None
    updated_at: datetime


class EventView(BaseModel):
    id: int
    ts: datetime
    event_type: str
    rel_path: Optional[str] = None
    detail: Optional[dict] = None
```

- [ ] **Step 5: Add the routes**

In `syncer/app/main.py`, update the models import:

```python
from app.models import (
    EventView, FileStateView, MappingCreate, MappingPatch, MappingView, RunAccepted,
)
```

Add after the `read_mapping` route:

```python
    @application.get("/mappings/{mapping_id}/state", response_model=list[FileStateView])
    def read_mapping_state(mapping_id: int):
        get_mapping(mapping_id)
        return repo.list_state(mapping_id)

    @application.get("/mappings/{mapping_id}/events", response_model=list[EventView])
    def read_mapping_events(mapping_id: int, limit: int = 100):
        if repo.get_mapping(mapping_id, include_archived=True) is None:
            raise HTTPException(404, "mapping not found")
        return repo.list_events(mapping_id, max(1, min(limit, 1000)))
```

(Events stay readable for archived mappings — that's the audit trail; state uses the normal 404-when-archived lookup.)

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest -q`
Expected: `30 passed`

- [ ] **Step 7: Commit**

```powershell
git add syncer/app/db.py syncer/app/models.py syncer/app/main.py syncer/tests/test_api.py
git commit -m "feat: expose per-mapping sync state and audit events via API; index sync_events"
```

---

### Task 7: Remove duplicated dead check in the PATCH handler

**Problem:** In `update_mapping` (`syncer/app/main.py`), the `ownership_change` guard already raises 409 when `nextcloud_path` changes while state exists. The nested block immediately after repeats the identical `count_state` check — it is unreachable dead code that obscures the real flow.

**Files:**
- Modify: `syncer/app/main.py` (inside `update_mapping`)

- [ ] **Step 1: Replace the redundant block**

Replace:

```python
        if new_path is not None and new_path != current["nextcloud_path"]:
            if repo.count_state(mapping_id):
                raise HTTPException(
                    409,
                    "nextcloud_path cannot change after synchronization; create a new mapping",
                )
            try:
                webdav.validate_folder(new_path)
            except FileNotFoundError as exc:
                raise HTTPException(422, str(exc)) from exc
            except Exception as exc:
                raise HTTPException(502, f"Nextcloud validation failed: {exc}") from exc
```

with:

```python
        if new_path is not None and new_path != current["nextcloud_path"]:
            try:
                webdav.validate_folder(new_path)
            except FileNotFoundError as exc:
                raise HTTPException(422, str(exc)) from exc
            except Exception as exc:
                raise HTTPException(502, f"Nextcloud validation failed: {exc}") from exc
```

- [ ] **Step 2: Run the full suite (the Task 5 PATCH test pins this behavior)**

Run: `python -m pytest -q`
Expected: `30 passed`

- [ ] **Step 3: Commit**

```powershell
git add syncer/app/main.py
git commit -m "refactor: drop duplicated immutability check in PATCH handler"
```

---

### Task 8: Tooling — pyproject.toml with ruff + mypy

Per the repo owner's global engineering baseline: tool-owned style, single config home. Keep `requirements.txt` as the canonical dependency source (the Dockerfile uses it); `pyproject.toml` holds tool config only.

**Files:**
- Create: `syncer/pyproject.toml`
- Delete: `syncer/pytest.ini` (config moves into pyproject)
- Modify: `syncer/requirements-dev.txt`

- [ ] **Step 1: Create `syncer/pyproject.toml`**

```toml
[project]
name = "nc-rag-sync"
version = "1.0.0"
description = "Nextcloud to PolyGraphRAG synchronizer"
requires-python = ">=3.11"

[tool.ruff]
line-length = 110
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]

[tool.mypy]
python_version = "3.11"
ignore_missing_imports = true
warn_unused_ignores = true
no_implicit_optional = true

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"
```

- [ ] **Step 2: Delete `syncer/pytest.ini` and update dev requirements**

Delete `syncer/pytest.ini`. Replace `syncer/requirements-dev.txt` with:

```text
-r requirements.txt
pytest==8.4.1
ruff==0.12.3
mypy==1.16.1
```

- [ ] **Step 3: Install tools, run ruff, and fix findings**

Run from `syncer/`:
```powershell
pip install -r requirements-dev.txt
ruff check .
```
Fix every reported finding **mechanically** (import sorting, unused imports, modern syntax). Rules to follow:
- Do not restructure logic to satisfy a lint rule; if a rule fights an intentional pattern (e.g. the `# noqa: BLE001`-style broad excepts in `sync.py` — note `BLE` is not in the selected set, so these should not fire), suppress with a targeted `# noqa: <RULE>` instead.
- After fixing, re-run `ruff check .` until clean, then run `python -m pytest -q` — still `30 passed`.

- [ ] **Step 4: Run mypy and fix findings**

Run from `syncer/`: `mypy app`
Fix reported errors with minimal, honest annotations. If a finding requires invasive refactoring, add a scoped `# type: ignore[<code>]` with the specific error code rather than restructuring. Re-run until clean, then `python -m pytest -q` — still `30 passed`.

- [ ] **Step 5: Commit**

```powershell
git add syncer/pyproject.toml syncer/requirements-dev.txt
git rm syncer/pytest.ini
git add -u syncer
git commit -m "chore: add ruff + mypy tooling via pyproject; consolidate pytest config"
```

---

### Task 9: CI workflow

**Files:**
- Create: `.github/workflows/ci.yml`

- [ ] **Step 1: Create the workflow**

```yaml
name: ci

on:
  push:
  pull_request:

jobs:
  syncer:
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: syncer
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
          cache: pip
          cache-dependency-path: syncer/requirements-dev.txt
      - run: pip install -r requirements-dev.txt
      - run: ruff check .
      - run: mypy app
      - run: python -m pytest -q
```

- [ ] **Step 2: Verify each CI command passes locally**

Run from `syncer/`: `ruff check .` then `mypy app` then `python -m pytest -q`
Expected: all clean / `30 passed`. (There is no remote configured, so the workflow cannot run in Actions yet — local green is the acceptance gate.)

- [ ] **Step 3: Commit**

```powershell
git add .github/workflows/ci.yml
git commit -m "ci: lint, type-check, and test the syncer on push and PR"
```

---

### Task 10: Dockerfile — run as non-root

**Files:**
- Modify: `syncer/Dockerfile`

- [ ] **Step 1: Replace `syncer/Dockerfile` contents**

```dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --timeout 120 --retries 5 -r requirements.txt

COPY app ./app
# --create-home: the README's in-container test command pip-installs pytest,
# which needs a writable user site under $HOME once we drop root.
RUN useradd --uid 10001 --create-home appuser
USER appuser
ENV PYTHONUNBUFFERED=1
EXPOSE 9630
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9630"]
```

- [ ] **Step 2: Verify the image builds and the documented test command still works**

Requires Docker running. From repo root:
```powershell
docker compose build nc-rag-sync
docker run --rm -v "${PWD}\syncer:/src" -w /src polygraphrag-nextcloud-nc-rag-sync `
  sh -c "pip install pytest==8.4.1 && python -m pytest -q"
```
Expected: build succeeds; test run prints `30 passed`. If Docker is unavailable in this environment, note that in the task result and mark this verification for the user.

- [ ] **Step 3: Commit**

```powershell
git add syncer/Dockerfile
git commit -m "hardening: run syncer container as non-root user"
```

---

### Task 11: Documentation alignment

**Files:**
- Modify: `README.md`, `docs/concept.md`, `CLAUDE.md`

- [ ] **Step 1: README — auth caveat and introspection endpoints**

In `README.md`, replace the paragraph:

```markdown
The syncer API requires `Authorization: Bearer <SYNCER_API_TOKEN>`. PolyGraphRAG auth is enabled
when `POLYGRAPHRAG_API_TOKENS` is non-empty.
```

with:

```markdown
The syncer API requires `Authorization: Bearer <SYNCER_API_TOKEN>` whenever `SYNCER_API_TOKEN` is
set. An empty token disables syncer auth entirely — acceptable only for loopback local development;
the VPS override refuses to start without one. PolyGraphRAG auth is enabled when
`POLYGRAPHRAG_API_TOKENS` is non-empty.
```

In the "Useful calls" code block, append two lines:

```powershell
Invoke-RestMethod http://127.0.0.1:19630/mappings/1/state -Headers $headers
Invoke-RestMethod "http://127.0.0.1:19630/mappings/1/events?limit=50" -Headers $headers
```

- [ ] **Step 2: concept.md — API contract, backoff, overlap wording**

In `docs/concept.md` §3 API contract list, after the `GET /mappings/{id}` line add:

```markdown
- `GET /mappings/{id}/state` (per-file sync status, doc ids, errors, pending deletes)
- `GET /mappings/{id}/events?limit=N` (recent audit events, newest first; readable for archived
  mappings)
```

In §4, after the paragraph ending "marks the file synced.", add:

```markdown
A file whose ingest failed and whose source etag is unchanged is retried with exponential backoff
(60 s doubling per recorded retry, capped at one hour) so a permanently failing document cannot
burn provider cost every poll cycle. A source change (new etag) retries immediately.
```

In §3, replace the sentence:

```markdown
This prevents two mappings—or a mapping and manual uploads—from claiming/deleting the same graph
rows.
```

with:

```markdown
This prevents two mappings—or a mapping and manual uploads—from claiming/deleting the same graph
rows. Workspace exclusivity is the enforced overlap rule; the same Nextcloud folder may
deliberately feed multiple workspaces, each with its own independent copies.
```

- [ ] **Step 3: CLAUDE.md — tooling line and endpoint list**

In `CLAUDE.md`, replace:

```markdown
There is no linter/formatter config checked in; match the existing style (see below).
```

with:

```markdown
Lint/type tooling lives in `syncer/pyproject.toml`: run `ruff check .` and `mypy app` from
`syncer/` before committing (CI enforces both).
```

and replace the route enumeration sentence fragment:

```markdown
(`POST /mappings`, `PATCH`, `/run`, `/enable`, `/disable`, `DELETE`
archives, `/restore`).
```

with:

```markdown
(`POST /mappings`, `PATCH`, `/run`, `/enable`, `/disable`, `DELETE`
archives, `/restore`; read-only `GET /mappings/{id}/state` and `/events`).
```

- [ ] **Step 4: Commit**

```powershell
git add README.md docs/concept.md CLAUDE.md
git commit -m "docs: document auth caveat, introspection endpoints, retry backoff, overlap semantics"
```

---

### Task 12: Self-healing — the scheduler loop must survive any outage, and `/health` must see it

**Problem (F13):** `Scheduler._loop` calls `_take_mappings()` outside the per-mapping try/except. A Postgres outage at that instant raises out of the `while` loop and kills the daemon thread permanently. The process keeps serving HTTP; once the DB recovers, `/health` reports "ok" again while syncing is dead until a container restart. Two fixes: (a) the loop must never die — swallow, log, and retry next tick; (b) `/health` must report scheduler liveness and return 503 when the thread is dead, so the outage is at least visible and alertable.

**Files:**
- Modify: `syncer/app/sync.py` (`Scheduler._loop`, new `is_alive`)
- Modify: `syncer/app/main.py` (`health` route)
- Test: `syncer/tests/test_sync.py`, `syncer/tests/test_api.py`

- [ ] **Step 1: Write the failing scheduler test**

In `syncer/tests/test_sync.py`, extend the imports at the top of the file:

```python
import time
from types import SimpleNamespace

from app.sync import Scheduler, run_cycle
```

(keep the existing imports; only `run_cycle` was imported from `app.sync` before). Then append:

```python
def test_scheduler_survives_repo_outage_and_keeps_polling():
    class FlakyRepo:
        def __init__(self):
            self.calls = 0

        def list_mappings(self, enabled_only=True):
            self.calls += 1
            raise RuntimeError("db down")

    config = SimpleNamespace(
        poll_interval=0.01, delete_grace_secs=0.0, max_file_bytes=0,
        canary_autocreate=True, events_retention_days=0.0,
    )
    repo = FlakyRepo()
    scheduler = Scheduler(config, repo, None, None)
    scheduler.start()
    time.sleep(0.15)
    try:
        assert scheduler.is_alive()
        assert repo.calls >= 2  # kept polling straight through the failures
    finally:
        scheduler.stop()
```

(`events_retention_days` is unused until Task 14 adds pruning to the loop; including it now keeps this test valid afterwards.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_sync.py::test_scheduler_survives_repo_outage_and_keeps_polling -q`
Expected: FAIL — `AttributeError` (`Scheduler` has no `is_alive`); after adding only the method it would still fail on `is_alive()` being `False` / `calls >= 2`, because the first `RuntimeError` killed the thread.

- [ ] **Step 3: Make the loop unkillable and add `is_alive`**

In `syncer/app/sync.py`, add to `Scheduler` (after the `request` method):

```python
    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())
```

In `_loop`, replace:

```python
    def _loop(self) -> None:
        while not self._stop.is_set():
            for mapping in self._take_mappings():
```

with:

```python
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                mappings = self._take_mappings()
            except Exception as exc:  # noqa: BLE001
                log.warning("scheduler could not list mappings; retrying next tick: %s", exc)
                mappings = []
            for mapping in mappings:
```

(the rest of the loop body is unchanged — the per-mapping try/except already self-heals individual cycle failures).

- [ ] **Step 4: Report scheduler liveness from `/health`**

In `syncer/app/main.py`, replace the `health` route with:

```python
    @application.get("/health")
    def health(response: Response):
        try:
            repo.list_mappings()
            database = "ok"
        except Exception:
            database = "error"
        scheduler_ok = scheduler.is_alive()
        healthy = database == "ok" and scheduler_ok
        if not healthy:
            response.status_code = 503
        return {
            "status": "ok" if healthy else "degraded",
            "database": database,
            "scheduler": "ok" if scheduler_ok else "dead",
        }
```

The compose healthcheck already probes `/health`, so a dead scheduler now flips the container to `unhealthy` in `docker compose ps` (visible/alertable; Docker alone does not auto-restart unhealthy containers — the loop hardening in Step 3 is the actual self-healing, this is the tripwire).

- [ ] **Step 5: Write the API-level liveness test**

In `syncer/tests/test_api.py`, extend `StubScheduler`:

```python
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
```

Append the test:

```python
def test_health_reports_scheduler_death(api):
    client, _, scheduler = api
    assert client.get("/health").json()["scheduler"] == "ok"
    scheduler.alive = False
    degraded = client.get("/health")
    assert degraded.status_code == 503
    assert degraded.json()["scheduler"] == "dead"
```

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest -q`
Expected: `32 passed`

- [ ] **Step 7: Commit**

```powershell
git add syncer/app/sync.py syncer/app/main.py syncer/tests/test_sync.py syncer/tests/test_api.py
git commit -m "fix: scheduler loop survives DB outages; /health reports scheduler liveness"
```

---

### Task 13: Self-healing — never upload a duplicate while a previous attempt's job is still running

**Problem (F14):** `run_cycle` re-uploads a file whenever its state row isn't clean, even if the previous attempt's ingest job is still running server-side (crash before the poll finished, or `SYNC_INGEST_TIMEOUT` elapsed on a slow document). The earlier job eventually completes and its document becomes a permanent orphan — unowned, undeletable by the syncer, and invisible to the unowned guard because `list_files` dedupes by `source_path`. Each duplicate also re-runs paid LLM ingestion.

**Design:** the file index (`GET /workspace/{id}/files`) exposes each row's `status`. If the newest indexed row for this path is **non-terminal** and our recorded `pending_hash` matches the bytes we just read, the previous attempt is still in flight for identical content — defer instead of re-uploading. The next healthy cycle after the job reaches `done` adopts it by content hash (existing behavior, pinned by a new test below). If the job reaches `failed`, the row's status is terminal and re-ingest proceeds as today.

**Files:**
- Modify: `syncer/app/sync.py` (import + in-flight deferral)
- Test: `syncer/tests/test_sync.py`

- [ ] **Step 1: Write the tests (one failing, one pinning existing recovery)**

Append to `syncer/tests/test_sync.py`:

```python
def pending_row(pending_etag, pending_hash):
    return {
        "rel_path": "guide.md",
        "remote_etag": None,
        "content_hash": None,
        "doc_id": None,
        "superseded_doc_id": None,
        "sync_status": "pending",
        "retry_count": 0,
        "last_error": None,
        "pending_epoch": None,
        "pending_etag": pending_etag,
        "pending_hash": pending_hash,
        "updated_epoch": None,
    }


def test_inflight_job_defers_instead_of_duplicate_upload():
    data = b"large slow document"
    digest = hashlib.sha256(data).hexdigest()
    webdav = FakeWebDav({"guide.md": (data, "e1")})
    graph, repo = FakeGraph(), FakeRepo()
    graph.files["Projects/Alpha/guide.md"] = GraphFile(
        "Projects/Alpha/guide.md", None, None, "processing"
    )
    repo.state["guide.md"] = pending_row("e1", digest)
    result = cycle(webdav, graph, repo)
    assert result.deferred == 1
    assert graph.ingests == []


def test_crashed_pending_ingest_adopts_completed_job_without_reupload():
    data = b"contract"
    digest = hashlib.sha256(data).hexdigest()
    webdav = FakeWebDav({"guide.md": (data, "e1")})
    graph, repo = FakeGraph(), FakeRepo()
    graph.files["Projects/Alpha/guide.md"] = GraphFile(
        "Projects/Alpha/guide.md", "doc-done", digest, "done"
    )
    repo.state["guide.md"] = pending_row("e1", digest)
    result = cycle(webdav, graph, repo)
    assert result.adopted == 1
    assert graph.ingests == []
    assert repo.state["guide.md"]["doc_id"] == "doc-done"
    assert repo.state["guide.md"]["sync_status"] == "synced"
```

- [ ] **Step 2: Run tests to verify status**

Run: `python -m pytest tests/test_sync.py -q`
Expected: `test_inflight_job_defers_instead_of_duplicate_upload` FAILS (`graph.ingests` has one entry — the duplicate upload). `test_crashed_pending_ingest_adopts_completed_job_without_reupload` PASSES already — it pins the existing crash-recovery adopt path, which had no coverage.

- [ ] **Step 3: Implement the in-flight deferral**

In `syncer/app/sync.py`, add to the imports:

```python
from app.polygraph import TERMINAL
```

Then inside `run_cycle`'s per-path `try:` block, directly **after** the adopt branch (the block ending `result.adopted += 1` / `continue`) and **before** `old_doc_id = cached.get("doc_id") if cached else None` / `repo.mark_pending(...)`, insert:

```python
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
```

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest -q`
Expected: `34 passed`

- [ ] **Step 5: Commit**

```powershell
git add syncer/app/sync.py syncer/tests/test_sync.py
git commit -m "fix: defer instead of duplicate-uploading while a prior ingest job is in flight"
```

---

### Task 14: Self-healing — bounded `sync_events` via retention pruning

**Problem (F15a):** the audit table grows forever. Add `SYNC_EVENTS_RETENTION_DAYS` (default 90; `0` disables) and prune from the scheduler loop at most once per 24 h so the table stays bounded without operator action.

**Files:**
- Modify: `syncer/app/config.py`
- Modify: `syncer/app/db.py` (`prune_events`)
- Modify: `syncer/app/sync.py` (`Scheduler._maybe_prune_events`)
- Modify: `syncer/tests/test_api.py` (`make_config` gains the new field)
- Modify: `docker-compose.yml`, `.env.example`, `.env.vps.example`
- Test: `syncer/tests/test_sync.py`

- [ ] **Step 1: Write the failing test**

Append to `syncer/tests/test_sync.py`:

```python
def test_event_pruning_runs_once_per_interval_and_can_be_disabled():
    class PruneRepo:
        def __init__(self):
            self.pruned = []

        def prune_events(self, days):
            self.pruned.append(days)
            return 3

    def config(days):
        return SimpleNamespace(
            poll_interval=0.01, delete_grace_secs=0.0, max_file_bytes=0,
            canary_autocreate=True, events_retention_days=days,
        )

    repo = PruneRepo()
    scheduler = Scheduler(config(30.0), repo, None, None)
    scheduler._maybe_prune_events()
    scheduler._maybe_prune_events()
    assert repo.pruned == [30.0]  # second call inside the 24h window is a no-op

    disabled_repo = PruneRepo()
    disabled = Scheduler(config(0.0), disabled_repo, None, None)
    disabled._maybe_prune_events()
    assert disabled_repo.pruned == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_sync.py::test_event_pruning_runs_once_per_interval_and_can_be_disabled -q`
Expected: FAIL with `AttributeError: 'Scheduler' object has no attribute '_maybe_prune_events'`

- [ ] **Step 3: Add config field**

In `syncer/app/config.py`, add to the `Config` dataclass fields:

```python
    events_retention_days: float
```

and to the `cls(...)` call in `from_env`:

```python
            events_retention_days=float(os.environ.get("SYNC_EVENTS_RETENTION_DAYS", "90")),
```

- [ ] **Step 4: Add the repository method**

In `syncer/app/db.py`, after `clear_pending_deletes`, add:

```python
    def prune_events(self, older_than_days: float) -> int:
        with self._connect() as conn:
            result = conn.execute(
                "DELETE FROM sync_events WHERE ts < now() - make_interval(days => %s)",
                (older_than_days,),
            )
            return result.rowcount
```

- [ ] **Step 5: Wire pruning into the scheduler**

In `syncer/app/sync.py`, add to `Scheduler`:

```python
    PRUNE_INTERVAL_SECS = 24 * 3600.0
```

as a class attribute, add `self._last_prune = 0.0` to `__init__`, and add the method (after `is_alive`):

```python
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
```

Note the guard is `self._last_prune and ...` so the very first loop iteration prunes immediately; a pruning failure only logs — it must never take the loop down.

In `_loop`, call it as the first statement inside the `while`:

```python
    def _loop(self) -> None:
        while not self._stop.is_set():
            self._maybe_prune_events()
            try:
                mappings = self._take_mappings()
```

Note: `_maybe_prune_events` sets `_last_prune` before calling the repo and swallows all exceptions, so a failing prune cannot re-introduce the Task 12 crash vector or hot-loop against a broken DB.

- [ ] **Step 6: Update the Task 5 test config and deployment files**

In `syncer/tests/test_api.py`, add to the `Config(...)` call in `make_config`:

```python
        events_retention_days=90.0,
```

In `docker-compose.yml`, in the `nc-rag-sync` service `environment` block, after `SYNC_CANARY_AUTOCREATE`, add:

```yaml
      SYNC_EVENTS_RETENTION_DAYS: ${SYNC_EVENTS_RETENTION_DAYS:-90}
```

In `.env.example` and `.env.vps.example`, after the `SYNC_CANARY_AUTOCREATE` line, add:

```dotenv
SYNC_EVENTS_RETENTION_DAYS=90
```

- [ ] **Step 7: Run the full suite**

Run: `python -m pytest -q`
Expected: `35 passed`

- [ ] **Step 8: Commit**

```powershell
git add syncer/app/config.py syncer/app/db.py syncer/app/sync.py syncer/tests/test_sync.py syncer/tests/test_api.py docker-compose.yml .env.example .env.vps.example
git commit -m "feat: bounded sync_events via SYNC_EVENTS_RETENTION_DAYS pruning (default 90 days)"
```

---

### Task 15: Self-healing — bounded container logs (compose log rotation)

**Problem (F15b):** no `logging` configuration in compose; Docker's default json-file driver never rotates, so long-running services (especially the chatty syncer and Caddy access logs) eventually fill the disk.

**Files:**
- Modify: `docker-compose.yml`
- Modify: `docker-compose.vps.yml`

- [ ] **Step 1: Add a shared logging anchor and apply it to every service**

In `docker-compose.yml`, after the `name: polygraphrag-nextcloud` line, add:

```yaml
x-logging: &default-logging
  driver: json-file
  options:
    max-size: 10m
    max-file: "5"
```

Then add this line to **each** of the six services (`postgres`, `polygraphrag`, `redis`, `nextcloud`, `nextcloud-cron`, `nc-rag-sync`), at the same indentation level as their `restart:` key:

```yaml
    logging: *default-logging
```

- [ ] **Step 2: Apply the same limits to Caddy in the VPS override**

`docker-compose.vps.yml` is a separate file, so the anchor must be repeated there. At the top of the file (before `services:`), add:

```yaml
x-logging: &default-logging
  driver: json-file
  options:
    max-size: 10m
    max-file: "5"
```

and add to the `caddy` service (same indentation as its `restart:` key):

```yaml
    logging: *default-logging
```

- [ ] **Step 3: Verify both compose files render**

From repo root (uses the existing local `.env`):

```powershell
docker compose config --quiet
```

Expected: exit code 0, no output. For the VPS overlay (its required variables are not in the local `.env`, so supply dummies for the render check only):

```powershell
$env:POSTGRES_DATA_DIR="x"; $env:POLYGRAPHRAG_DATA_DIR="x"; $env:REDIS_DATA_DIR="x"
$env:NEXTCLOUD_HTML_DIR="x"; $env:CADDY_DATA_DIR="x"; $env:CADDY_CONFIG_DIR="x"
$env:NEXTCLOUD_DOMAIN="cloud.example.com"; $env:POLYGRAPHRAG_DOMAIN="rag.example.com"
$env:ACME_EMAIL="ops@example.com"; $env:POLYGRAPHRAG_API_TOKENS="dummy"; $env:SYNCER_API_TOKEN="dummy"
docker compose -f docker-compose.yml -f docker-compose.vps.yml config --quiet
```

Expected: exit code 0. Then clear the dummies (`Remove-Item Env:POSTGRES_DATA_DIR` etc.) or just note they are session-local. If Docker is unavailable in this environment, note it and mark this verification for the user.

- [ ] **Step 4: Commit**

```powershell
git add docker-compose.yml docker-compose.vps.yml
git commit -m "ops: rotate container logs (10m x 5 files) so disks cannot fill"
```

---

## Final verification (run after all tasks)

- [ ] From `syncer/`: `ruff check .` → clean; `mypy app` → clean; `python -m pytest -q` → `35 passed`.
- [ ] `git log --oneline` shows one commit per task on `main`.
- [ ] `git status` is clean and `.env` was never staged.
- [ ] If Docker is available: `docker compose build nc-rag-sync` and `docker compose config --quiet` succeed.
- [ ] Self-healing spot-check against the findings: F13 (scheduler survives a dead DB and `/health` exposes thread death), F14 (no duplicate upload while a job is in flight; completed jobs adopted), F15 (`sync_events` pruned by retention; container logs rotated) — each is pinned by a test or a rendered config, not just code review.

## Deferred items (documented, deliberately not in this plan)

These need either a user decision or information not in the repo. Do **not** implement them without asking:

1. **Caddy public route: blocklist → allowlist.** The current 403 rules enumerate destructive paths; any new destructive upstream route ships exposed. Switching to an allowlist requires enumerating PolyGraphRAG's query/read routes from the running service's OpenAPI spec (`/docs`) — the upstream source is not in this repo.
2. **DB connection pooling** (`psycopg_pool`): every `Repository` call opens a fresh connection. Works at current scale; pooling interacts with the advisory-lock-held-for-a-whole-cycle pattern and needs care.
3. **Scheduler lock-contention re-queue**: a manual `/run` request that arrives while another process holds the mapping's advisory lock is dropped until the next poll tick.
4. **Streaming large files**: WebDAV read and ingest upload buffer whole files (up to 200 MB) in memory.
5. **Resume in-flight ingest jobs by `job_id`**: Task 13 already prevents duplicate uploads by deferring while a non-terminal job exists for the same bytes; storing the PolyGraphRAG `job_id` in `sync_state` would additionally let a restarted syncer poll the in-flight job to completion instead of waiting for the file index to reflect it. Only worth it if in-flight recovery latency matters.
6. **Public query rate limiting / spend alerts**: acknowledged in docs/security.md as an external control; vanilla Caddy lacks rate limiting.
7. **PolyGraphRAG-side metadata accumulation**: failed ingest attempts leave historical rows in the upstream file index (the client already dedupes by newest). Cleanup belongs upstream — this repo cannot fix it.
8. **Pre-existing orphaned documents**: any duplicate-upload orphans created *before* Task 13 share a `source_path` with an owned document and are invisible to the unowned guard. If suspected, the documented recovery is rebuilding the workspace from the authoritative Nextcloud corpus (docs/security.md, incident recovery).
