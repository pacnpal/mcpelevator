"""End-to-end OAuth: a real handshake against an in-process mock authorization
server + protected MCP endpoint, with no network (httpx ASGITransport).

This exercises the *actual* MCP SDK provider path — protected-resource discovery,
authorization-server metadata discovery, Dynamic Client Registration, the PKCE
authorization-code grant, the token exchange, and a later refresh — the same code
the control plane and bridge run in production. The only thing stubbed is the
browser: the test fetches the authorization URL itself and reads the redirect,
exactly as a signed-in operator's browser would.

Flow proven:
    begin_authorization → (discovery + DCR) → auth URL
    → "browser" GET /authorize → 302 back with code+state
    → complete_authorization → token exchange → tokens stored
    → bridge provider on an *expired* token → silent refresh → 200 from /mcp
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

from app.auth import oauth_flow
from app.auth.oauth_store import ServerTokenStorage
from app.bridge import host

MCP_URL = "https://mock/mcp"
ISSUER = "https://mock"

# Access tokens the mock has issued and still honors; refresh rotates in a new one.
_ISSUED_ACCESS = {"access-1"}
_CODES: dict[str, dict] = {}


def _prm(_request: Request) -> JSONResponse:
    return JSONResponse({"resource": MCP_URL, "authorization_servers": [ISSUER]})


def _asm(_request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "issuer": ISSUER,
            "authorization_endpoint": f"{ISSUER}/authorize",
            "token_endpoint": f"{ISSUER}/token",
            "registration_endpoint": f"{ISSUER}/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
        }
    )


async def _register(request: Request) -> JSONResponse:
    body = await request.json()
    return JSONResponse(
        {
            "client_id": "mock-client-id",
            "redirect_uris": body.get("redirect_uris", []),
            "grant_types": body.get("grant_types", ["authorization_code", "refresh_token"]),
            "response_types": body.get("response_types", ["code"]),
            "token_endpoint_auth_method": body.get("token_endpoint_auth_method", "none"),
        },
        status_code=201,
    )


def _authorize(request: Request) -> RedirectResponse:
    q = request.query_params
    code = "auth-code-xyz"
    _CODES[code] = {"redirect_uri": q["redirect_uri"], "state": q.get("state")}
    sep = "&" if "?" in q["redirect_uri"] else "?"
    return RedirectResponse(f"{q['redirect_uri']}{sep}code={code}&state={q.get('state', '')}", status_code=302)


async def _token(request: Request) -> JSONResponse:
    form = await request.form()
    grant = form.get("grant_type")
    if grant == "authorization_code":
        if form.get("code") not in _CODES:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        access = "access-1"
    elif grant == "refresh_token":
        if form.get("refresh_token") != "refresh-1":
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        access = "access-2"  # rotate the access token on refresh
    else:
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
    _ISSUED_ACCESS.add(access)
    return JSONResponse(
        {
            "access_token": access,
            "token_type": "Bearer",
            "refresh_token": "refresh-1",
            "expires_in": 3600,
            "scope": "read",
        }
    )


def _mcp(request: Request) -> JSONResponse:
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else ""
    if token not in _ISSUED_ACCESS:
        return JSONResponse(
            {"error": "unauthorized"},
            status_code=401,
            headers={
                "WWW-Authenticate": (
                    f'Bearer resource_metadata="{ISSUER}/.well-known/oauth-protected-resource/mcp"'
                )
            },
        )
    return JSONResponse({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})


def _mock_app() -> Starlette:
    return Starlette(
        routes=[
            Route("/.well-known/oauth-protected-resource/mcp", _prm),
            Route("/.well-known/oauth-protected-resource", _prm),
            Route("/.well-known/oauth-authorization-server", _asm),
            Route("/register", _register, methods=["POST"]),
            Route("/authorize", _authorize),
            Route("/token", _token, methods=["POST"]),
            Route("/mcp", _mcp, methods=["GET", "POST"]),
        ]
    )


@pytest.fixture
def mock_transport(monkeypatch):
    """Route every httpx client the flow builds at the in-process mock app."""
    app = _mock_app()
    transport = httpx.ASGITransport(app=app)
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(oauth_flow.httpx, "AsyncClient", factory)
    _ISSUED_ACCESS.clear()
    _ISSUED_ACCESS.add("access-1")  # reset baseline; access-1 becomes valid after exchange
    _ISSUED_ACCESS.clear()  # start empty so /mcp 401s until the exchange issues access-1
    _CODES.clear()
    return transport


class _Srv:
    id = "srv-e2e-1"
    command = MCP_URL
    args = ["streamable-http"]
    env: dict = {}
    oauth_client_id = None
    oauth_client_secret = None
    oauth_scopes = "read"


async def test_full_oauth_handshake_and_refresh(mock_transport):
    store = ServerTokenStorage(_Srv.id)
    store.clear()
    callback = "http://127.0.0.1:8080/api/oauth/callback"
    try:
        # 1) Begin: real discovery + DCR against the mock, returns the auth URL.
        auth_url = await oauth_flow.begin_authorization(_Srv, callback_url=callback)
        assert auth_url.startswith(f"{ISSUER}/authorize")
        qs = parse_qs(urlparse(auth_url).query)
        state = qs["state"][0]

        # 2) "Browser": follow the auth URL, read the redirect back with code+state.
        async with httpx.AsyncClient(transport=mock_transport) as browser:
            resp = await browser.get(auth_url, follow_redirects=False)
        assert resp.status_code == 302
        loc = parse_qs(urlparse(resp.headers["location"]).query)
        code = loc["code"][0]
        assert loc["state"][0] == state

        # 3) Complete: token exchange, tokens land in the shared store.
        server_id = await oauth_flow.complete_authorization(state, code)
        assert server_id == _Srv.id
        tokens = await store.get_tokens()
        assert tokens is not None and tokens.access_token == "access-1"
        assert tokens.refresh_token == "refresh-1"
        # Discovered metadata was persisted for the bridge's refresh path.
        assert store.get_metadata() is not None

        # 4) Bridge refresh: force the stored access token to look expired, then have the
        # bridge-side provider make a request. It must refresh silently (access-1 ->
        # access-2) rather than fall into the interactive path, and get a 200.
        data = store._read()
        data["expires_at"] = 1.0  # a real past timestamp (unix epoch + 1s) — long expired
        store._write(data)

        provider = host._build_oauth_auth(
            {"server_id": _Srv.id, "url": MCP_URL, "scopes": "read"}
        )
        async with httpx.AsyncClient(auth=provider, transport=mock_transport) as client:
            r = await client.post(MCP_URL, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert r.status_code == 200, r.text
        refreshed = await store.get_tokens()
        assert refreshed is not None and refreshed.access_token == "access-2"
    finally:
        store.clear()
