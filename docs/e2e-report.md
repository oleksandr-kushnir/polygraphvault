# Persistent E2E verification report

Date: 2026-07-12 (Europe/Berlin). Updated after applying the final segmented Compose networks and
confirming the pending deletion grace completed.

The fixtures below are intentionally retained in the running Nextcloud and PolyGraphRAG volumes for
manual exploration. Re-run `scripts/prepare-persistent-e2e.ps1` to recreate missing source fixtures
or reuse existing mappings without producing duplicate workspaces.

## Persistent spaces

| Nextcloud folder | Workspace | Final substantive files | Media |
|---|---|---:|---|
| `PolyGraphRAG E2E/Agent Operations Library` | `agent_operations_library` | 2 | Markdown, PDF |
| `PolyGraphRAG E2E/Visual Security Library` | `visual_security_library` | 2 | Markdown, JPEG |
| `PolyGraphRAG E2E/Audio Briefing Library` | `audio_briefing_library` | 2 | Markdown, spoken WAV |

Nested paths were preserved in every PolyGraphRAG `source_path` and citation. No workspace contained
a path owned by another mapping.

## Results

- Syncer unit/client suite: **16 passed**, including real-shape multi-`propstat` Nextcloud WebDAV
  recursion, encoded nested paths, API metadata, replacement failure retention, and ownership
  conflict behavior.
- Upstream PolyGraphRAG suite at inspected commit `669fd36be705020160c78d3870cecfce00214bca`:
  **236 passed**.
- Upstream in-process smoke test: **36/36 passed**.
- Local and VPS Compose rendering: passed.
- Caddy VPS configuration: valid; TLS automation and security routes adapted successfully.
- All six containers healthy; Nextcloud reports installed version `33.0.6` with no DB upgrade or
  maintenance state.
- Database ownership isolated: `ragdb -> raguser`, `nextcloud -> nextcloud`,
  `ncragsync -> ncragsync`.
- Syncer API authentication: unauthenticated `/mappings` returned **401**; authenticated access and
  database health returned **200/ok**.
- Browser acceptance: Nextcloud login, dashboard, Files application, and all three persistent
  folders rendered; PolyGraphRAG Swagger UI rendered the complete API contract.
- Real ingestion: 3/3 Agent Operations, 2/2 Visual Security, and 2/2 Audio Briefing jobs reached
  `done`; durable sync ownership rows all reached `synced`.
- Workspace exclusivity: a second mapping targeting `agent_operations_library` returned **409**.
- Graph isolation: every workspace reported zero foreign paths.
- Real queries passed with citations to the expected Nextcloud paths:
  - Agent Operations combined Hermes runtime practices with the inference-time feedback paper and
    cited both the nested Markdown file and PDF.
  - Visual Security identified filesystem isolation, network policy, privacy routing, and audit
    trail from the real JPEG and cited the image plus its context note.
  - Audio Briefing recovered recursive mapping and the canary/bulk-delete/grace controls from the
    spoken WAV and cited both audio and transcript.
- Upload-first replacement: `lifecycle/update-check.md` changed from
  `doc-5565ffb9391c55f3653908bae258fa13` to
  `doc-59c5fb9033b5f38c1e0059c9940c7372`; exactly one current index row remained and
  `superseded_doc_id` was cleared only after the new job reached `done`.
- Canary loss: deleting `.nc-rag-sync-health` produced a recorded `health_degraded` cycle,
  recreated the marker, and retained all three graph documents.
- Mapping archive/restore: the Visual Security mapping disappeared from active listings while
  archived, restored disabled, re-enabled successfully, and retained both ownership rows and graph
  documents.
- Restart recovery: the syncer container was rebuilt/recreated while a deletion grace timer was
  pending; the original `pending_delete_since` survived unchanged.
- Deletion grace completion: after the required healthy five-minute (`SYNC_DELETE_GRACE_SECS=300`)
  window elapsed, the syncer deleted `lifecycle/update-check.md`
  (`doc-59c5fb9033b5f38c1e0059c9940c7372`) — recorded as a `deleted` event at 13:43:21 UTC. The
  `agent_operations_library` workspace file index now returns exactly two documents
  (`guides/runtime-and-operations.md`, `research/inference-time-feedback.pdf`), no `sync_state` row
  carries a pending delete, and the mapping settled at two owned documents.

## Segmented-network application

The final Compose network segmentation from `docker-compose.yml` was applied to the running local
stack (`docker compose up -d --remove-orphans`); the previous flat `_db` and `_proxy` networks were
removed. Both local and VPS (`--env-file .env.vps.example`) Compose renderings validated. The
recreated topology was verified directly from the running containers:

- `postgres` → `db-rag`, `db-nextcloud`, `db-sync`, `postgres-control`
- `polygraphrag` → `db-rag`, `rag`, `rag-proxy`
- `nextcloud` → `db-nextcloud`, `nextcloud`, `nextcloud-proxy`
- `nc-rag-sync` → `db-sync`, `nextcloud`, `rag`, `syncer-control`
- `redis` → `nextcloud`

Nextcloud and PolyGraphRAG share no database network (`db-nextcloud` vs `db-rag`) and no proxy
network (`nextcloud-proxy` vs `rag-proxy`); Postgres reaches each consumer over a dedicated `db-*`
network. Post-redeploy re-verification: all six containers healthy; syncer unit/client suite **16
passed**; database ownership still isolated (`ragdb -> raguser`, `nextcloud -> nextcloud`,
`ncragsync -> ncragsync`); syncer API returned **401** unauthenticated and **200** with the bearer
token; and a triggered reconciliation cycle for `agent_operations_library` scanned two files with
zero ingests, deletes, errors, and no degraded health — confirming the syncer still reaches Nextcloud
WebDAV and PolyGraphRAG across the segmented networks.

## Fresh multimodal isolation E2E (post-segmentation)

After the segmented networks were applied, three brand-new, exclusively owned workspaces were created
end-to-end through the mapping API to exercise fresh ingestion of additional supported media types and
to prove graph isolation. Each graph's signature fact was embedded **only** in its distinctive media
file, so a correct, cited answer confirms that media type was genuinely ingested and queried.

| Folder (`PolyGraphRAG E2E Fresh/…`) | Workspace | Media types | Files |
|---|---|---|---:|
| `Text Ledger` | `e2e_fresh_text_ledger` | `txt`, `csv`, `html` | 3 |
| `Image Atlas` | `e2e_fresh_image_atlas` | `png` (vision) | 1 |
| `Audio Desk` | `e2e_fresh_audio_desk` | `wav` (whisper) | 1 |

- Ingestion: all five files reached `done`, including the PNG via the vision model and the spoken WAV
  via Whisper. Every `source_path` preserved its nested folder path.
- Signature queries returned correct answers, each citing only its own source file(s):
  - Text Ledger: Project Falcon is led by Dana Ruiz, based in Lisbon, budget 42,000 EUR — cited
    `team/leadership.txt` and `finance/budget.csv`.
  - Image Atlas: the Zephyr Gateway routes inbound traffic through region EU-West-3 secured with mTLS
    — cited `diagrams/zephyr-gateway.png` (text recovered from the rendered image).
  - Audio Desk: the Meridian backup policy keeps three encrypted copies, one off site, with weekly
    restore tests — cited `briefings/meridian-backup.wav` (recovered from the spoken audio).
- Cross-graph isolation: querying each workspace for the other workspaces' signature facts returned
  no answer and leaked none of the forbidden tokens (Zephyr/EU-West-3/mTLS, Falcon/Dana Ruiz/Lisbon/
  42,000, Meridian/encrypted copies). Index isolation also held: each workspace's file index contained
  only paths under its own mapped folder, with zero foreign paths.

These three fresh workspaces, their Nextcloud source folders, and their mappings are intentionally
left running for manual testing.

## Synchronization lifecycle E2E (new / changed / deleted file)

A dedicated, exclusively owned workspace `e2e_sync_lifecycle` (Nextcloud folder
`PolyGraphRAG Sync Lifecycle/Vault Log`, mapping id 7, `md` only) was used to exercise each
reconciliation transition end to end and confirm the PolyGraphRAG graph reflects the corresponding
change. Each fact lives in exactly one file, so a correct/incorrect answer proves the graph state.

Baseline: `baseline.md` ("Orion vault … building C7") and `notes.md` ("night shift lead is Priya
Vance") both reached `done`; the query "Who is the night shift lead?" answered **Priya Vance**, cited
`notes.md`.

- **New file.** Added `roster.md` ("backup coordinator is Tomas Blackwood") and triggered a cycle.
  The graph index grew to three `done` docs; the two untouched files kept their original `doc_id`s
  (no needless re-ingest). Event `ingested roster.md`. Query "Who is the backup coordinator?" →
  **Tomas Blackwood**, cited `roster.md`. New file reflected. ✔
- **Changed file.** Overwrote `notes.md` (lead → **Kenji Sato**) and triggered a cycle. Upload-first
  replacement: `notes.md` moved from `doc-8b9e214f…` to `doc-144d1607…`, still exactly one index row,
  old `doc_id` absent, `baseline`/`roster` unchanged. Event `reingested notes.md`. Query now answered
  **Kenji Sato** (cited `notes.md`), and a negative probe for the old name returned "no information
  available about Priya Vance" — the superseded content and its entities were purged with the old
  `doc_id`. Changed file reflected. ✔
- **Deleted file.** Removed `roster.md` from Nextcloud (14:13:36 UTC). The first healthy cycle marked
  `sync_state.pending_delete_since=14:13:36` while **retaining** the graph doc (index still three
  files) — `baseline`/`notes` carried no pending clock, proving the delete grace guard. Healthy
  cycles were then triggered across the `SYNC_DELETE_GRACE_SECS=300` window; the syncer deleted the
  doc once the grace elapsed (event `deleted roster.md` at 14:18:56 UTC, ≈5m20s after the file
  vanished). The file index returned to the two surviving docs and the `roster.md` `sync_state` row
  was removed. The deleted fact left the graph shortly after (eventual entity/relation cleanup within
  tens of seconds of the file-index delete): "Who is the backup coordinator?" → "no information
  available", citing only `baseline.md` and `notes.md`. Deleted file reflected. ✔

Full audit trail (`sync_events`, mapping 7): `ingested baseline.md` → `ingested notes.md` →
`ingested roster.md` → `reingested notes.md` → `deleted roster.md`. This workspace, its folder, and
mapping 7 are left running (final state: `baseline.md`, `notes.md`) for manual inspection.

## Image provenance used

- PolyGraphRAG: `sha256:d08c2b699e6c1e687d5b95c587b0cf76c6beebdb6a90dfdc19cb53e24c93dfa0`
- PolyGraphRAG Postgres: `sha256:3b6608624c5a8bd905063984ff5fbccfacfab7c6dafe0da3f269273525d60202`
- Nextcloud: `sha256:35170a1c67e759ef874eaa0fde74eb223cf221dacfb67a8ccc14e8d7c5f2c990`
- Redis: `sha256:6ab0b6e7381779332f97b8ca76193e45b0756f38d4c0dcda72dbb3c32061ab99`
- Caddy: `sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648`

## Deliberately retained credentials and data

The copied provider credentials remain only in ignored `.env`, as requested. Local Nextcloud and
syncer test credentials are also present there for manual testing. No secret value is included in
tracked documentation. Docker volumes and all three persistent libraries/workspaces remain running.
