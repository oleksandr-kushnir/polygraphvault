<p align="center">
  <img alt="PolyGraphVault: drop a file into a folder, get a queryable knowledge graph"
       src="docs/images/hero.png" width="100%">
</p>

<h1 align="center">PolyGraphVault</h1>

<p align="center">
  <strong>Drop a file into a folder. Get a queryable knowledge graph. No pipeline required.</strong>
</p>

<p align="center">
  <img alt="Python 3.11" src="https://img.shields.io/badge/python-3.11-blue">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-async-009688">
  <img alt="Postgres pgvector + Apache AGE" src="https://img.shields.io/badge/Postgres-pgvector%20%2B%20AGE-336791">
  <img alt="Docker Compose" src="https://img.shields.io/badge/Docker-compose-2496ED">
  <img alt="Self-hosted" src="https://img.shields.io/badge/self--hosted-loopback%20%2B%20Caddy%20TLS-5B4FCF">
  <a href="https://github.com/oleksandr-kushnir/polygraphvault/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/oleksandr-kushnir/polygraphvault/actions/workflows/ci.yml/badge.svg"></a>
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-black"></a>
  <a href="https://www.linkedin.com/in/oleksandr-kushnir-ai/"><img alt="LinkedIn" src="https://img.shields.io/badge/LinkedIn-Oleksandr%20Kushnir-0A66C2?logo=linkedin&logoColor=white"></a>
</p>

PolyGraphVault turns a self-hosted [Nextcloud](https://nextcloud.com/files/) into the front door of
a [PolyGraphRAG](https://github.com/oleksandr-kushnir/polygraphrag) knowledge-graph engine. Point it
at a folder, and every file you drop in is continuously reconciled into its own isolated GraphRAG
workspace — vector embeddings **and** an entity/relationship graph in Postgres — with zero pipelines
to babysit.

```text
Nextcloud folder  ->  polygraphvault-sync  ->  PolyGraphRAG workspace/graph
   (put files)         (watches & syncs)          (query over HTTP)
```

Humans manage documents the way they already do — folders, share links, and the official
[Nextcloud desktop and mobile apps](https://nextcloud.com/install/#install-clients) for Windows,
macOS, Linux, Android, and iOS. Machines get a clean HTTP API over the resulting graph. The vault in
between never forgets, never leaks between projects, and never destroys the last good copy of
anything.

---

## 🤖 For the automators: this is your ingestion layer, solved

**Stop building document pipelines.** If you live in n8n, Make, Zapier, or a pile of cron scripts,
PolyGraphVault collapses the entire "parse → OCR → chunk → embed → extract entities → store" stack
into two HTTP calls:

1. **Write** — route any trigger's output into a Nextcloud folder with one WebDAV `PUT`. Email
   attachments, form uploads, scraped reports, meeting recordings — whatever your flow already
   produces.
2. **Query** — hit `POST /workspace/{id}/query` like any other HTTP node once it's ingested.

Everything in between — OCR, transcription, entity extraction, graph building across **22 file
types** (PDFs, Office docs, images, audio) — happens automatically. The whole API is declared in
`/openapi.json`, so a tool-calling agent can learn it from the spec alone.

```
   your trigger ──PUT──►  Nextcloud folder  ──►  graph  ──POST /query──►  structured answer
   (any node)             (one WebDAV call)                              (JSON, no glue code)
```

### Why teams reach for it

| If you're… | You get… |
|---|---|
| 🤖 **An AI automator** | A drop-in ingestion backend. One WebDAV write in, one query call out. No parsing, no embeddings plumbing, no vector-DB ops. |
| 🧠 **Building autonomous agents** | Durable, inspectable memory. Agents write findings as Markdown; minutes later those facts are retrievable entities and relationships. `query/data` returns structured evidence — no LLM prose. And because Nextcloud is the source of truth, a human can open any file the agent "remembers." |
| 🔀 **Running document-heavy workflows** | One deployment, many sealed vaults. Each folder→workspace mapping owns its graph exclusively — per-client, per-case, per-project, with no cross-talk, enforced at creation. |
| 🔐 **Serious about data ownership** | Everything self-hosted: Nextcloud, Postgres (pgvector + Apache AGE), and the syncer behind loopback-only ports, with Caddy TLS as the single hardened public surface. Model providers are pluggable per role — OpenAI-compatible, Ollama, vLLM, OpenRouter — so even inference can stay in-house. |

---

## 🛡️ Built like infrastructure, not a demo

The whole design exists to never destroy the last good copy of your knowledge:

- **Never lose the last good copy.** Changed files are re-ingested *before* the old graph document
  is deleted. A failed replacement leaves the previous version fully queryable.
- **Deletes require proof of health.** A WebDAV canary, minimum-file floors, and a
  max-delete-fraction guard turn "the share went dark" into a paused, audited degradation — not a
  mass delete. Missing files must stay missing through a grace window of *healthy* scans before
  anything is removed.
- **Exclusive ownership, no adoption.** The syncer tracks exactly which graph documents it created
  and never touches rows it doesn't own.
- **Everything is audited.** Every ingest, replacement, delete, failure, and degradation lands in a
  queryable `sync_events` trail.
- **Isolation by construction.** Three separate Postgres databases/roles, internal-only Docker
  networks, constant-time bearer-token auth, and a mapping API that is deliberately never exposed
  publicly (SSH tunnel/VPN only).

---

## 🚀 Try it in five minutes

The working `.env` is intentionally git-ignored (it holds provider and local test credentials).
Clone, then:

```powershell
docker compose pull
docker compose build polygraphvault-sync
docker compose up -d
docker compose ps
```

Then open **`http://127.0.0.1:19630/settings`** — a single page linking to both interactive Swagger
consoles. The syncer's `/docs` is a full CRUD UI for mappings: every route has a "Try it out" button
that fires the real request.

Local endpoints (remapped in `.env` to coexist with other projects):

| Service | URL |
|---|---|
| **Settings entry point** | `http://127.0.0.1:19630/settings` |
| Syncer API / docs | `http://127.0.0.1:19630/docs` |
| PolyGraphRAG API / docs | `http://127.0.0.1:19622/docs` |
| Nextcloud UI | `http://127.0.0.1:18088` |
| Postgres | `127.0.0.1:15432` |

**Auth in one line:** the syncer API requires `Authorization: Bearer <SYNCER_API_TOKEN>` whenever
`SYNCER_API_TOKEN` is set (an empty token disables auth — fine for loopback dev only; the VPS
override refuses to start without one). PolyGraphRAG auth turns on when `POLYGRAPHRAG_API_TOKENS` is
non-empty. A single shared `API_TOKEN` in `.env` guards **both** services when their specific tokens
are left blank — one value locks everything down locally. Keep the two split on the VPS (see
[.env.vps.example](.env.vps.example)); they guard different privilege levels.

Read [the reviewed concept](docs/concept.md) and [security model](docs/security.md) before a VPS
deployment.

---

## 🗂️ Create your first mapping

Create the Nextcloud folder first, then create the mapping. `create_workspace=true` spins up a new,
empty, exclusively owned PolyGraphRAG workspace:

```powershell
$headers = @{ Authorization = "Bearer $env:SYNCER_API_TOKEN" }
$body = @{
  nextcloud_path      = "Knowledge/Customer Alpha"
  workspace_id        = "customer_alpha"
  workspace_name      = "Customer Alpha"
  create_workspace    = $true
  path_root           = "/nextcloud/sync-worker"
  include_extensions  = "md,txt,pdf,docx,pptx,xlsx"
  sync_hidden         = $false
  min_files           = 1
  max_delete_fraction = 0.25
} | ConvertTo-Json

Invoke-RestMethod http://127.0.0.1:19630/mappings `
  -Method Post -Headers $headers -ContentType application/json -Body $body
```

Everyday calls:

```powershell
Invoke-RestMethod http://127.0.0.1:19630/mappings -Headers $headers
Invoke-RestMethod http://127.0.0.1:19630/mappings/1/run -Method Post -Headers $headers
Invoke-RestMethod http://127.0.0.1:19630/mappings/1/disable -Method Post -Headers $headers
Invoke-RestMethod http://127.0.0.1:19630/mappings/1/enable -Method Post -Headers $headers
Invoke-RestMethod http://127.0.0.1:19630/mappings/1/state -Headers $headers
Invoke-RestMethod "http://127.0.0.1:19630/mappings/1/events?limit=50" -Headers $headers
```

Archiving a mapping retains its graph and ownership state; restore brings it back:

```powershell
Invoke-RestMethod http://127.0.0.1:19630/mappings/1 -Method Delete -Headers $headers
Invoke-RestMethod http://127.0.0.1:19630/mappings/1/restore -Method Post -Headers $headers
```

---

## 🧪 Persistent E2E spaces

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

---

## 🌐 VPS deployment

Copy `.env.vps.example` to `.env`, replace every `change-me` value, point both DNS names at the
server, create the `/srv/polygraphvault/*` directories, and deploy:

```bash
docker compose -f docker-compose.yml -f docker-compose.vps.yml pull
docker compose -f docker-compose.yml -f docker-compose.vps.yml build polygraphvault-sync
docker compose -f docker-compose.yml -f docker-compose.vps.yml up -d
```

Caddy exposes only HTTPS Nextcloud and the token-protected PolyGraphRAG route. The mapping API stays
loopback-only; reach it through SSH forwarding or a private VPN. Follow every requirement in
[docs/security.md](docs/security.md), especially the non-admin Nextcloud service account, firewall,
backup, and token guidance.

---

## ✅ Tests

```powershell
docker run --rm -v "${PWD}\syncer:/src" -w /src `
  polygraphvault-sync `
  sh -c "pip install pytest==8.4.1 && python -m pytest -q"
```

The repository also validates local/VPS Compose rendering, Caddy configuration, upstream
PolyGraphRAG's mocked suite, live authentication, database-role isolation, recursive WebDAV
behavior, multiple real media types, graph isolation, queries, replacement, delete grace, and
restart recovery.

---

## 📄 License & third-party components

PolyGraphVault's own source — the **syncer** (`syncer/`) and the deployment configuration in this
repository — is released under the [MIT License](LICENSE).

PolyGraphVault is an *orchestration layer*: it does not bundle or redistribute the source of the
services it composes. Those run as unmodified upstream container images, each governed by its own
license, and are pulled at deploy time — not vendored here:

| Component | Image | License |
|---|---|---|
| PolyGraphRAG (+ Postgres distro) | `ghcr.io/oleksandr-kushnir/polygraphrag*` | MIT |
| Nextcloud | `nextcloud:stable` | AGPL-3.0 |
| PostgreSQL (pgvector + Apache AGE) | upstream | PostgreSQL License / Apache-2.0 |
| Redis | upstream | BSD-3-Clause / RSALv2+SSPL (per version) |
| Caddy | upstream | Apache-2.0 |

Using these images unmodified as containers imposes no source-distribution obligation on this
repository. If you modify and redistribute any of the copyleft-licensed components (notably
Nextcloud, AGPL-3.0), you must comply with that component's own terms.
