# PolyGraphVault

**Drop a file into a folder. Get a queryable knowledge graph.**

PolyGraphVault turns a self-hosted [Nextcloud](https://nextcloud.com/files/) into the front door of
a [PolyGraphRAG](https://github.com/oleksandr-kushnir/polygraphrag) knowledge-graph engine. Every
mapped folder is continuously reconciled into its own isolated GraphRAG workspace — vector
embeddings **and** an entity/relationship graph in Postgres — with zero pipelines to babysit:

```text
Nextcloud folder -> polygraphvault-sync -> PolyGraphRAG workspace/graph
```

Humans manage documents the way they already do — folders, sharing links, and the official
[Nextcloud desktop and mobile apps](https://nextcloud.com/install/#install-clients) for Windows,
macOS, Linux, Android, and iOS. Machines get a clean HTTP API over the resulting graph. The vault in between never
forgets, never leaks between projects, and never destroys the last good copy of anything.

## Why you might love it

**🤖 If you're an AI Automator (n8n, Make, Zapier, scripts)** — stop building ingestion pipelines.
Route any trigger's output (email attachments, form uploads, scraped reports, meeting recordings)
into a Nextcloud folder via one WebDAV call, and PolyGraphVault handles OCR, transcription, entity
extraction, and graph building. Your flow then queries `POST /workspace/{id}/query` like any other
HTTP node. PDFs, Office docs, images, and audio — 22 file types, one folder.

**🧠 If you're building Autonomous AI Agents** — this is durable, inspectable memory. Agents write
findings as Markdown files; minutes later those facts are entities and relationships they can
retrieve with graph-aware queries (`query/data` returns structured evidence, no LLM prose). The
whole API is declared in `/openapi.json`, so a tool-calling agent learns it from the spec alone.
And because Nextcloud is the source of truth, a human can open any file the agent "remembers" and
read it — memory you can audit, version, and roll back.

**🔀 If you run document-heavy workflows or teams** — one deployment, many sealed vaults. Each
folder-to-workspace mapping owns its graph exclusively: per-client, per-case, per-project knowledge
bases with no cross-talk, enforced at creation time. Renames, edits, and deletions in the folder
propagate to the graph — with a configurable grace period and health guards standing between an
accidental folder wipe and your knowledge base.

**🔐 If you care where your data lives** — everything is self-hosted: Nextcloud, Postgres
(pgvector + Apache AGE), and the syncer run on your box behind loopback-only ports, with Caddy TLS
as the single hardened public surface for the VPS profile. Model providers are pluggable per role
(OpenAI or any compatible endpoint — Ollama, vLLM, OpenRouter…), so even inference can stay
in-house.

## Built like infrastructure, not a demo

- **Never lose the last good copy.** Changed files are re-ingested *before* the old graph document
  is deleted; a failed replacement leaves the previous version queryable.
- **Deletes require proof of health.** A WebDAV canary, minimum-file floors, and a
  max-delete-fraction guard turn "the share went dark" into a paused, audited degradation instead
  of a mass delete. Missing files must stay missing through a grace window of *healthy* scans.
- **Exclusive ownership, no adoption.** The syncer tracks exactly which graph documents it created
  and will never touch rows it doesn't own.
- **Everything is audited.** Every ingest, replacement, delete, failure, and degradation lands in a
  queryable `sync_events` trail.
- **Isolation by construction.** Three separate Postgres databases/roles, internal-only Docker
  networks, constant-time bearer-token auth, and a mapping API that is deliberately never exposed
  publicly (SSH tunnel/VPN only).

## Local deployment

The working `.env` is intentionally ignored by Git. It contains the retained provider credentials
and local test credentials requested for manual testing.

```powershell
docker compose pull
docker compose build polygraphvault-sync
docker compose up -d
docker compose ps
```

Current local endpoints (configured in `.env` to coexist with the other projects):

- **Settings entry point: `http://127.0.0.1:19630/settings`** — a single page linking to both
  interactive OpenAPI (Swagger) consoles below. The syncer root `/` redirects here. This page is a
  navigation-only convenience and stays reachable without a token.
- Nextcloud UI: `http://127.0.0.1:18088`
- PolyGraphRAG API/docs: `http://127.0.0.1:19622/docs`
- Syncer API/docs: `http://127.0.0.1:19630/docs`
- Postgres: `127.0.0.1:15432`

The syncer's Swagger console (`/docs`) is a full CRUD UI for mappings: every route has a "Try it
out" button that fires the real request. Reach it from the settings page above.

The syncer API requires `Authorization: Bearer <SYNCER_API_TOKEN>` whenever `SYNCER_API_TOKEN` is
set. An empty token disables syncer auth entirely — acceptable only for loopback local development;
the VPS override refuses to start without one. PolyGraphRAG auth is enabled when
`POLYGRAPHRAG_API_TOKENS` is non-empty. A single shared `API_TOKEN` in `.env` is used by **both**
services whenever their specific `SYNCER_API_TOKEN` / `POLYGRAPHRAG_API_TOKENS` are left blank — one
value guards everything locally. Keep the two split on the VPS (see [.env.vps.example](.env.vps.example)):
they guard different privilege levels. The browser-facing PolyGraphRAG docs link is set with
`POLYGRAPHRAG_DOCS_URL`.

Read [the reviewed concept](docs/concept.md) and [security model](docs/security.md) before a VPS
deployment.

## Runtime mapping example

Create the Nextcloud folder first, then create the mapping. `create_workspace=true` creates a new,
empty, exclusively owned PolyGraphRAG workspace.

```powershell
$headers = @{ Authorization = "Bearer $env:SYNCER_API_TOKEN" }
$body = @{
  nextcloud_path = "Knowledge/Customer Alpha"
  workspace_id = "customer_alpha"
  workspace_name = "Customer Alpha"
  create_workspace = $true
  path_root = "/nextcloud/sync-worker"
  include_extensions = "md,txt,pdf,docx,pptx,xlsx"
  sync_hidden = $false
  min_files = 1
  max_delete_fraction = 0.25
} | ConvertTo-Json

Invoke-RestMethod http://127.0.0.1:19630/mappings `
  -Method Post -Headers $headers -ContentType application/json -Body $body
```

Useful calls:

```powershell
Invoke-RestMethod http://127.0.0.1:19630/mappings -Headers $headers
Invoke-RestMethod http://127.0.0.1:19630/mappings/1/run -Method Post -Headers $headers
Invoke-RestMethod http://127.0.0.1:19630/mappings/1/disable -Method Post -Headers $headers
Invoke-RestMethod http://127.0.0.1:19630/mappings/1/enable -Method Post -Headers $headers
Invoke-RestMethod http://127.0.0.1:19630/mappings/1/state -Headers $headers
Invoke-RestMethod "http://127.0.0.1:19630/mappings/1/events?limit=50" -Headers $headers
```

Archiving a mapping retains its graph and ownership state:

```powershell
Invoke-RestMethod http://127.0.0.1:19630/mappings/1 -Method Delete -Headers $headers
Invoke-RestMethod http://127.0.0.1:19630/mappings/1/restore -Method Post -Headers $headers
```

## Persistent E2E spaces

The preparation script is idempotent and leaves its source folders, mappings, and workspaces in
place for manual use:

```powershell
.\scripts\prepare-persistent-e2e.ps1 `
  -NextcloudPassword $env:NEXTCLOUD_SYNC_PASSWORD `
  -SyncerToken $env:SYNCER_API_TOKEN
```

It creates:

- `Agent Operations Library` (`agent_operations_library`): nested Markdown + PDF + lifecycle file.
- `Visual Security Library` (`visual_security_library`): nested Markdown + real security diagram.
- `Audio Briefing Library` (`audio_briefing_library`): nested Markdown + spoken WAV briefing.

## VPS deployment

Copy `.env.vps.example` to `.env`, replace every `change-me` value, point both DNS names at the
server, create the `/srv/polygraphvault/*` directories, and deploy:

```bash
docker compose -f docker-compose.yml -f docker-compose.vps.yml pull
docker compose -f docker-compose.yml -f docker-compose.vps.yml build polygraphvault-sync
docker compose -f docker-compose.yml -f docker-compose.vps.yml up -d
```

Caddy exposes only HTTPS Nextcloud and the token-protected PolyGraphRAG route. The mapping API
remains loopback-only; reach it through SSH forwarding or a private VPN. Follow every requirement in
[docs/security.md](docs/security.md), especially the non-admin Nextcloud service account, firewall,
backup, and token guidance.

## Tests

```powershell
docker run --rm -v "${PWD}\syncer:/src" -w /src `
  polygraphvault-sync `
  sh -c "pip install pytest==8.4.1 && python -m pytest -q"
```

The repository also validates local/VPS Compose rendering, Caddy configuration, upstream
PolyGraphRAG's mocked suite, live authentication, database-role isolation, recursive WebDAV
behavior, multiple real media types, graph isolation, queries, replacement, delete grace, and
restart recovery.
