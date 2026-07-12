from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

import httpx


TERMINAL = {"done", "failed", "save_failed"}


class IngestError(RuntimeError):
    pass


@dataclass(frozen=True)
class GraphFile:
    source_path: str
    doc_id: str | None
    content_hash: str | None
    status: str


class PolyGraphClient:
    def __init__(
        self,
        base_url: str,
        api_token: str = "",
        ingest_timeout: float = 900.0,
        poll_interval: float = 2.0,
    ) -> None:
        headers = {"Authorization": f"Bearer {api_token}"} if api_token else {}
        self._base = base_url.rstrip("/")
        self._http = httpx.Client(headers=headers, timeout=120.0)
        self._ingest_timeout = ingest_timeout
        self._poll_interval = poll_interval

    def close(self) -> None:
        self._http.close()

    def health(self) -> bool:
        response = self._http.get(f"{self._base}/health")
        response.raise_for_status()
        return response.json().get("status") == "ok"

    def ensure_workspace(self, workspace_id: str, name: str | None, create: bool) -> None:
        response = self._http.get(f"{self._base}/all-workspaces/list")
        response.raise_for_status()
        if any(item.get("id") == workspace_id for item in response.json().get("workspaces", [])):
            return
        if not create:
            raise KeyError(f"PolyGraphRAG workspace not found: {workspace_id}")
        response = self._http.post(
            f"{self._base}/all-workspaces/create",
            json={"id": workspace_id, "name": name or workspace_id, "description": ""},
        )
        response.raise_for_status()

    def list_files(self, workspace_id: str) -> dict[str, GraphFile]:
        response = self._http.get(f"{self._base}/workspace/{workspace_id}/files")
        response.raise_for_status()
        result: dict[str, GraphFile] = {}
        for row in response.json().get("files", []):
            source_path = row.get("source_path")
            # The API returns newest first. Keep the newest row if an earlier failed
            # attempt left historical metadata for the same source path.
            if source_path and source_path not in result:
                result[source_path] = GraphFile(
                    source_path=source_path,
                    doc_id=row.get("doc_id"),
                    content_hash=row.get("content_hash"),
                    status=row.get("status") or "",
                )
        return result

    def ingest(
        self,
        workspace_id: str,
        source_path: str,
        path_root: str,
        data: bytes,
        last_modified: str = "",
    ) -> GraphFile:
        filename = os.path.basename(source_path) or "document"
        metadata = [{
            "source_path": source_path,
            "path_root": path_root,
            "last_modified_time": last_modified,
        }]
        response = self._http.post(
            f"{self._base}/workspace/{workspace_id}/upload/batch",
            files={"files": (filename, data, "application/octet-stream")},
            data={"metadata": json.dumps(metadata)},
        )
        response.raise_for_status()
        jobs = response.json().get("jobs", [])
        if not jobs or not jobs[0].get("job_id"):
            raise IngestError(f"upload rejected for {source_path}")
        return self._wait(workspace_id, source_path, jobs[0]["job_id"])

    def _wait(self, workspace_id: str, source_path: str, job_id: str) -> GraphFile:
        deadline = time.monotonic() + self._ingest_timeout
        while True:
            response = self._http.get(
                f"{self._base}/workspace/{workspace_id}/status/{job_id}"
            )
            response.raise_for_status()
            job = response.json()
            status = job.get("status")
            if status == "done":
                return GraphFile(
                    source_path=source_path,
                    doc_id=job.get("doc_id"),
                    content_hash=job.get("content_hash"),
                    status="done",
                )
            if status in TERMINAL:
                raise IngestError(f"ingest {status} for {source_path}: {job.get('error')}")
            if time.monotonic() >= deadline:
                raise IngestError(f"ingest timed out for {source_path}; last status={status}")
            time.sleep(self._poll_interval)

    def delete(self, workspace_id: str, doc_id: str) -> None:
        response = self._http.request(
            "DELETE",
            f"{self._base}/workspace/{workspace_id}/file/delete",
            json={"doc_id": doc_id},
        )
        response.raise_for_status()
