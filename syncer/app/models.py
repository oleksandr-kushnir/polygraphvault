from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

DEFAULT_EXTENSIONS = "md,txt,csv,html,pdf,docx,pptx,xlsx"
SUPPORTED_EXTENSIONS = frozenset(
    "md txt csv html pdf docx pptx xlsx jpg jpeg png gif bmp tiff webp "
    "mp3 wav m4a ogg flac opus webm".split()
)


def validate_extensions(value: str) -> str:
    normalized = [part.strip().lower().lstrip(".") for part in (value or "").split(",") if part.strip()]
    unknown = sorted(set(normalized) - SUPPORTED_EXTENSIONS)
    if unknown:
        raise ValueError(f"unsupported extensions: {', '.join(unknown)}")
    return ",".join(dict.fromkeys(normalized))


def clean_path(value: str) -> str:
    value = value.replace("\\", "/").strip().strip("/")
    if not value or value == ".":
        raise ValueError("path must name a folder below the Nextcloud user root")
    if any(part in ("", ".", "..") for part in value.split("/")):
        raise ValueError("path contains an invalid segment")
    return value


class MappingCreate(BaseModel):
    nextcloud_path: str
    workspace_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,47}$")
    workspace_name: str | None = None
    create_workspace: bool = False
    path_root: str = "/nextcloud"
    enabled: bool = True
    include_extensions: str = DEFAULT_EXTENSIONS
    sync_hidden: bool = False
    excludes: str = ""
    min_files: int = Field(default=1, ge=0)
    max_delete_fraction: float = Field(default=0.25, ge=0, le=1)

    @field_validator("nextcloud_path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return clean_path(value)

    @field_validator("include_extensions")
    @classmethod
    def validate_include_extensions(cls, value: str) -> str:
        return validate_extensions(value)


class MappingPatch(BaseModel):
    nextcloud_path: str | None = None
    path_root: str | None = None
    enabled: bool | None = None
    include_extensions: str | None = None
    sync_hidden: bool | None = None
    excludes: str | None = None
    min_files: int | None = Field(default=None, ge=0)
    max_delete_fraction: float | None = Field(default=None, ge=0, le=1)

    @field_validator("nextcloud_path")
    @classmethod
    def validate_path(cls, value: str | None) -> str | None:
        return clean_path(value) if value is not None else value

    @field_validator("include_extensions")
    @classmethod
    def validate_include_extensions(cls, value: str | None) -> str | None:
        return validate_extensions(value) if value is not None else value


class MappingView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    workspace_id: str
    path_root: str
    nextcloud_path: str
    enabled: bool
    include_extensions: str
    sync_hidden: bool
    excludes: str
    min_files: int
    max_delete_fraction: float
    archived_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class RunAccepted(BaseModel):
    status: str = "scheduled"
    mapping_id: int


class FileStateView(BaseModel):
    rel_path: str
    sync_status: str
    doc_id: str | None = None
    content_hash: str | None = None
    remote_etag: str | None = None
    retry_count: int = 0
    last_error: str | None = None
    pending_delete_since: datetime | None = None
    updated_at: datetime


class EventView(BaseModel):
    id: int
    ts: datetime
    event_type: str
    rel_path: str | None = None
    detail: dict | None = None
