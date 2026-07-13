# PolyGraphVault: standalone system concept (Nextcloud to PolyGraphRAG)

## 1. Goal and scope

This repository deploys a fresh, standalone document-to-knowledge-graph system:

```text
Nextcloud folder -> polygraphvault-sync -> PolyGraphRAG workspace/graph
```

There is no agent workspace and no host-folder bridge. Nextcloud is the source of truth for
document bytes; PolyGraphRAG is a derived, rebuildable projection. The synchronizer combines the
relevant responsibilities of the former `nc-sync` and `rag-sync` workers:

- WebDAV traversal, etag handling, partition tolerance, and health checks from `nc-sync`.
- file lifecycle reconciliation, ingestion job polling, graph deletes, scope filters, delete grace,
  and audit state from `rag-sync`.

The PolyGraphRAG and PolyGraphRAG Postgres images are pulled from
`ghcr.io/oleksandr-kushnir`. The syncer is the only custom application image in this repository.

## 2. Services

| Service | Responsibility | Exposed port |
|---|---|---|
| `postgres` | isolated PolyGraphRAG, Nextcloud, and syncer databases/roles | loopback only |
| `polygraphrag` | multimodal ingestion, isolated workspaces/graphs, query API | loopback only |
| `nextcloud` | source document UI and WebDAV server | loopback only |
| `nextcloud-cron` | Nextcloud background jobs | none |
| `redis` | Nextcloud cache and transactional file locking | none |
| `polygraphvault-sync` | mapping API and background reconciliation | loopback only |

Persistent application data uses Docker named volumes in the local deployment. A container rebuild
or recreation does not delete it. The deployment is fresh by design and does not import volumes or
database rows from `multi-agents`.

The services do not share database credentials. PolyGraphRAG, Nextcloud, and the syncer each own a
separate database/role, and Compose networks restrict which services can reach Postgres, Redis,
Nextcloud, PolyGraphRAG, and the public proxy.

## 3. Runtime mappings

Mappings are rows in Postgres and are managed through the syncer's HTTP API. No folder-to-graph
mapping is read from environment variables.

A mapping contains:

- `nextcloud_path`: folder relative to the configured WebDAV user's file root.
- `workspace_id`: target PolyGraphRAG workspace/graph.
- `path_root`: display/citation prefix recorded in PolyGraphRAG.
- `enabled`: hot-reloaded switch for this mapping.
- `include_extensions`: validated allowlist. Empty means exactly all PolyGraphRAG-supported types,
  never arbitrary/unknown types and never no types.
- `sync_hidden`: whether dot-files and dot-folders are included.
- `excludes`: mapping-specific glob/directory rules.
- `min_files`: lower-bound health guard once the mapping has indexed state.
- `max_delete_fraction`: maximum fraction of owned files allowed to disappear in one cycle before
  the scan is treated as degraded.

API contract:

- `GET /health`
- `GET /mappings`
- `POST /mappings`
- `GET /mappings/{id}`
- `GET /mappings/{id}/state` (per-file sync status, doc ids, errors, pending deletes)
- `GET /mappings/{id}/events?limit=N` (recent audit events, newest first; readable for archived
  mappings)
- `PATCH /mappings/{id}`
- `DELETE /mappings/{id}` (archives the mapping; ownership state is retained and graph data is not
  purged)
- `POST /mappings/{id}/restore`
- `POST /mappings/{id}/enable`
- `POST /mappings/{id}/disable`
- `POST /mappings/{id}/run` (request an immediate cycle)

Version 1 enforces one mapping per PolyGraphRAG workspace. Creating a mapping verifies that the
target workspace exists and is empty; with `create_workspace=true`, the syncer creates it. This
prevents two mappings—or a mapping and manual uploads—from claiming/deleting the same graph rows.
Workspace exclusivity is the enforced overlap rule; the same Nextcloud folder may deliberately feed
multiple workspaces, each with its own independent copies.
After a mapping owns a workspace, documents must enter it through that mapping.

Scope and enable changes take effect without recreating a container. Ownership/citation fields
(`nextcloud_path`, `workspace_id`, and `path_root`) are immutable after the first state row; changing
them requires a new mapping/workspace or an explicit future migration workflow.

## 4. Reconciliation model

For every enabled mapping, the worker recursively lists the Nextcloud folder through WebDAV and
joins the result with:

1. its durable sync state (`etag`, SHA-256, PolyGraphRAG `doc_id`, status), and
2. PolyGraphRAG's authoritative `GET /workspace/{id}/files` index.

The resulting actions are:

Every descendant folder is traversed. A mapping for `Clients/Acme` therefore ingests both
`Clients/Acme/handbook.pdf` and `Clients/Acme/legal/contracts/msa.docx` into the same workspace,
with `handbook.pdf` and `legal/contracts/msa.docx` retained as distinct relative paths.

| Nextcloud state | Graph/state | Action |
|---|---|---|
| new file | absent | download bytes and ingest |
| same etag/hash | synced | no-op |
| changed bytes | old document exists | ingest replacement; only after `done`, delete old document |
| file absent | old document exists | delete after healthy grace period |
| prior ingest failed | file present | retry |

Uploads supply `source_path=<relative file path>` and `path_root=<mapping path_root>`. The syncer
polls the returned job until `done` or a terminal failure and only then marks the file synced.
Deletes use the exact `doc_id` and therefore do not depend on filenames.

A file whose ingest failed and whose source etag is unchanged is retried with exponential backoff
(60 s doubling per recorded retry, capped at one hour) so a permanently failing document cannot
burn provider cost every poll cycle. A source change (new etag) retries immediately.

Replacement is deliberately upload-first. If replacement ingestion fails, the old graph document
remains queryable and its ownership metadata is retained while the new source etag/error is marked
for retry. Once the replacement is durable, the old `doc_id` becomes a tracked cleanup item and is
deleted. A crash or deletion failure may temporarily leave both versions, but never removes the
last good version.

The syncer's `sync_state` is the authority for which `doc_id` values it owns. Prefix matching against
PolyGraphRAG's file index is used only to recover a previously recorded pending ingest; an unowned
row is never adopted or deleted automatically. Restoring the sync database is therefore part of
disaster recovery, not optional cache reconstruction.

Only one cycle for a given mapping may run at a time. A Postgres advisory lock enforces this across
processes/restarts; destructive actions re-check the mapping's enabled/version state. Different
mappings are processed serially in the initial implementation because PolyGraphRAG is explicitly a
single-worker, low-usage service.

## 5. Destructive-operation safety

Graph deletion is the highest-risk operation and is guarded by all of the following:

- A failed WebDAV request makes the cycle unhealthy and authorizes no deletes.
- Once a mapping has indexed files, a scan below `min_files` is unhealthy.
- A pre-provisioned WebDAV canary file in each production mapped folder is one health signal for a
  wrong-root listing; it is not proof that every descendant was visible. Automatic canary creation
  is a local-development option and is disabled on the VPS so the production service account can be
  read-only.
- Any descendant `401`, `403`, timeout, or `5xx` aborts the recursive scan and authorizes no delete.
- A scan that exceeds `max_delete_fraction` is degraded even if the canary is present.
- A missing source file must remain missing through `SYNC_DELETE_GRACE_SECS` of healthy scans.
- Any unhealthy cycle clears pending-delete clocks.
- Scope changes are explicit mapping changes. Files excluded by the new scope are treated as
  intentionally removed and follow the same delete grace.
- Mapping archival never deletes the target workspace, its graph, or the ownership rows required
  for later recovery/cleanup.
- A file that grows beyond the size cap is skipped and its existing graph document is retained; it
  is never interpreted as a source deletion.

The worker records ingestion, re-ingestion, deletion, degraded health, and failures in an audit
table and emits structured logs.

## 6. Identity, renames, and duplicates

The stable source identity is `(mapping_id, relative_path)`. A rename is therefore a new path plus a
grace-delayed deletion of the old path. Content hashes avoid unnecessary re-ingestion when only a
Nextcloud etag changes, but they do not merge two distinct paths with identical bytes: both paths
remain independently addressable and citable.

## 7. Configuration and security boundary

Environment variables are reserved for deployment-wide concerns: database credentials, WebDAV
service-account credentials, service URLs, API tokens, polling interval, file-size limit, timeouts,
and delete grace. Secrets are stored only in an uncommitted `.env`; `.env.example` contains no live
credentials.

All backend host ports bind to `127.0.0.1`. The syncer sends a bearer token to PolyGraphRAG and its
own API independently requires a bearer token. External exposure is provided by the VPS override.
That profile publishes only Caddy on ports 80/443, configures Nextcloud's proxy/HTTPS settings, adds
security headers, and leaves every backend port bound to loopback or private Compose networks. The
mapping API is deliberately not reverse-proxied: it remains reachable through the VPS loopback
interface (normally over an SSH tunnel or private VPN), reducing the public attack surface.

The public PolyGraphRAG route is token-protected and blocks workspace creation/purge, direct upload,
restore, and file-delete administration; those remain on the VPS loopback/internal network. Public
query access can still incur model cost, so production should add upstream/CDN rate and spend
alerts. VPS manifests use reviewed published-image digests; `latest` is only the local update path.

## 8. Testing contract

The implementation is accepted only after these layers pass:

1. Pure decision tests for new, unchanged, changed, missing, failed, and scope-filtered files.
2. WebDAV parser/client tests, including encoded paths and canary handling.
3. PolyGraphRAG client tests for workspace creation, upload metadata, job polling, and delete.
4. API/database tests for runtime mapping CRUD and validation.
5. Compose rendering and container health checks.
6. Security-route checks: unauthorized public API calls fail, destructive PolyGraphRAG routes are
   unavailable through Caddy, and backend ports are not internet-facing.
7. Persistent real-data E2E spaces with deterministic names, created through the mapping API and
   left in place for manual use. They cover multiple isolated graphs, recursive nested folders,
   text/Office/PDF, image, and audio inputs supported by PolyGraphRAG; verify ingestion status,
   paths/hashes, isolation, real queries/citations, replacement, rename, grace-delayed deletion,
   failed replacement retaining the old graph, canary/permission failure, overlap rejection, and
   restart recovery.

Tests that require real model inference are separated from infrastructure smoke tests so the stack
can validate routing, persistence, and API contracts cheaply. The acceptance gate nevertheless
includes real inference/query paths using the configured DeepSeek/OpenAI-compatible providers. A
checked-in manifest/report records every persistent test folder, mapping, workspace, and sample so
reruns adopt deliberately instead of creating duplicates.
