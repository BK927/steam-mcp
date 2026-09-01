from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

import pytest

from steam_mcp.server import (
    _HttpGateway,
    _build_http_app,
    _read_bool_env,
    _read_int_env,
    _read_path_env,
    mcp,
)


async def _inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
    await send({"type": "http.response.start", "status": 204, "headers": []})
    await send({"type": "http.response.body", "body": b""})


def _request(app: Any, path: str, token: str = "") -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    headers = []
    if token:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    scope = {"type": "http", "path": path, "method": "GET", "headers": headers}

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    asyncio.run(app(scope, receive, send))
    return messages


def _asgi_request(
    app: Any,
    path: str,
    *,
    body: bytes = b"{}",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    supplied = list(headers or [])
    supplied.append((b"content-length", str(len(body)).encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "server": ("steam.example.run.app", 443),
        "client": ("127.0.0.1", 1234),
        "headers": supplied,
    }
    delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    asyncio.run(app(scope, receive, send))
    return messages


def test_http_gateway_health_and_auth() -> None:
    token = "a" * 32
    app = _HttpGateway(
        _inner,
        mcp_path="/mcp",
        health_path="/healthz",
        access_token=token,
        allow_unauthenticated=False,
    )
    assert _request(app, "/healthz")[0]["status"] == 200
    assert _request(app, "/mcp")[0]["status"] == 401
    assert _request(app, "/mcp", token)[0]["status"] == 204


def test_http_gateway_serves_oauth_metadata() -> None:
    from steam_mcp.oauth import create_oauth_runtime

    oauth = create_oauth_runtime(
        issuer="https://steam.example.run.app",
        resource="https://steam.example.run.app/mcp",
        scope="steam.read",
        login_secret="l" * 32,
        signing_secret="s" * 32,
        access_token="a" * 32,
        store="memory",
        project="",
        collection="test",
    )
    app = _HttpGateway(
        _inner,
        mcp_path="/mcp",
        health_path="/healthz",
        access_token="a" * 32,
        allow_unauthenticated=False,
        oauth=oauth,
    )
    response = _request(app, "/.well-known/oauth-authorization-server")
    assert response[0]["status"] == 200
    body = json.loads(response[1]["body"])
    assert body["issuer"] == "https://steam.example.run.app"
    assert body["client_id_metadata_document_supported"] is True


def test_runtime_env_readers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLAG", "true")
    monkeypatch.setenv("COUNT", "8080")
    monkeypatch.setenv("PATH_VALUE", "/mcp/")
    assert _read_bool_env("FLAG") is True
    assert _read_int_env("COUNT", 1, 1, 65535) == 8080
    assert _read_path_env("PATH_VALUE", "/default") == "/mcp"

    monkeypatch.setenv("FLAG", "sometimes")
    with pytest.raises(RuntimeError):
        _read_bool_env("FLAG")


def _registry_hash() -> str:
    async def snapshot() -> dict[str, Any]:
        return {
            "tools": [
                tool.model_dump(by_alias=True, exclude_none=True)
                for tool in await mcp.list_tools()
            ],
            "templates": [
                template.model_dump(by_alias=True, exclude_none=True)
                for template in await mcp.list_resource_templates()
            ],
            "resources": [
                resource.model_dump(by_alias=True, exclude_none=True)
                for resource in await mcp.list_resources()
            ],
            "prompts": [
                prompt.model_dump(by_alias=True, exclude_none=True)
                for prompt in await mcp.list_prompts()
            ],
        }

    raw = json.dumps(asyncio.run(snapshot()), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def test_http_uses_same_registry_and_default_two_mib_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCP_ALLOW_UNAUTHENTICATED", "true")
    monkeypatch.setenv("STEAM_PROCESS_ROLE", "mcp")
    monkeypatch.delenv("HTTP_MAX_BODY_BYTES", raising=False)
    before = _registry_hash()
    gateway, _, _ = _build_http_app()
    route = gateway.app.routes[0]
    manager = route.app.session_manager
    assert manager.app is mcp._lowlevel_server
    assert manager.max_request_body_size == 2_097_152
    assert _registry_hash() == before


def test_http_rejects_bad_host_origin_and_oversized_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp.server.transport_security import TransportSecurityMiddleware
    from starlette.requests import Request

    monkeypatch.setenv("MCP_ALLOW_UNAUTHENTICATED", "true")
    monkeypatch.setenv("STEAM_PROCESS_ROLE", "mcp")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://steam.example.run.app")
    gateway, _, _ = _build_http_app()
    common = [(b"content-type", b"application/json")]
    manager = gateway.app.routes[0].app.session_manager
    security = TransportSecurityMiddleware(manager.security_settings)

    def security_response(headers: list[tuple[bytes, bytes]]) -> Any:
        scope = {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "path": "/mcp",
            "headers": headers,
        }
        return asyncio.run(security.validate_request(Request(scope), is_post=True))

    bad_host = security_response([*common, (b"host", b"evil.example")])
    assert bad_host.status_code == 421
    bad_origin = security_response(
        [
            *common,
            (b"host", b"steam.example.run.app"),
            (b"origin", b"https://evil.example"),
        ]
    )
    assert bad_origin.status_code == 403

    oversized = _asgi_request(
        gateway,
        "/mcp",
        body=b"x" * (2_097_152 + 1),
        headers=[
            *common,
            (b"host", b"steam.example.run.app"),
            (b"origin", b"https://steam.example.run.app"),
        ],
    )
    assert oversized[0]["status"] == 413


def test_worker_role_wires_private_path_token_and_retry_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    class Analysis:
        async def run_job(self, job_id: str, payload: dict[str, Any]) -> None:
            seen.append((job_id, payload))

    monkeypatch.setitem(mcp._steam_services, "analysis", Analysis())
    monkeypatch.setenv("STEAM_PROCESS_ROLE", "worker")
    monkeypatch.setenv("STEAM_JOB_WORKER_TOKEN", "private-token")
    gateway, _, _ = _build_http_app()
    payload = json.dumps({"job_id": "job", "payload": {"task": "game_overview"}}).encode()
    wrong = _asgi_request(
        gateway,
        "/internal/jobs/run",
        body=payload,
        headers=[(b"x-steam-worker-token", b"wrong")],
    )
    assert wrong[0]["status"] == 401
    accepted = _asgi_request(
        gateway,
        "/internal/jobs/run",
        body=payload,
        headers=[
            (b"x-steam-worker-token", b"private-token"),
            (b"x-cloudtasks-taskretrycount", b"2"),
        ],
    )
    assert accepted[0]["status"] == 200
    assert seen == [("job", {"task": "game_overview", "_attempt": 2})]
