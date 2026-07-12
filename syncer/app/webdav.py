from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import quote, unquote, urlsplit

import httpx

DAV = "{DAV:}"
SENTINEL = ".nc-rag-sync-health"
PROPFIND_BODY = (
    b'<?xml version="1.0"?>'
    b'<d:propfind xmlns:d="DAV:"><d:prop>'
    b"<d:getetag/><d:getcontentlength/><d:getlastmodified/><d:resourcetype/>"
    b"</d:prop></d:propfind>"
)


def clean_etag(value: str | None) -> str:
    value = (value or "").strip()
    if value.startswith("W/"):
        value = value[2:]
    return value.strip('"')


@dataclass(frozen=True)
class WebDavEntry:
    etag: str
    size: int
    last_modified: str


class WebDavClient:
    def __init__(self, base_url: str, user: str, password: str, timeout: float = 60.0) -> None:
        self._root = f"{base_url.rstrip('/')}/remote.php/dav/files/{quote(user, safe='')}"
        self._root_path = unquote(urlsplit(self._root).path).rstrip("/")
        self._http = httpx.Client(auth=(user, password), timeout=timeout)

    def close(self) -> None:
        self._http.close()

    def _url(self, path: str) -> str:
        path = path.replace("\\", "/").strip("/")
        return self._root if not path else f"{self._root}/{quote(path, safe='/')}"

    def _request_propfind(self, path: str, depth: str) -> httpx.Response:
        url = self._url(path)
        if not url.endswith("/"):
            url += "/"
        return self._http.request(
            "PROPFIND",
            url,
            headers={"Depth": depth, "Content-Type": "application/xml"},
            content=PROPFIND_BODY,
        )

    def validate_folder(self, folder: str) -> None:
        response = self._request_propfind(folder, "0")
        if response.status_code == 404:
            raise FileNotFoundError(f"Nextcloud folder not found: {folder}")
        response.raise_for_status()

    def ensure_sentinel(self, folder: str, autocreate: bool = True) -> bool:
        path = f"{folder.strip('/')}/{SENTINEL}"
        response = self._http.request(
            "PROPFIND",
            self._url(path),
            headers={"Depth": "0", "Content-Type": "application/xml"},
            content=PROPFIND_BODY,
        )
        if response.status_code != 404:
            response.raise_for_status()
            return False
        if not autocreate:
            return False
        created = self._http.put(
            self._url(path), content=b"nc-rag-sync health canary - do not delete\n"
        )
        created.raise_for_status()
        return True

    def list(self, folder: str) -> dict[str, WebDavEntry]:
        folder = folder.strip("/")
        out: dict[str, WebDavEntry] = {}
        self._walk(folder, folder, out)
        return out

    def _walk(self, root: str, current: str, out: dict[str, WebDavEntry]) -> None:
        response = self._request_propfind(current, "1")
        if response.status_code == 404:
            raise FileNotFoundError(f"Nextcloud folder disappeared: {current}")
        response.raise_for_status()
        tree = ET.fromstring(response.content)
        current_absolute = f"{self._root_path}/{current}".rstrip("/")
        for item in tree.findall(f"{DAV}response"):
            href = item.find(f"{DAV}href")
            if href is None or not href.text:
                continue
            absolute = unquote(urlsplit(href.text).path).rstrip("/")
            prefix = f"{self._root_path}/{root}".rstrip("/")
            if absolute == current_absolute:
                continue
            if not absolute.startswith(prefix + "/"):
                continue
            rel = absolute[len(prefix) + 1 :]
            etag = ""
            size = 0
            modified = ""
            is_collection = False
            for propstat in item.findall(f"{DAV}propstat"):
                prop = propstat.find(f"{DAV}prop")
                if prop is None:
                    continue
                etag_node = prop.find(f"{DAV}getetag")
                size_node = prop.find(f"{DAV}getcontentlength")
                modified_node = prop.find(f"{DAV}getlastmodified")
                resource = prop.find(f"{DAV}resourcetype")
                if etag_node is not None:
                    etag = clean_etag(etag_node.text)
                if size_node is not None and size_node.text:
                    try:
                        size = int(size_node.text)
                    except ValueError:
                        size = 0
                if modified_node is not None and modified_node.text:
                    try:
                        modified = parsedate_to_datetime(modified_node.text).isoformat()
                    except (TypeError, ValueError):
                        modified = modified_node.text
                if resource is not None and resource.find(f"{DAV}collection") is not None:
                    # Nextcloud can return several propstat blocks. Once any successful
                    # block identifies a collection, a later block for missing optional
                    # properties must not turn it back into a file.
                    is_collection = True
            if is_collection:
                self._walk(root, f"{root}/{rel}", out)
            else:
                out[rel] = WebDavEntry(etag=etag, size=size, last_modified=modified)

    def read(self, folder: str, rel_path: str) -> tuple[bytes, str]:
        path = f"{folder.strip('/')}/{rel_path.strip('/')}"
        response = self._http.get(self._url(path))
        response.raise_for_status()
        etag = clean_etag(response.headers.get("ETag") or response.headers.get("OC-ETag"))
        return response.content, etag
