# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A standalone document-to-knowledge-graph pipeline:

```text
Nextcloud folder -> nc-rag-sync -> PolyGraphRAG workspace/graph
```

Nextcloud is the source of truth for document bytes; a PolyGraphRAG workspace is a derived,
rebuildable projection. The only custom code in this repo is the **syncer** (`syncer/`, service
`nc-rag-sync`) — a FastAPI app that owns folder-to-workspace mappings and continuously reconciles
them. PolyGraphRAG, its Postgres distribution, Nextcloud, Redis, and Caddy are all pulled/published
images (`ghcr.io/oleksandr-kushnir/polygraphrag*`, `nextcloud:stable`, etc.); do not expect their
source here.

Read [docs/concept.md](docs/concept.md) (the authoritative design/behavior spec) and
[docs/security.md](docs/security.md) before changing sync semantics or a VPS deployment.

## Commands

Run the syncer test suite (pure Python, uses in-memory fakes — no live database/Nextcloud needed):

```powershell
# Inside the built image (matches CI/README):
docker run --rm -v "${PWD}\syncer:/src" -w /src `
  polygraphrag-nextcloud-nc-rag-sync `
  sh -c "pip install pytest==8.4.1 && python -m pytest -q"

# Or locally from syncer/ with deps installed (pip install -r requirements-dev.txt):
python -m pytest -q                    # all tests
python -m pytest tests/test_sync.py -q # one module
python -m pytest tests/test_sync.py::test_changed_file_deletes_old_doc_then_reingests
```

Local stack (endpoints are remapped in `.env` to coexist with other projects — Nextcloud
`:18088`, PolyGraphRAG `:19622`, syncer `:19630`, Postgres `:15432`):

```powershell
docker compose pull
docker compose build nc-rag-sync
docker compose up -d
docker compose ps
```

VPS stack (adds Caddy TLS + security-hardened overrides):

```bash
docker compose -f docker-compose.yml -f docker-compose.vps.yml up -d
```

There is no linter/formatter config checked in; match the existing style (see below).

## Architecture

### Mappings are runtime state, never config

A **mapping** is a row in the syncer's Postgres DB, created/changed only through the syncer's
token-protected HTTP API (`POST /mappings`, `PATCH`, `/run`, `/enable`, `/disable`, `DELETE`
archives, `/restore`). No folder-to-workspace relationship is ever read from environment variables.
Environment variables are reserved for deployment-wide concerns (DB/WebDAV credentials, service
URLs, API tokens, poll interval, size cap, timeouts, delete grace).

Invariants enforced at mapping creation/update ([syncer/app/main.py](syncer/app/main.py),
[syncer/app/models.py](syncer/app/models.py)):
- One mapping per PolyGraphRAG workspace; the target workspace must exist and be **empty** (or be
  created with `create_workspace=true`). This is what guarantees exclusive ownership of graph rows.
- `nextcloud_path` and `path_root` become immutable once any `sync_state` row exists (they are
  ownership/citation identity). Changing them requires a new mapping.

### The syncer package (`syncer/app/`)

- `main.py` — FastAPI app factory (`create_app`), bearer-token middleware (constant-time compare;
  `/health` is unauthenticated), all mapping routes, and lifespan wiring. `app = create_app()`.
- `config.py` — frozen `Config` dataclass built from env in `Config.from_env()`.
- `db.py` — `Repository`: raw psycopg (autocommit, `dict_row`), the full `SCHEMA` DDL
  (`sync_mappings`, `sync_state`, `sync_events`) applied idempotently at startup, and the
  `mapping_lock` Postgres advisory lock.
- `sync.py` — the reconciliation core. `run_cycle(...)` is a large pure-ish function (all I/O comes
  in through injected clients/repo) that decides ingest/no-op/replace/delete per file. `Scheduler`
  is a single background thread that serially processes mappings, triggered by `request(mapping_id)`
  or the poll interval.
- `polygraph.py` — `PolyGraphClient`: workspace list/create, file index, upload+poll ingest, delete.
- `webdav.py` — `WebDavClient`: recursive PROPFIND walk, etag cleaning, the `.nc-rag-sync-health`
  canary sentinel.
- `filters.py` / `models.py` — scope logic (`in_scope`) and Pydantic request/response models +
  extension allowlist.

### Reconciliation is safety-critical — preserve these guarantees when editing `sync.py`

The whole point of the design is to never destroy the last good copy of a graph document. When
touching `run_cycle`, keep:
- **Upload-first replacement.** A changed file is re-ingested; the old `doc_id` is deleted only
  after the new one reaches `done`. A failed replacement leaves the old document queryable.
- **Delete guards.** Deletes are authorized only on a healthy cycle. A cycle is `health_degraded`
  (and clears all pending-delete clocks) if: the WebDAV canary is missing/just-created, indexed
  files drop below `min_files`, missing files exceed `max_delete_fraction`, or PolyGraphRAG holds a
  graph row the syncer doesn't own (`unowned`). Any WebDAV error aborts the scan entirely.
- **Delete grace.** A missing source file must stay missing through `SYNC_DELETE_GRACE_SECS` of
  *healthy* scans before its `doc_id` is deleted.
- **Ownership.** `sync_state` (not filename/prefix matching) is the authority for which `doc_id`s the
  syncer owns; unowned graph rows are never adopted or deleted. Identity is `(mapping_id, rel_path)`,
  so a rename = new path + grace-delayed delete of the old path.
- **Version/lock checks.** Destructive actions re-check `mapping_is_current(id, version)` under a
  per-mapping advisory lock so a concurrent disable/patch/archive can't be raced.

`sync_events` is the audit trail; emit an event for every ingest/reingest/delete/degradation/failure.

### Isolation model (compose networks + DB roles)

Postgres holds three separate databases/roles — PolyGraphRAG (`ragdb`/`raguser`), Nextcloud
(`nextcloud`/`nextcloud`), syncer (`ncragsync`/`ncragsync`) — created by
[docker/postgres/init-databases.sh](docker/postgres/init-databases.sh). Services do not share DB
credentials. `internal: true` compose networks restrict who can reach Postgres/Redis/Nextcloud/
PolyGraphRAG; only `*-proxy` networks are external. All host ports bind `127.0.0.1`.

On the VPS, Caddy ([Caddyfile](Caddyfile)) is the only public surface: it serves Nextcloud and a
**read-only** PolyGraphRAG route that returns 403 for all `DELETE`s and for workspace-create/upload/
restore paths. The mapping API is deliberately never reverse-proxied — reach it over SSH tunnel/VPN
on loopback. `SYNC_CANARY_AUTOCREATE` is `false` on the VPS so the Nextcloud service account can be
read-only.

## Conventions

- Python targets 3.11; `from __future__ import annotations` at the top of every module. Type hints on
  public signatures, `@dataclass(frozen=True)` for value/config objects, Pydantic for API I/O.
- The syncer talks to Postgres with **raw psycopg and hand-written SQL** (no ORM). Keep the schema in
  `db.py`'s `SCHEMA` string and evolve it idempotently (`CREATE TABLE IF NOT EXISTS` / additive).
- Tests use hand-rolled in-memory fakes (`FakeWebDav`, `FakeGraph`, `FakeRepo` in
  `tests/test_sync.py`) rather than a live stack; add new reconciliation cases as pure `run_cycle`
  tests there, matching the existing table-driven style.
- `.env` is intentionally git-ignored and may hold live provider credentials — never commit it or
  echo its contents. `.upstream-polygraphrag/` is a vendored read-only reference copy (also ignored).
