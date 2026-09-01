from __future__ import annotations

import asyncio
from typing import Any

import pytest

from steam_mcp.server import (
    _HttpGateway,
    _read_bool_env,
    _read_int_env,
    _read_path_env,
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
