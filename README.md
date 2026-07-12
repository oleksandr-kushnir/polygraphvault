# PolyGraphRAG + Nextcloud

Standalone document-to-knowledge-graph system:

```text
Nextcloud folder -> nc-rag-sync -> PolyGraphRAG workspace/graph
```

The syncer walks every mapped Nextcloud folder recursively. Folder-to-workspace mappings live in
Postgres and are created or changed through the protected syncer API—never through environment
variables. PolyGraphRAG and its Postgres distribution use the published images from
`oleksandr-kushnir/polygraphrag`; only the syncer is built locally.

Read [the reviewed concept](docs/concept.md) and [security model](docs/security.md) before a VPS
deployment.

## Local deployment

The working `.env` is intentionally ignored by Git. It contains the retained provider credentials
and local test credentials requested for manual testing.

```powershell
docker compose pull
docker compose build nc-rag-sync
docker compose up -d
docker compose ps
```

Current local endpoints (configured in `.env` to coexist with the other projects):

- Nextcloud UI: `http://127.0.0.1:18088`
- PolyGraphRAG API/docs: `http://127.0.0.1:19622/docs`
- Syncer API/docs: `http://127.0.0.1:19630/docs`
- Postgres: `127.0.0.1:15432`

The syncer API requires `Authorization: Bearer <SYNCER_API_TOKEN>`. PolyGraphRAG auth is enabled
when `POLYGRAPHRAG_API_TOKENS` is non-empty.

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
server, create the `/srv/polygraphrag-nextcloud/*` directories, and deploy:

```bash
docker compose -f docker-compose.yml -f docker-compose.vps.yml pull
docker compose -f docker-compose.yml -f docker-compose.vps.yml build nc-rag-sync
docker compose -f docker-compose.yml -f docker-compose.vps.yml up -d
```

Caddy exposes only HTTPS Nextcloud and the token-protected PolyGraphRAG route. The mapping API
remains loopback-only; reach it through SSH forwarding or a private VPN. Follow every requirement in
[docs/security.md](docs/security.md), especially the non-admin Nextcloud service account, firewall,
backup, and token guidance.

## Tests

```powershell
docker run --rm -v "${PWD}\syncer:/src" -w /src `
  polygraphrag-nextcloud-nc-rag-sync `
  sh -c "pip install pytest==8.4.1 && python -m pytest -q"
```

The repository also validates local/VPS Compose rendering, Caddy configuration, upstream
PolyGraphRAG's mocked suite, live authentication, database-role isolation, recursive WebDAV
behavior, multiple real media types, graph isolation, queries, replacement, delete grace, and
restart recovery.
