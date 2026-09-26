"""Real loopback boundary tests using only synthetic Memory Explorer data."""
from __future__ import annotations

import http.client
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time

import pytest

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))
from _explorer_store import ExplorerStore  # noqa: E402
from _explorer_store import ExplorerError  # noqa: E402

spec = importlib.util.spec_from_file_location("explore_memory", BIN / "explore-memory.py")
explorer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = explorer
spec.loader.exec_module(explorer)


@pytest.fixture
def service():
    with ExplorerStore(demo=True) as store:
        server = explorer.create_server(store)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server, store
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()


def request(server, method, path, *, body=None, token=True, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    all_headers = dict(headers or {})
    if token:
        all_headers["Authorization"] = "Bearer " + server.capability
    if body is not None and not isinstance(body, bytes):
        body = json.dumps(body).encode("utf-8")
    if body is not None:
        all_headers.setdefault("Content-Type", "application/json")
    conn.request(method, path, body=body, headers=all_headers)
    response = conn.getresponse()
    data = response.read()
    result = (response.status, dict(response.getheaders()), data)
    conn.close()
    return result


def json_request(server, method, path, **kwargs):
    status, headers, data = request(server, method, path, **kwargs)
    return status, headers, json.loads(data)


def test_summary_records_detail_and_preview(service):
    server, store = service
    status, headers, summary = json_request(server, "GET", "/api/summary")
    assert status == 200
    assert summary["snapshot_id"] == store.snapshot_id
    assert summary["mode"] == "demo"
    assert headers["Cache-Control"] == "no-store"
    assert "default-src 'self'" in headers["Content-Security-Policy"]
    assert "Access-Control-Allow-Origin" not in headers
    status, _, listed = json_request(server, "GET", "/api/records?lifecycle=all&limit=2")
    assert status == 200 and len(listed["items"]) == 2
    assert "details" not in listed["items"][0]
    handle = listed["items"][0]["handle"]
    path = "/api/record?handle=%s&snapshot_id=%s" % (handle, store.snapshot_id)
    status, _, detail = json_request(server, "GET", path)
    assert status == 200 and detail["handle"] == handle
    status, _, preview = json_request(server, "POST", "/api/preview", body={
        "query": "what did we decide about Friday delivery", "budget": 800,
        "snapshot_id": store.snapshot_id,
    })
    assert status == 200 and preview["items"]


@pytest.mark.parametrize("headers,token", [({}, False),
                                         ({"Authorization": "Bearer wrong"}, False),
                                         ({"Host": "localhost:1"}, True),
                                         ({"Origin": "https://evil.example"}, True),
                                         ({"Sec-Fetch-Site": "cross-site"}, True)])
def test_capability_host_and_origin(service, headers, token):
    server, _ = service
    status, response_headers, error = json_request(
        server, "GET", "/api/summary", headers=headers, token=token)
    assert status == 403
    assert set(error) == {"error"}
    assert "Access-Control-Allow-Origin" not in response_headers
    status, _, _ = json_request(server, "GET", "/api/summary", headers={"Origin": server.origin})
    assert status == 200


def test_exact_routes_traversal_and_parameter_validation(service):
    server, _ = service
    for path in ("/api/../facts.json", "/facts.json", "/api/summary/", "/%2e%2e/signing-key.pub"):
        status, _, _ = json_request(server, "GET", path)
        assert status == 404
    for path in ("/api/records?offset=-1", "/api/records?limit=101",
                 "/api/records?limit=true", "/api/records?offset=0&offset=1",
                 "/api/summary?x=1", "/api/record?handle=facts:0"):
        status, _, _ = json_request(server, "GET", path)
        assert status == 400
    status, _, _ = json_request(server, "GET", "/api/records?query=" + "a" * 9000)
    assert status == 413


def test_post_bounds_types_and_stale_snapshot(service):
    server, store = service
    sid = store.snapshot_id
    for body in ([], {"query": "delivery", "budget": True, "snapshot_id": sid},
                 {"query": "delivery", "budget": 800, "snapshot_id": 12},
                 {"query": "delivery", "budget": 800, "snapshot_id": sid, "private": 1},
                 {"query": "x" * 2001, "budget": 800, "snapshot_id": sid}):
        status, _, _ = json_request(server, "POST", "/api/preview", body=body)
        assert status == 400
    status, _, _ = json_request(server, "POST", "/api/refresh", body={"extra": True})
    assert status == 400
    status, _, _ = json_request(server, "POST", "/api/refresh", body=b"[1]")
    assert status == 400
    status, _, _ = json_request(server, "POST", "/api/refresh", body=b"x" * (16 * 1024 + 1))
    assert status == 413
    status, _, fresh = json_request(server, "POST", "/api/refresh", body={})
    assert status == 200 and fresh["snapshot_id"] != sid
    status, _, _ = json_request(server, "GET", "/api/record?handle=facts:0&snapshot_id=" + sid)
    assert status == 409
    status, _, _ = json_request(server, "POST", "/api/preview", body={
        "query": "delivery", "budget": 800, "snapshot_id": sid,
    })
    assert status == 409


def test_failed_refresh_keeps_previous_snapshot(service, monkeypatch):
    server, store = service
    sid = store.snapshot_id

    def fail_refresh():
        raise ExplorerError("A stable snapshot could not be obtained.")

    monkeypatch.setattr(store, "refresh", fail_refresh)
    status, _, body = json_request(server, "POST", "/api/refresh", body={})
    assert status == 503 and "refresh failed" in body["error"]
    assert store.snapshot_id == sid
    status, _, summary = json_request(server, "GET", "/api/summary")
    assert status == 200 and summary["snapshot_id"] == sid
    status, _, _ = json_request(server, "POST", "/api/preview", body={
        "query": "delivery", "budget": 800, "snapshot_id": sid,
    })
    assert status == 200


def test_slow_socket_does_not_block_other_requests(service):
    server, _ = service
    slow = socket.create_connection(("127.0.0.1", server.server_port), timeout=3)
    try:
        partial = (("POST /api/refresh HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
                    "Authorization: Bearer %s\r\nContent-Type: application/json\r\n"
                    "Content-Length: 10\r\n\r\n{") %
                   (server.server_port, server.capability)).encode("ascii")
        slow.sendall(partial)
        started = time.monotonic()
        status, _, _ = json_request(server, "GET", "/api/summary")
        assert status == 200 and time.monotonic() - started < 1.5
    finally:
        slow.close()


def test_http_timeout_and_quiet_logs(service, monkeypatch, capsys):
    server, store = service

    def timed_out(*_args, **_kwargs):
        raise explorer.PreviewError("Preview timed out.")

    monkeypatch.setattr(explorer, "run_preview", timed_out)
    status, _, error = json_request(server, "POST", "/api/preview", body={
        "query": "PRIVATE_QUERY_TOKEN", "budget": 800, "snapshot_id": store.snapshot_id,
    })
    assert status == 504 and error == {"error": "Preview timed out."}
    output = capsys.readouterr()
    assert "PRIVATE_QUERY_TOKEN" not in output.out + output.err
    assert server.capability not in output.out + output.err


def test_fixed_assets_require_safe_host(service):
    server, _ = service
    status, headers, body = request(server, "GET", "/", token=False)
    assert status == 200 and b"<html" in body.lower()
    assert "Access-Control-Allow-Origin" not in headers
    status, _, _ = json_request(server, "GET", "/", token=False,
                                headers={"Host": "evil.example"})
    assert status == 403


def test_no_mode_never_constructs_store(monkeypatch, capsys):
    def fail(*_args, **_kwargs):
        raise AssertionError("store must not be constructed")
    monkeypatch.setattr(explorer, "ExplorerStore", fail)
    assert explorer.main([]) == 0
    assert "--demo" in capsys.readouterr().out


def test_sqlite_mode_refused_before_store(monkeypatch, capsys):
    monkeypatch.setenv("NOCKBRAIN_STORE", "sqlite")
    assert explorer.main(["--demo"]) == 2
    assert "SQLite" in capsys.readouterr().err


def test_cli_sigterm_cleans_owned_scratch(tmp_path):
    env = dict(os.environ, TMPDIR=str(tmp_path), NOCKBRAIN_STORE="json")
    before = set(Path("/tmp").glob("nock-explorer-*"))
    process = subprocess.Popen(
        [sys.executable, str(BIN / "explore-memory.py"), "--demo"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    try:
        url = process.stdout.readline().strip()
        assert url.startswith("http://127.0.0.1:") and "#token=" in url
        created = set(Path("/tmp").glob("nock-explorer-*")) - before
        assert created and not list(tmp_path.glob("nock-explorer-*"))
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=5) == 0
        assert not any(path.exists() for path in created)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_private_fields_do_not_enter_http(tmp_path):
    source = tmp_path / "store"
    source.mkdir()
    marker = "DO_NOT_LEAK_PRIVATE_FIELD"
    fact = {"id": "sample", "kind": "decision", "status": "current", "confidence": 0.8,
            "content": "<script>alert('literal')</script>", "source_date": "2026-09-01",
            "secret_internal": marker}
    (source / "facts.json").write_text(json.dumps([fact]))
    (source / "signing-key").write_text(marker)
    with ExplorerStore(store=source) as store:
        server = explorer.create_server(store)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for path in ("/api/summary", "/api/records?lifecycle=all",
                         "/api/record?handle=facts:0&snapshot_id=" + store.snapshot_id):
                status, _, data = request(server, "GET", path)
                assert status == 200 and marker.encode() not in data
            status, _, detail = json_request(server, "GET", "/api/record?handle=facts:0&snapshot_id=" + store.snapshot_id)
            assert detail["content"] == fact["content"]
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()
