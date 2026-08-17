"""One credential channel per token request.

RFC 6749 §2.3 is explicit that a client "MUST NOT use more than one authentication
method in each request", and authorization servers enforce it. The MCP SDK breaks that
rule for clients registered as ``client_secret_basic``: it builds the token request body
first — ``client_id`` included — and only afterwards consults the auth method and adds an
``Authorization: Basic`` header, stripping just ``client_secret`` from the body
(``mcp.client.auth.oauth2``: ``_exchange_token_authorization_code`` /``_refresh_token``
→ ``prepare_token_auth``). The client id therefore travels in both channels at once, and
a server that enforces §2.3 refuses the exchange outright:

    {"error":"invalid_request",
     "error_description":"Client must not use multiple authentication methods"}

That is what Cloudflare returns, and it is not reachable only by unlucky configuration:
whichever auth method the authorization server assigns at Dynamic Client Registration is
the one we get, and Cloudflare assigns Basic.

The fix belongs at the request, not at registration. Asking DCR for
``client_secret_post`` instead would trade this failure for a worse one — RFC 7591 §3.2.2
lets a server reject client metadata it cannot honour, so a Basic-only provider would
stop being able to REGISTER at all, turning a broken exchange into a broken sign-in. Here
the credential simply stops being sent twice, which is correct for every provider: with
Basic in the header the client is already authenticated, and RFC 6749 §4.1.3 requires
``client_id`` in the body only for a client that is *not* authenticating.

Applied by both callers of the SDK provider, because both send token requests: the
control plane exchanges the authorization code, and the bridge refreshes on its own
afterwards. Fixing only the first would let a sign-in succeed and then fail at the first
refresh.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from urllib.parse import parse_qsl, urlencode

import httpx

_FORM = "application/x-www-form-urlencoded"


def strip_duplicate_client_id(request: httpx.Request) -> httpx.Request:
    """``request`` with ``client_id`` removed from the form body when the request is
    ALSO authenticating with an ``Authorization: Basic`` header. Anything else — no Basic
    header, a non-form body, no ``client_id`` in it — is returned untouched, so this can
    sit in front of every request the SDK emits without having to know which is which."""
    if not request.headers.get("Authorization", "").lower().startswith("basic "):
        return request
    if _FORM not in request.headers.get("Content-Type", ""):
        return request
    body = request.content.decode("utf-8", "replace")
    fields = parse_qsl(body, keep_blank_values=True)
    kept = [(k, v) for k, v in fields if k != "client_id"]
    if len(kept) == len(fields):
        return request  # nothing duplicated
    headers = dict(request.headers)
    headers.pop("content-length", None)  # httpx recomputes it for the new body
    return httpx.Request(
        request.method, request.url, headers=headers, content=urlencode(kept)
    )


class SingleChannelAuthMixin:
    """Mixin for ``OAuthClientProvider`` that enforces the one-channel rule on the way out.

    ``async_auth_flow`` is a generator that yields each request to httpx and is sent the
    response back, so the whole flow — discovery, registration, code exchange, refresh —
    passes through here. Requests are forwarded unchanged except for the one shape that
    violates RFC 6749 §2.3, and responses are handed back to the SDK untouched, so the
    SDK keeps owning every decision about what to send and what to do with the answer."""

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        flow = super().async_auth_flow(request)  # type: ignore[misc]
        response: httpx.Response | None = None
        while True:
            try:
                outgoing = (
                    await flow.asend(response) if response is not None else await anext(flow)
                )
            except StopAsyncIteration:
                return
            response = yield strip_duplicate_client_id(outgoing)
