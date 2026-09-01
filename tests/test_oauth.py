from __future__ import annotations

import asyncio
import base64
import hashlib
from urllib.parse import parse_qs, urlsplit

from mcp.server.auth.provider import AuthorizationParams
from pydantic import AnyUrl
from starlette.requests import Request

from steam_mcp.oauth import (
    CHATGPT_STABLE_CLIENT_ID,
    CHATGPT_STABLE_REDIRECT_URI,
    create_oauth_runtime,
)


ISSUER = "https://steam.example.run.app"
RESOURCE = f"{ISSUER}/mcp"
LOGIN_SECRET = "login-" + "a" * 40
SIGNING_SECRET = "signing-" + "b" * 40


def _runtime():
    return create_oauth_runtime(
        issuer=ISSUER,
        resource=RESOURCE,
        scope="steam.read",
        login_secret=LOGIN_SECRET,
        signing_secret=SIGNING_SECRET,
        access_token="static-" + "c" * 40,
        store="memory",
        project="",
        collection="test_codes",
    )


def _request(method: str, url: str, body: bytes = b"") -> Request:
    parsed = urlsplit(url)
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": parsed.scheme,
            "path": parsed.path,
            "raw_path": parsed.path.encode(),
            "query_string": parsed.query.encode(),
            "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
            "client": ("127.0.0.1", 1234),
            "server": (parsed.hostname or "localhost", parsed.port or 443),
        },
        receive,
    )


def test_oauth_metadata_and_chatgpt_client_contract() -> None:
    runtime = _runtime()
    metadata = runtime.provider.authorization_metadata()
    assert metadata["client_id_metadata_document_supported"] is True
    assert metadata["authorization_response_iss_parameter_supported"] is True
    assert metadata["token_endpoint_auth_methods_supported"] == ["none"]
    client = asyncio.run(runtime.provider.get_client(CHATGPT_STABLE_CLIENT_ID))
    assert client is not None
    assert [str(value) for value in client.redirect_uris or []] == [
        CHATGPT_STABLE_REDIRECT_URI
    ]


def test_personal_login_issues_one_time_audience_bound_token() -> None:
    runtime = _runtime()
    provider = runtime.provider
    client = asyncio.run(provider.get_client(CHATGPT_STABLE_CLIENT_ID))
    assert client is not None
    verifier = "v" * 64
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    login_url = asyncio.run(
        provider.authorize(
            client,
            AuthorizationParams(
                state="opaque-state",
                scopes=["steam.read"],
                code_challenge=challenge,
                redirect_uri=AnyUrl(CHATGPT_STABLE_REDIRECT_URI),
                redirect_uri_provided_explicitly=True,
                resource=RESOURCE,
            ),
        )
    )
    transaction = parse_qs(urlsplit(login_url).query)["transaction"][0]
    response = asyncio.run(
        provider.login(
            _request(
                "POST",
                f"{ISSUER}/oauth/login",
                f"transaction={transaction}&access_key={LOGIN_SECRET}".encode(),
            )
        )
    )
    assert response.status_code == 302
    location = response.headers["location"]
    returned = parse_qs(urlsplit(location).query)
    assert returned["state"] == ["opaque-state"]
    assert returned["iss"] == [ISSUER]
    code = returned["code"][0]
    authorization_code = asyncio.run(provider.load_authorization_code(client, code))
    assert authorization_code is not None
    tokens = asyncio.run(provider.exchange_authorization_code(client, authorization_code))
    assert tokens.refresh_token
    access = asyncio.run(provider.load_access_token(tokens.access_token))
    assert access is not None
    assert access.resource == RESOURCE
    assert asyncio.run(provider.load_authorization_code(client, code)) is None


def test_login_rejects_wrong_secret_without_consuming_transaction() -> None:
    runtime = _runtime()
    provider = runtime.provider
    client = asyncio.run(provider.get_client(CHATGPT_STABLE_CLIENT_ID))
    assert client is not None
    login_url = asyncio.run(
        provider.authorize(
            client,
            AuthorizationParams(
                state=None,
                scopes=["steam.read"],
                code_challenge="x" * 43,
                redirect_uri=AnyUrl(CHATGPT_STABLE_REDIRECT_URI),
                redirect_uri_provided_explicitly=True,
                resource=RESOURCE,
            ),
        )
    )
    transaction = parse_qs(urlsplit(login_url).query)["transaction"][0]
    response = asyncio.run(
        provider.login(
            _request(
                "POST",
                f"{ISSUER}/oauth/login",
                f"transaction={transaction}&access_key=wrong".encode(),
            )
        )
    )
    assert response.status_code == 401
    assert b"not accepted" in response.body
