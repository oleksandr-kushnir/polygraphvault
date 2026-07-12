from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from app.config import Config
from app.db import Repository
from app.models import (
    EventView, FileStateView, MappingCreate, MappingPatch, MappingView, RunAccepted,
)
from app.polygraph import PolyGraphClient
from app.sync import Scheduler
from app.webdav import WebDavClient


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)


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

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        repo.init()
        scheduler.start()
        try:
            yield
        finally:
            scheduler.stop()
            webdav.close()
            graph.close()

    application = FastAPI(
        title="Nextcloud to PolyGraphRAG Syncer",
        version="1.0.0",
        lifespan=lifespan,
    )
    application.state.config = cfg
    application.state.repo = repo
    application.state.webdav = webdav
    application.state.graph = graph
    application.state.scheduler = scheduler

    @application.middleware("http")
    async def require_token(request: Request, call_next):
        if not cfg.syncer_api_token or request.url.path == "/health":
            return await call_next(request)
        supplied = request.headers.get("authorization", "")
        expected = f"Bearer {cfg.syncer_api_token}"
        if not secrets.compare_digest(supplied, expected):
            return JSONResponse(
                status_code=401,
                content={"detail": "invalid or missing bearer token"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)

    def get_mapping(mapping_id: int) -> dict:
        mapping = repo.get_mapping(mapping_id)
        if mapping is None:
            raise HTTPException(404, "mapping not found")
        return mapping

    @application.get("/health")
    def health(response: Response):
        try:
            repo.list_mappings()
            database = "ok"
        except Exception:
            database = "error"
        if database != "ok":
            response.status_code = 503
        return {"status": "ok" if database == "ok" else "degraded", "database": database}

    @application.get("/mappings", response_model=list[MappingView])
    def list_mappings():
        return repo.list_mappings()

    @application.get("/mappings/{mapping_id}", response_model=MappingView)
    def read_mapping(mapping_id: int):
        return get_mapping(mapping_id)

    @application.get("/mappings/{mapping_id}/state", response_model=list[FileStateView])
    def read_mapping_state(mapping_id: int):
        get_mapping(mapping_id)
        return repo.list_state(mapping_id)

    @application.get("/mappings/{mapping_id}/events", response_model=list[EventView])
    def read_mapping_events(mapping_id: int, limit: int = 100):
        if repo.get_mapping(mapping_id, include_archived=True) is None:
            raise HTTPException(404, "mapping not found")
        return repo.list_events(mapping_id, max(1, min(limit, 1000)))

    @application.post("/mappings", response_model=MappingView, status_code=201)
    def create_mapping(body: MappingCreate):
        try:
            webdav.validate_folder(body.nextcloud_path)
        except FileNotFoundError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(502, f"Nextcloud validation failed: {exc}") from exc
        try:
            graph.ensure_workspace(
                body.workspace_id, body.workspace_name, body.create_workspace
            )
        except KeyError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(502, f"PolyGraphRAG validation failed: {exc}") from exc
        try:
            if graph.list_files(body.workspace_id):
                raise HTTPException(
                    409,
                    "target workspace is not empty; mappings require an empty, exclusively owned workspace",
                )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(502, f"PolyGraphRAG file-index validation failed: {exc}") from exc
        values = body.model_dump(exclude={"workspace_name", "create_workspace"})
        try:
            mapping = repo.create_mapping(values)
        except psycopg.errors.UniqueViolation as exc:
            raise HTTPException(409, "this folder-to-workspace mapping already exists") from exc
        if mapping["enabled"]:
            scheduler.request(mapping["id"])
        return mapping

    @application.patch("/mappings/{mapping_id}", response_model=MappingView)
    def update_mapping(mapping_id: int, body: MappingPatch):
        current = get_mapping(mapping_id)
        values = body.model_dump(exclude_unset=True, exclude_none=True)
        new_path = values.get("nextcloud_path")
        new_root = values.get("path_root")
        ownership_change = (
            (new_path is not None and new_path != current["nextcloud_path"])
            or (new_root is not None and new_root != current["path_root"])
        )
        if ownership_change and repo.count_state(mapping_id):
            raise HTTPException(
                409,
                "nextcloud_path/path_root cannot change after synchronization; create a new mapping",
            )
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
        try:
            updated = repo.update_mapping(mapping_id, values)
        except psycopg.errors.UniqueViolation as exc:
            raise HTTPException(409, "this folder-to-workspace mapping already exists") from exc
        if updated and updated["enabled"]:
            scheduler.request(mapping_id)
        return updated

    @application.delete("/mappings/{mapping_id}")
    def delete_mapping(mapping_id: int):
        get_mapping(mapping_id)
        repo.archive_mapping(mapping_id)
        return {"status": "archived", "mapping_id": mapping_id, "graph_purged": False}

    @application.post("/mappings/{mapping_id}/restore", response_model=MappingView)
    def restore_mapping(mapping_id: int):
        existing = repo.get_mapping(mapping_id, include_archived=True)
        if existing is None:
            raise HTTPException(404, "mapping not found")
        if existing.get("archived_at") is None:
            return existing
        restored = repo.restore_mapping(mapping_id)
        return restored

    @application.post("/mappings/{mapping_id}/enable", response_model=MappingView)
    def enable_mapping(mapping_id: int):
        get_mapping(mapping_id)
        mapping = repo.update_mapping(mapping_id, {"enabled": True})
        scheduler.request(mapping_id)
        return mapping

    @application.post("/mappings/{mapping_id}/disable", response_model=MappingView)
    def disable_mapping(mapping_id: int):
        get_mapping(mapping_id)
        return repo.update_mapping(mapping_id, {"enabled": False})

    @application.post("/mappings/{mapping_id}/run", response_model=RunAccepted, status_code=202)
    def run_mapping(mapping_id: int):
        mapping = get_mapping(mapping_id)
        if not mapping["enabled"]:
            raise HTTPException(409, "mapping is disabled")
        scheduler.request(mapping_id)
        return RunAccepted(mapping_id=mapping_id)

    return application


app = create_app()
