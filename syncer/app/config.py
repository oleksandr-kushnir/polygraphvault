from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import quote


@dataclass(frozen=True)
class Config:
    postgres_dsn: str
    nextcloud_url: str
    nextcloud_user: str
    nextcloud_password: str
    polygraphrag_url: str
    polygraphrag_api_token: str
    syncer_api_token: str
    poll_interval: float
    delete_grace_secs: float
    max_file_bytes: int
    ingest_timeout: float
    canary_autocreate: bool

    @classmethod
    def from_env(cls) -> "Config":
        explicit_dsn = os.environ.get("POSTGRES_DSN", "").strip()
        if explicit_dsn:
            postgres_dsn = explicit_dsn
        else:
            user = quote(os.environ.get("POSTGRES_USER", "raguser"), safe="")
            password = quote(os.environ.get("POSTGRES_PASSWORD", ""), safe="")
            host = os.environ.get("POSTGRES_HOST", "postgres")
            port = os.environ.get("POSTGRES_PORT", "5432")
            database = quote(os.environ.get("POSTGRES_DB", "ncragsync"), safe="")
            postgres_dsn = f"postgresql://{user}:{password}@{host}:{port}/{database}"
        return cls(
            postgres_dsn=postgres_dsn,
            nextcloud_url=os.environ.get("NEXTCLOUD_URL", "http://nextcloud").rstrip("/"),
            nextcloud_user=os.environ.get("NEXTCLOUD_USER", "admin"),
            nextcloud_password=os.environ.get("NEXTCLOUD_PASSWORD", ""),
            polygraphrag_url=os.environ.get(
                "POLYGRAPHRAG_URL", "http://polygraphrag:9622"
            ).rstrip("/"),
            polygraphrag_api_token=os.environ.get("POLYGRAPHRAG_API_TOKEN", "")
            .split(",")[0]
            .strip(),
            syncer_api_token=os.environ.get("SYNCER_API_TOKEN", "").strip(),
            poll_interval=float(os.environ.get("SYNC_POLL_INTERVAL", "30")),
            delete_grace_secs=float(os.environ.get("SYNC_DELETE_GRACE_SECS", "300")),
            max_file_bytes=int(float(os.environ.get("SYNC_MAX_FILE_MB", "200")) * 1_000_000),
            ingest_timeout=float(os.environ.get("SYNC_INGEST_TIMEOUT", "900")),
            canary_autocreate=os.environ.get("SYNC_CANARY_AUTOCREATE", "true").lower()
            in {"1", "true", "yes", "on"},
        )
