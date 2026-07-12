# Security model and VPS hardening

This deployment starts from PolyGraphRAG's own security boundary—loopback-only backend ports,
optional API bearer tokens, and TLS whenever an API leaves the host—and tightens it for the combined
Nextcloud use case. The public surface is exactly:

- `https://cloud.example.com`: Nextcloud's authenticated UI/WebDAV.
- `https://rag.example.com`: PolyGraphRAG, with a required bearer token.

The mapping API stays on `127.0.0.1:9630`; administer it through SSH port forwarding or a private
VPN. Postgres, Redis, and the internal Nextcloud-to-syncer WebDAV path are never public.

## Required production controls

1. Point both DNS names at the VPS, then deploy with `docker-compose.vps.yml`. Caddy obtains and
   renews certificates and redirects HTTP to HTTPS.
2. Open inbound TCP 22 (preferably restricted), 80, and 443 plus UDP 443. Deny every other inbound
   port. The Compose backend ports bind only to `127.0.0.1` as a second boundary.
3. Generate different random values of at least 32 bytes for `POLYGRAPHRAG_API_TOKENS` and
   `SYNCER_API_TOKEN`. Never put tokens in URLs, logs, shell history, or committed files.
4. Store `.env` as mode `0600`, owned by the deployment account. Use SSH keys, disable SSH password
   login and direct root login, and keep the account outside unnecessary privileged groups.
5. Do not run the syncer as the Nextcloud admin in production. Create a `sync-worker` user, grant it
   only the shared/group folders that should be indexed, issue a Nextcloud app password, and put
   that app password in `NEXTCLOUD_SYNC_PASSWORD`. Enable 2FA for interactive admin users.
6. Keep `min_files >= 1` for valuable, normally non-empty mappings and retain the default delete
   grace. Use `min_files=0` only for a folder that is intentionally allowed to become empty.
7. Use the three independent database passwords from `.env`. Nextcloud, PolyGraphRAG, and the
   syncer own separate roles/databases; never collapse them back to the Postgres bootstrap role.

## Endpoint protections

- Caddy supplies TLS, HSTS, content-sniffing/frame/referrer restrictions, structured access logs,
  and a public request-body ceiling. It does not log Authorization headers by default.
- PolyGraphRAG rejects every endpoint except `/health` without a configured bearer token.
- The public Caddy route additionally rejects every `DELETE` plus workspace creation, direct upload,
  and workspace restore. The syncer uses PolyGraphRAG's private service network for administration.
- The syncer independently authenticates its mapping API and validates workspace slugs and
  Nextcloud paths; `..`, absolute paths, and malformed graph names are rejected.
- Mapping deletion never purges a graph. File-level graph deletion requires a healthy recursive
  WebDAV scan, a canary, a configurable minimum file count, and a grace period.
- The syncer's HTTP clients do not log request headers, credentials, or document bodies.
- Database, WebDAV, RAG, control, and proxy traffic use separate Compose networks. Nextcloud and
  PolyGraphRAG do not share a database or proxy network; Caddy is the only container joined to both
  public-facing proxy networks.

## Service account and shared folders

The safest operational layout is one non-admin `sync-worker` account plus Nextcloud Group Folders:

1. Enable the Group Folders app from the authenticated Nextcloud administration UI.
2. Create a group such as `rag-source` and add `sync-worker` plus only the human users who need the
   indexed folder.
3. Create each source folder as a Group Folder and grant `sync-worker` read access. The syncer never
   needs to write user documents; its only write is the hidden health canary in the mapped root, so
   grant the minimum write permission needed for that marker.
4. Map the folder name shown in the `sync-worker` WebDAV root to its PolyGraphRAG workspace through
   the mapping API.

If policy requires a strictly read-only service account, pre-create `.nc-rag-sync-health` in each
mapped root as an administrator; the worker can then operate without creating it.

## Supply chain and updates

Local development follows the repository's published update tags. The VPS example pins the exact
reviewed digests recorded in `.env.vps.example`, including one identical Nextcloud digest for the
web and cron containers. On each production release, review upstream changes, update the digests,
and redeploy during a maintenance window. Continue tracking security updates for
Nextcloud, Redis, Caddy, PolyGraphRAG, RAG-Anything, LightRAG, LibreOffice, and the base images.

## Backups and incident recovery

Back up the Nextcloud data directory and `nextcloud` database consistently, and separately back up
the PolyGraphRAG database/data plus the `ncragsync` database. Encrypt backups, store at least one
copy off-host, and test restores. Nextcloud trash/versioning and the sync delete grace help with
mistakes but are not backups. During an incident, disable mappings first, preserve logs and database
state, rotate API/app passwords, then restore or re-index from the authoritative Nextcloud corpus.

## Mapping API through SSH

From an administrator workstation:

```bash
ssh -L 9630:127.0.0.1:9630 deploy@vps.example.com
curl -H "Authorization: Bearer $SYNCER_API_TOKEN" http://127.0.0.1:9630/mappings
```

This still provides runtime URL-based mapping management without placing the control plane on the
public internet.
