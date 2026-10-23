"""Debug mode (MCPE_DEBUG): credential fingerprints, the request trace, and the
admin-only diagnostics bundle."""

from __future__ import annotations

import base64
import json

import logging

import pytest
from fastapi.testclient import TestClient

from conftest import LOOPBACK, mint_token

from app import debug
from app.config import get_settings
from app.main import create_app
from app.runners.builtin import INTERNAL_SECRET


@pytest.fixture(autouse=True)
def _restore_logging():
    """Debug mode reconfigures every logger and the excepthooks process-wide; put them
    back for later tests."""
    import faulthandler
    import sys
    import threading

    names = ["", *logging.root.manager.loggerDict]
    saved = {n: (logging.getLogger(n).level, list(logging.getLogger(n).handlers),
                 logging.getLogger(n).propagate) for n in names}
    hooks = (sys.excepthook, threading.excepthook, faulthandler.is_enabled())
    yield
    for name in logging.root.manager.loggerDict:
        if name not in saved:
            logging.getLogger(name).setLevel(logging.NOTSET)
    for n, (level, handlers, propagate) in saved.items():
        log = logging.getLogger(n)
        log.setLevel(level)
        log.handlers[:] = handlers
        log.propagate = propagate
    for log in [logging.getLogger(), *(logging.getLogger(n) for n in logging.root.manager.loggerDict)]:
        for handler in log.handlers:
            for f in [f for f in handler.filters if isinstance(f, debug._ScrubFilter)]:
                handler.removeFilter(f)
    sys.excepthook, threading.excepthook = hooks[0], hooks[1]
    if not hooks[2]:
        faulthandler.disable()


def _jwt(claims: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJSUzI1NiJ9.{body}.sig"


def test_fingerprints_never_log_a_usable_credential():
    token = "mcpe_" + "x" * 40
    assert debug.fingerprint("Authorization", f"Bearer {token}") == "Bearer mcpe_xxxxxxx… (45 chars)"
    jwt = debug.fingerprint("authorization", f"Bearer {_jwt({'iss': 'https://idp', 'sub': 'ada', 'exp': 1})}")
    assert jwt["jwt"] == {"iss": "https://idp", "sub": "ada", "exp": 1, "expired": True}
    assert debug.fingerprint("x-mcpe-internal", INTERNAL_SECRET) == "<valid>"
    assert debug.fingerprint("x-mcpe-internal", "guess") == "<INVALID>"
    assert debug.fingerprint("cookie", "a=1; b=2") == "<2 cookie(s)>"
    assert debug.fingerprint("x-api-key", "sekrit") == "<redacted, 6 chars>"
    assert debug.fingerprint("accept", "text/plain") == "text/plain"


def test_scrub_hides_secrets_in_bodies_and_library_logs():
    token = "mcpe_" + "y" * 40
    out = debug.scrub(f'{{"token": "{token}", "env": {{"GITHUB_TOKEN": "ghp_1", "MODE": "x"}}}}')
    assert token not in out and "ghp_1" not in out and '"MODE": "x"' in out
    assert "sk-1" not in debug.scrub("arguments={'api_key': 'sk-1', 'q': 'cats'}")
    assert "<jwt>" in debug.scrub(f"Bearer {_jwt({'sub': 'a'})}")
    assert "it's" not in debug.scrub('{"password": "it\'s"}')
    out = debug.scrub('{"token": "ab\\"cd", "next": "ok"}')
    assert "ab" not in out and "cd" not in out and '"next": "ok"' in out


def test_scrub_redacts_opaque_credentials_and_truncated_fields():
    # Built at runtime so a secret scanner doesn't flag the fixture.
    opaque = "ghp" + "_" + "abc123XYZ789"
    out = debug.scrub(f"upstream said: Bearer {opaque} rejected")
    assert opaque not in out and "Bearer <redacted> rejected" in out
    for scheme in ("Bearer", "Token", "ApiKey"):  # any scheme in an Authorization header
        out = debug.scrub(f"Authorization: {scheme} {opaque}\nretrying")
        assert opaque not in out and out == "Authorization: <redacted>\nretrying"
    digest = 'Authorization: Digest username="ada", response="' + opaque + '"'
    assert debug.scrub(digest) == "Authorization: <redacted>"
    assert debug.scrub(f"Cookie: marker=first; session={opaque}") == "Cookie: <redacted>"
    assert debug.scrub(f"Set-Cookie: session={opaque}; HttpOnly") == "Set-Cookie: <redacted>"
    assert debug.fingerprint("authorization", opaque) == f"<redacted, {len(opaque)} chars>"
    basic = base64.b64encode(b"user:" + b"pass123").decode()
    assert basic not in debug.scrub(f"Basic {basic}")
    assert debug.scrub("no bearer provider configured") == "no bearer provider configured"
    pw = "s3%40" + "cr3t"  # built at runtime, as above
    url = debug.scrub('{"DATABASE_URL": "postgres://alice:' + pw + '@db/app"}')
    assert "alice" not in url and pw not in url and "postgres://<redacted>@db/app" in url
    raw = "p@" + "ss"
    assert debug.scrub(f"postgres://alice:{raw}@db/app") == "postgres://<redacted>@db/app"
    assert debug.scrub("http://host?email=a@b.com") == "http://host?email=a@b.com"
    assert "mcpe_abcdefg" in debug.scrub("Bearer mcpe_" + "abcdefg" * 6)
    # A body cut off inside a secret value has no closing quote to end the match.
    body = debug._body_text([b'{"api_key": "' + b"s3cr3t" * 5000 + b'"}'], 1024)
    assert "s3cr3t" not in body and body.endswith(" bytes)")


def test_scrub_stays_linear_on_long_keyword_heavy_lines():
    import time

    started = time.perf_counter()
    for line in ('{"text": "' + "key " * 32000 + '"}', "?" + "key" * 40000, "'key' " * 20000,
                 "?" * 60000, "?key" * 30000, "a." * 50000, "a-" * 50000 + "key:", "key " * 30000 + ":",
                 '{"key": [' * 20000, '"key": {} ' * 20000):
        debug.scrub(line)
    assert time.perf_counter() - started < 1.0  # over a minute before the scans were linear


def test_bundle_config_hides_url_passwords(monkeypatch):
    monkeypatch.setattr(get_settings(), "public_base_url", "https://ada:" + "s3cr3" + "t@mcp.example.com")
    assert debug._config()["public_base_url"] == "https://<redacted>@mcp.example.com"


def test_scrub_redacts_secret_fields_whatever_their_value():
    out = debug.scrub("""{"api_key": 123456789, "count": 3} {'token': b'opaque-cred'} {"total_tokens": 42}""")
    assert "123456789" not in out and "opaque-cred" not in out and '"count": 3' in out
    assert '"api_key": "<redacted>"' in out and "'token': b'<redacted>'" in out
    assert '"total_tokens": 42' in out  # a short count isn't a credential
    assert debug.scrub('{"token": null}') == '{"token": null}'
    # Nested containers are redacted to their matching close, brackets in strings aside.
    live = "sk_live" + "_99Nested"
    nested = '{"api_keys": [{"value": "' + live + '", "note": "a]b"}], "n": 1}'
    assert debug.scrub(nested) == '{"api_keys": "<redacted>", "n": 1}'
    assert live not in debug.scrub('{"token": {"a": [{"b": "' + live)  # cut off mid-value


def test_bundle_scrubs_every_string_it_returns():
    token = "mcpe_" + "q" * 40
    out = debug._scrubbed({"traces": [{"path": f"/s/x/{token}", "headers": {"authorization": "Bearer mcpe_qqqqqqq… (45 chars)"}}]})
    assert token not in str(out)
    assert out["traces"][0]["headers"]["authorization"] == "Bearer mcpe_qqqqqqq… (45 chars)"


def test_scrub_redacts_secret_fields_in_header_and_config_syntax():
    live = "sk_live" + "_51Habc123"
    out = debug.scrub(f"X-API-Key: {live}\nclient_secret: opaque-Value9 user: ada")
    assert live not in out and "opaque-Value9" not in out and "user: ada" in out
    assert "X-API-Key: <redacted>" in out
    assert debug.scrub("missing key: retrying") == "missing key: retrying"
    # The name decides: a short or all-letter value in a credential field is redacted,
    # a count stays.
    pw = "summer" + "flower"
    assert pw not in debug.scrub(f"client_secret: {pw}")
    assert "12345" not in debug.scrub('{"password": 12345}')
    assert debug.scrub("max_tokens: 4096, keys: 3") == "max_tokens: 4096, keys: 3"
    live = "sk_live" + "_51Habc"
    out = debug.scrub('{"api_keys": ["' + live + '", "x"], "cookies": {"sid": "' + live + '"}, "n": 1}')
    assert live not in out and '"api_keys": "<redacted>"' in out and '"n": 1' in out
    quoted = "opaque-" + "Value9"
    assert debug.scrub(f'client_secret: "{quoted}"') == 'client_secret: "<redacted>"'
    assert quoted not in debug.scrub(f"X-API-Key: '{quoted}'")
    assert '"api_key": "<redacted>"' in debug.scrub('{"api_key": "x1y2z3w4"}')  # quoted rule unchanged


def test_scrub_catches_query_secrets_in_any_line():
    line = 'GET /api/oauth/callback?code=abc123&state=xyz&page=2&api_key=k HTTP/1.1" 303'
    out = debug.scrub(line)
    assert "abc123" not in out and "xyz" not in out and "=k " not in out and "page=2" in out
    form = debug.scrub("client_secret=abc123&grant_type=authorization_code&code=xyz")
    assert "abc123" not in form and "xyz" not in form and "grant_type=authorization_code" in form
    asgi = "Started scope={'path': '/mcp', 'query_string': b'code=abc123&state=xyz&page=2'}"
    out = debug.scrub(asgi)
    assert "abc123" not in out and "xyz" not in out and "page=2" in out


def test_retained_log_records_are_bounded():
    ring = debug._Ring()
    ring.setFormatter(logging.Formatter("%(message)s"))
    ring.emit(logging.LogRecord("x", logging.DEBUG, __file__, 1, "y" * 100_000, None, None))
    assert len(debug.RECORDS[-1]) < 9000


def test_query_strings_are_scrubbed():
    out = debug.query_for_log(b"code=abc123&state=xyz&api_key=k&page=2&q=mcpe_" + b"z" * 20)
    assert out["code"] == "<redacted, 6 chars>" and out["state"] == "<redacted, 3 chars>"
    assert out["api_key"] == "<redacted, 1 chars>" and out["page"] == "2"
    assert "z" * 20 not in out["q"]
    assert debug.query_for_log(b"") is None


def test_handlers_installed_before_debug_mode_are_scrubbed():
    import io

    stream = io.StringIO()
    early = logging.getLogger("mcpelevator.test.early")
    early.addHandler(logging.StreamHandler(stream))
    early.propagate = False
    debug.configure_logging(keep=False)
    early.warning("token=%s", "mcpe_" + "q" * 30)
    assert "q" * 30 not in stream.getvalue()


def test_scrub_filter_covers_tracebacks_and_rpc_targets():
    import io

    stream = io.StringIO()
    early = logging.getLogger("mcpelevator.test.exc")
    early.addHandler(logging.StreamHandler(stream))
    early.propagate = False
    debug.configure_logging(keep=False)
    secret = "mcpe_" + "w" * 30
    try:
        raise RuntimeError(f"bad token {secret}")
    except RuntimeError:
        early.exception("failed")
    assert "w" * 30 not in stream.getvalue() and "RuntimeError" in stream.getvalue()
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "resources/read",
                       "params": {"uri": f"x://{secret}"}}).encode()
    assert "w" * 30 not in json.dumps(debug._rpc_summary(body))


def test_trace_keeps_only_a_prefix_of_a_large_response():
    import asyncio

    big = b"y" * 2_000_000

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": big})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        pass

    scope = {"type": "http", "method": "GET", "path": "/api/big", "headers": [], "query_string": b""}
    asyncio.run(debug.TraceMiddleware(app)(scope, receive, send))
    trace = debug.TRACES[-1]
    assert trace["bytes"] == 2_000_000 and len(trace["response_body"]) < 20000


def test_trace_logs_a_stream_by_whole_events(caplog):
    """A secret split across two stream chunks is scrubbed, and an event too long to
    hold is logged as a prefix without holding the rest."""
    import asyncio

    secret = "s3cr3t" + "Q" * 10
    chunks = [b'data: {"api_', ('key": "%s"}\r\n' % secret).encode(), b"\r\n",
              b"data: " + b"z" * 40_000, b"\r\n\r\ndata: tail\r\n\r\n"]

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/event-stream")]})
        for i, chunk in enumerate(chunks):
            await send({"type": "http.response.body", "body": chunk, "more_body": i < len(chunks) - 1})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        pass

    scope = {"type": "http", "method": "GET", "path": "/s/x/mcp", "headers": [], "query_string": b""}
    with caplog.at_level(logging.DEBUG, logger="app.debug"):
        asyncio.run(debug.TraceMiddleware(app)(scope, receive, send))
    events = [r.getMessage() for r in caplog.records if "<- event" in r.getMessage()]
    assert len(events) == 3 and secret not in "".join(events) and '"api_key": "<redacted>"' in events[0]
    assert "rest not logged" in events[1] and len(events[1]) < 6000 and "data: tail" in events[2]


def test_trace_reads_one_huge_stream_message_in_windows(caplog, monkeypatch):
    """One ASGI message holding a huge event and then a secret one: the big event is
    cut to a prefix, and the next event is still found and scrubbed."""
    import asyncio

    seen, pattern = [], debug._EVENT_END

    class Spy:  # records how much is split at once
        def split(self, data):
            seen.append(len(data))
            return pattern.split(data)

    monkeypatch.setattr(debug, "_EVENT_END", Spy())
    body = b"data: " + b"z" * 200_000 + b'\n\ndata: {"token": "s3cr3tQQQQ"}\n\n'

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/event-stream")]})
        await send({"type": "http.response.body", "body": body})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        pass

    scope = {"type": "http", "method": "GET", "path": "/s/x/mcp", "headers": [], "query_string": b""}
    with caplog.at_level(logging.DEBUG, logger="app.debug"):
        asyncio.run(debug.TraceMiddleware(app)(scope, receive, send))
    events = [r.getMessage() for r in caplog.records if "<- event" in r.getMessage()]
    assert max(seen) <= 2 * debug._BODY_LIMIT + 3  # never the whole message at once
    assert "rest not logged" in events[0] and '"token": "<redacted>"' in events[1]


def test_trace_records_the_body_the_app_reads_and_never_reads_ahead():
    import asyncio

    reads = []
    message = {"type": "http.request", "body": b'{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}'}

    async def refusing(scope, receive, send):  # like the Host guard: answers unread
        await send({"type": "http.response.start", "status": 403, "headers": []})
        await send({"type": "http.response.body", "body": b"no"})

    async def reading(scope, receive, send):
        await receive()
        await refusing(scope, receive, send)

    async def receive():
        reads.append(1)
        return message

    async def send(_):
        pass

    scope = {"type": "http", "method": "POST", "path": "/s/x/mcp", "headers": [], "query_string": b""}
    asyncio.run(debug.TraceMiddleware(refusing)(scope, receive, send))
    assert reads == [] and "body" not in debug.TRACES[-1]
    asyncio.run(debug.TraceMiddleware(reading)(scope, receive, send))
    assert debug.TRACES[-1]["rpc"] == ["tools/list"]


def test_trace_bounds_a_large_request_body():
    import asyncio

    seen = []

    async def app(scope, receive, send):
        total = 0
        while True:
            message = await receive()
            total += len(message.get("body", b""))
            if not message.get("more_body"):
                break
        seen.append(total)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    chunk = b"x" * 10000
    messages = [{"type": "http.request", "body": chunk, "more_body": i < 99} for i in range(100)]
    pulled = []

    async def receive():
        pulled.append(1)
        return messages[len(pulled) - 1]

    async def send(message):
        pass

    scope = {"type": "http", "method": "POST", "path": "/api/x", "headers": [], "query_string": b""}
    asyncio.run(debug.TraceMiddleware(app)(scope, receive, send))
    assert seen == [1_000_000]  # the app still gets the whole body
    trace = debug.TRACES[-1]
    assert trace["body"].endswith("…(more)") and len(trace["body"]) < 20000
    assert trace["request_bytes"] == 1_000_000  # counted past the cut


def test_trace_of_a_body_dripped_a_byte_at_a_time_stays_fast():
    import asyncio
    import time

    n = 40_000
    sent = iter(range(n))

    async def app(scope, receive, send):
        while (await receive()).get("more_body"):
            pass
        await send({"type": "http.response.start", "status": 200, "headers": []})
        for i in range(n):
            await send({"type": "http.response.body", "body": b"y", "more_body": i < n - 1})

    async def receive():
        i = next(sent)
        return {"type": "http.request", "body": b"x", "more_body": i < n - 1}

    async def send(message):
        pass

    scope = {"type": "http", "method": "POST", "path": "/api/x", "headers": [], "query_string": b""}
    started = time.perf_counter()
    asyncio.run(debug.TraceMiddleware(app)(scope, receive, send))
    assert time.perf_counter() - started < 2.0  # quadratic in the chunk count before
    assert debug.TRACES[-1]["request_bytes"] == n and debug.TRACES[-1]["bytes"] == n


def test_trace_records_an_error_after_the_last_body_message():
    import asyncio

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})
        raise RuntimeError("background task failed")

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        pass

    scope = {"type": "http", "method": "GET", "path": "/api/x", "headers": [], "query_string": b""}
    with pytest.raises(RuntimeError):
        asyncio.run(debug.TraceMiddleware(app)(scope, receive, send))
    assert debug.TRACES[-1]["exception"] is True and "disconnected" not in debug.TRACES[-1]


def test_scrub_filter_keeps_uvicorn_access_args():
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                               ("1.2.3.4:5", "GET", "/api/oauth/callback?code=abc123&page=2", "1.1", 200), None)
    debug._ScrubFilter().filter(record)
    assert isinstance(record.args, tuple) and record.args[4] == 200
    assert "abc123" not in record.args[2] and "page=2" in record.args[2]


def test_bundle_needs_an_admin_when_auth_is_enforced(monkeypatch):
    from sqlmodel import Session

    from app.db import get_engine
    from app.registry import settings as runtime_settings

    monkeypatch.setattr(get_settings(), "debug", True)
    with Session(get_engine()) as s:
        previous = runtime_settings.control_plane_auth(s)
        runtime_settings.write(s, {"control_plane_auth": "always"})
    try:
        with TestClient(create_app()) as client:
            assert client.get("/api/debug", headers=LOOPBACK).status_code == 401
    finally:
        with Session(get_engine()) as s:
            runtime_settings.write(s, {"control_plane_auth": previous})


def test_bundle_is_404_unless_debug(monkeypatch):
    monkeypatch.setattr(get_settings(), "debug", False)
    with TestClient(create_app()) as client:
        assert client.get("/api/debug", headers=LOOPBACK).status_code == 404


def test_debug_traces_requests_and_serves_the_bundle(monkeypatch):
    monkeypatch.setattr(get_settings(), "debug", True)
    debug.TRACES.clear()
    with TestClient(create_app()) as client:
        token = mint_token("control")
        r = client.post("/s/nope/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                        headers={**LOOPBACK, "authorization": f"Bearer {token}"})
        assert r.status_code == 404
        trace = next(t for t in debug.TRACES if t["path"] == "/s/nope/mcp")
        # Refused before its body was read, so the body is never waited on or logged.
        assert trace["status"] == 404 and "body" not in trace
        assert trace["response_body"] == "unknown server"
        assert token not in json.dumps(trace)

        minted = client.post("/api/tokens", json={"name": "x", "scope": "all"}, headers=LOOPBACK)
        trace = next(t for t in reversed(debug.TRACES) if t["path"] == "/api/tokens")
        assert minted.json()["token"] not in trace["response_body"]
        assert '"scope":"all"' in trace["body"]  # read by the route, so traced

        bundle = client.get("/api/debug", headers=LOOPBACK).json()
        assert {"versions", "config", "servers", "groups", "traces", "logs"} <= set(bundle)
        assert bundle["config"]["debug"] is True


def test_scrub_redacts_env_assignments_after_other_text():
    secret = "opaque-" + "Value9"
    out = debug.scrub(f"loading API_KEY={secret} from .env")
    assert secret not in out and "loading API_KEY=<redacted> from .env" == out
    assert debug.scrub("sampling max_tokens=4096") == "sampling max_tokens=4096"
    # A spaced "=" is code in a traceback line, left as written.
    line = 'token = request.headers.get("authorization")'
    assert debug.scrub(line) == line


def test_bridge_debug_mode_writes_upstream_logs_once(monkeypatch):
    import uvicorn

    from app.bridge import host

    monkeypatch.setenv("MCPE_BRIDGE_SPEC", "{}")
    monkeypatch.setenv("MCPE_BRIDGE_PORT", "1")
    monkeypatch.setattr(debug, "enabled", lambda: True)
    monkeypatch.setattr(debug, "configure_logging", lambda **_: None)
    monkeypatch.setattr(host, "build_proxy", lambda spec: None)
    monkeypatch.setattr(host, "build_app", lambda spec, proxy: None)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    before = list(host._UPSTREAM_LOG.handlers)
    host.main()
    # Only the root's scrubbed handler writes them; no second copy of each line.
    assert host._UPSTREAM_LOG.handlers == before


def test_scrub_redacts_presigned_url_signatures():
    sig, cred = "f00d" * 16, "AKIA" + "EXAMPLE7Q/20261023/us-east-1/s3/aws4_request"
    url = (f"GET https://b.s3.amazonaws.com/o?X-Amz-Credential={cred}&X-Amz-Signature={sig}"
           f"&X-Amz-Expires=300 and https://a.blob.core.windows.net/c/b?sv=2024&sig={sig}")
    out = debug.scrub(url)
    assert sig not in out and cred not in out and "X-Amz-Expires=300" in out and "sv=2024" in out
    logged = debug.query_for_log(f"X-Amz-Signature={sig}&sig={sig}&page=2".encode())
    assert sig not in str(logged) and logged["page"] == "2"
    # Only a query: a CORS header keeps its value.
    assert debug.fingerprint("access-control-allow-credentials", "true") == "true"


def test_uvicorn_cli_loggers_reach_the_ring(monkeypatch):
    import faulthandler
    import sys
    import threading

    root = logging.getLogger()
    access = logging.getLogger("uvicorn.access")
    # As uvicorn's CLI leaves them: own handler, not propagating.
    monkeypatch.setattr(root, "handlers", [h for h in root.handlers if not getattr(h, "_mcpe_debug", False)])
    monkeypatch.setattr(access, "handlers", [logging.StreamHandler()])
    monkeypatch.setattr(access, "propagate", False)
    monkeypatch.setattr(root, "level", root.level)
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    monkeypatch.setattr(faulthandler, "enable", lambda: None)
    debug.configure_logging(keep=True)
    access.info('127.0.0.1:1 - "GET /api/health-probe HTTP/1.1" 200')
    assert access.propagate and not access.handlers
    assert any("/api/health-probe" in line for line in debug.RECORDS)
