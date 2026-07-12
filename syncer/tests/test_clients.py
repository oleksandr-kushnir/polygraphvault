from __future__ import annotations

import httpx
import pytest

from app.models import MappingCreate
from app.polygraph import PolyGraphClient
from app.webdav import WebDavClient


def dav_xml(items):
    responses = []
    for href, is_collection, etag, size in items:
        resource = "<d:collection/>" if is_collection else ""
        responses.append(
            f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>"
            f"<d:getetag>\"{etag}\"</d:getetag><d:getcontentlength>{size}</d:getcontentlength>"
            f"<d:getlastmodified>Sun, 12 Jul 2026 10:00:00 GMT</d:getlastmodified>"
            f"<d:resourcetype>{resource}</d:resourcetype>"
            f"</d:prop></d:propstat></d:response>"
        )
    body = "<?xml version='1.0'?><d:multistatus xmlns:d='DAV:'>" + "".join(responses) + "</d:multistatus>"
    return body.encode()


def test_webdav_walk_is_recursive_and_decodes_paths():
    def handler(request: httpx.Request):
        path = request.url.path.rstrip("/")
        if path.endswith("/Projects/Alpha"):
            return httpx.Response(207, content=dav_xml([
                ("/remote.php/dav/files/admin/Projects/Alpha/", True, "root", 0),
                ("/remote.php/dav/files/admin/Projects/Alpha/Sub%20Folder/", True, "sub", 0),
                ("/remote.php/dav/files/admin/Projects/Alpha/readme.md", False, "e1", 5),
            ]))
        if path.endswith("/Projects/Alpha/Sub%20Folder") or path.endswith("/Projects/Alpha/Sub Folder"):
            return httpx.Response(207, content=dav_xml([
                ("/remote.php/dav/files/admin/Projects/Alpha/Sub%20Folder/", True, "sub", 0),
                ("/remote.php/dav/files/admin/Projects/Alpha/Sub%20Folder/Guide.pdf", False, "e2", 12),
            ]))
        raise AssertionError(str(request.url))

    client = WebDavClient("http://nextcloud", "admin", "pw")
    client._http.close()
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    files = client.list("Projects/Alpha")
    assert set(files) == {"readme.md", "Sub Folder/Guide.pdf"}
    assert files["Sub Folder/Guide.pdf"].etag == "e2"


def test_collection_flag_survives_later_propstat_block():
    root_xml = b"""<?xml version='1.0'?>
    <d:multistatus xmlns:d='DAV:'>
      <d:response><d:href>/remote.php/dav/files/admin/Root/</d:href>
        <d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat>
        <d:propstat><d:prop><d:getetag/></d:prop></d:propstat>
      </d:response>
      <d:response><d:href>/remote.php/dav/files/admin/Root/Nested/</d:href>
        <d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat>
        <d:propstat><d:prop><d:getetag/></d:prop></d:propstat>
      </d:response>
    </d:multistatus>"""
    nested_xml = dav_xml([
        ("/remote.php/dav/files/admin/Root/Nested/", True, "sub", 0),
        ("/remote.php/dav/files/admin/Root/Nested/file.md", False, "e1", 3),
    ])

    def handler(request: httpx.Request):
        is_nested = request.url.path.rstrip("/").endswith("Nested")
        return httpx.Response(207, content=nested_xml if is_nested else root_xml)

    client = WebDavClient("http://nextcloud", "admin", "pw")
    client._http.close()
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    assert set(client.list("Root")) == {"Nested/file.md"}


def test_readonly_canary_mode_does_not_put_missing_marker():
    methods = []

    def handler(request: httpx.Request):
        methods.append(request.method)
        return httpx.Response(404)

    client = WebDavClient("http://nextcloud", "admin", "pw")
    client._http.close()
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    assert client.ensure_sentinel("Projects/Alpha", autocreate=False) is False
    assert methods == ["PROPFIND"]


def test_polygraph_client_sends_current_metadata_contract_and_polls_done():
    requests = []

    def handler(request: httpx.Request):
        requests.append(request)
        if request.method == "POST" and request.url.path.endswith("/upload/batch"):
            body = request.content.decode("utf-8", errors="ignore")
            assert '"source_path": "Projects/Alpha/legal/msa.md"' in body
            assert '"path_root": "/nextcloud/admin"' in body
            return httpx.Response(200, json={"jobs": [{"job_id": "job-1"}]})
        if request.method == "GET" and request.url.path.endswith("/status/job-1"):
            return httpx.Response(200, json={
                "status": "done", "doc_id": "doc-1", "content_hash": "abc"
            })
        raise AssertionError(str(request.url))

    client = PolyGraphClient("http://poly", "token", poll_interval=0)
    client._http.close()
    client._http = httpx.Client(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer token"},
    )
    result = client.ingest(
        "alpha", "Projects/Alpha/legal/msa.md", "/nextcloud/admin", b"terms"
    )
    assert result.doc_id == "doc-1"
    assert all(r.headers.get("authorization") == "Bearer token" for r in requests)


def test_polygraph_file_index_keeps_newest_duplicate():
    rows = [
        {"source_path": "Projects/A/a.md", "doc_id": "new", "content_hash": "2", "status": "done"},
        {"source_path": "Projects/A/a.md", "doc_id": "old", "content_hash": "1", "status": "done"},
    ]
    client = PolyGraphClient("http://poly")
    client._http.close()
    client._http = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"files": rows}))
    )
    assert client.list_files("alpha")["Projects/A/a.md"].doc_id == "new"


def test_mapping_rejects_unknown_extension_and_bad_workspace_slug():
    with pytest.raises(ValueError):
        MappingCreate(nextcloud_path="Source", workspace_id="good", include_extensions="md,exe")
    with pytest.raises(ValueError):
        MappingCreate(nextcloud_path="Source", workspace_id="Bad-Workspace")
