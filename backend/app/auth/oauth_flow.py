"""Control-plane OAuth authorization-code flow for remote upstreams.

Getting the *first* set of tokens for an OAuth-protected upstream is interactive:
the operator has to sign in at the provider in a browser. That can't happen inside
the headless bridge subprocess, so it happens here, in the control plane, which has
a UI and a public callback URL.

We drive the MCP SDK's ``OAuthClientProvider`` — which already implements discovery
(RFC 8414 / SEP-985), Dynamic Client Registration, PKCE, the code exchange, and
RFC 8707 resource binding — rather than reimplementing OAuth by hand. The provider
expects a ``redirect_handler`` (given the authorization URL) and a
``callback_handler`` (returns the ``(code, state)`` from the redirect). A desktop
client blocks a local browser + loopback server between the two; we instead split
them across two HTTP requests:

* ``begin_authorization`` starts the provider in a background task and returns the
  authorization URL the moment the provider produces it — the SPA sends the browser
  there.
* The provider then parks in ``callback_handler`` until the upstream redirects the
  browser back to ``/api/oauth/callback``; ``complete_authorization`` feeds the
  ``(code, state)`` in, the background task finishes the exchange, and the tokens
  land in the shared :class:`~app.auth.oauth_store.ServerTokenStorage` file — where
  the bridge picks them up (and refreshes them) from then on.

State is single-process, in-memory (this is a single-worker uvicorn); a background
reaper drops entries the operator never completed.
"""

from __future__ import annotations

import asyncio
import heapq
import io
import logging
import re
import time
from typing import Optional
from urllib.parse import parse_qs, urlparse, urlsplit, urlunsplit

import httpx
from mcp.client.auth import OAuthClientProvider, OAuthRegistrationError, TokenStorage
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from pydantic import AnyHttpUrl

from app.auth.oauth_store import ServerTokenStorage
from app.runners.remote import DEFAULT_TRANSPORT, canonical_transport
from app.util import new_id

logger = logging.getLogger(__name__)

CLIENT_NAME = "mcpelevator"
# SEP-2207 (accepted 2026): an OAuth client that wants a refresh token keeps
# ``refresh_token`` in its grant_types (we do) AND requests the ``offline_access``
# scope. Most authorization servers only mint a refresh token when the client asks
# for offline access, so without this every remote-OAuth session lapses on the short
# access-token clock and the operator has to re-authenticate by hand.
OFFLINE_ACCESS = "offline_access"
# Seconds to obtain the authorization URL (metadata discovery + client registration).
_URL_TIMEOUT = 30.0
# Seconds the operator has to complete the browser sign-in before we give up.
_FLOW_TIMEOUT = 600.0

# A minimal MCP initialize call — just enough of a real request to make the upstream
# answer 401 and hand us the ``WWW-Authenticate`` that kicks off the OAuth handshake.
_INIT_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": CLIENT_NAME, "version": "oauth-setup"},
    },
}
_INIT_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "MCP-Protocol-Version": "2025-06-18",
}


class OAuthBeginError(RuntimeError):
    """A failure to START the interactive OAuth flow, carrying an operator-facing message
    and the HTTP status the API should answer with. Lets ``begin_authorization`` translate
    a raw SDK/provider error into something actionable before it reaches the route handler,
    instead of the route dumping the provider's raw JSON body as an opaque 502."""

    def __init__(self, message: str, *, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


# Keys whose VALUES must never reach a log line or an operator-facing message. Our own
# error strings are safe by construction, but a third-party one is not: the MCP SDK
# embeds the RAW provider response body in its error text (``OAuthTokenError`` carries
# the token-endpoint body, ``OAuthRegistrationError`` the registration body — which
# contains a freshly issued ``client_secret``). Those messages are now surfaced by
# design (WARNING logs, and the 502 detail from ``_classify_begin_error``), so they are
# scrubbed on the way out. Values are replaced, keys kept: the operator still sees the
# shape of the failure, just not the credential.
# Matched ANYWHERE in a name, not just at a word boundary: the credential families come
# with vendor prefixes (RFC 7592's ``registration_access_token``, ``initial_access_token``,
# and ``client_secret`` itself), and a leading ``\b`` cannot see them — ``_`` is a word
# character, so there is no boundary before the suffix. Prefixes are always still the same
# kind of credential, so matching the family is the safe reading.
_SECRET_KEYS_ANYWHERE = (
    "access_token",
    "refresh_token",
    "id_token",
    "secret",
    "password",
)
# Matched as WHOLE words only. ``code`` is the reason this split exists: allowing a prefix
# would scrub ``status_code`` and ``error_code``, which are diagnostics rather than
# credentials and must stay readable.
_SECRET_KEYS_WHOLE = (
    "code_verifier",
    "code",
    # Pydantic renders a rejected field as ``… input_value=['SUPER-TOKEN'], input_type=list``
    # with the FIELD NAME on an earlier line, so a key/value pattern anchored on
    # ``access_token`` can't reach it — and the SDK feeds exactly this into
    # ``OAuthTokenError`` when a provider returns a malformed token response. Treat the
    # echoed input as sensitive regardless of which field failed: it is provider data by
    # definition, and the diagnosis survives without it (the field name, the expected
    # type, and the actual type are all reported separately).
    "input_value",
)
# WHERE a value ends is decided by a scanner, not by the pattern. Expressing "a complete
# quoted string, or a balanced container, or a bare token" in a regex produced a steady
# drip of leaks — a prefix match beside a "<redacted>" that claimed to have covered it —
# because each shape needs state the pattern doesn't have: escapes inside quotes, a
# closing bracket inside a quoted element (``['SUPER]SECRET']``), an unterminated opener.
# The lazy container match that handled the first of those was also quadratic on repeated
# unterminated openers (~1s at 48 KB), which is a denial-of-service on a single-worker
# control plane fed by a remote party. A forward scan handles every shape in one linear
# pass, and the pattern is left doing only what patterns are good at: finding the key.
_SEPARATOR = r"(?P<sep>[\"']?[ \t\r\n]*+[=:][ \t\r\n]*+)"
# Requiring the separator right after the key is what spares names that merely START with
# one: code_challenge and authorization_endpoint are not credentials and stay readable.
#
# Whitespace runs in the separator use POSSESSIVE quantifiers (``*+``, Python 3.11+),
# which is what lets them be unbounded without being dangerous: a plain ``*`` before a
# character that can fail is polynomial in the length of a space run (CodeQL
# py/polynomial-redos), while a fixed bound like ``{0,4}`` is linear but WRONG, since JSON
# permits arbitrary whitespace and a provider writing five spaces would sail past the
# redaction entirely.


def _escapable(literal: str) -> str:
    """Pattern source matching ``literal`` spelled plainly OR with any of its characters
    written as a JSON ``\\uXXXX`` escape.

    JSON permits escapes in MEMBER NAMES, and the SDK embeds a provider's response body
    verbatim, so a body answering with ``{"\\u0061ccess_token": "LIVE"}`` is well-formed
    JSON that a literal-spelling pattern cannot see — it read as a non-secret field and
    the token went to the log untouched. Matching the escapes here rather than decoding
    the text first is what preserves the offsets the scanner splices on."""
    alternatives = []
    for char in literal:
        forms = [re.escape(char)]
        if char.isalpha():  # either case is the same letter to a case-insensitive match
            forms += [rf"\\u00{ord(char.lower()):02x}", rf"\\u00{ord(char.upper()):02x}"]
        else:
            forms.append(rf"\\u{ord(char):04x}")
        alternatives.append("(?:" + "|".join(forms) + ")")
    return "".join(alternatives)


# ``(?<!\w)`` rather than ``\b``: it means the same thing for a plainly spelled key, but
# an escaped one STARTS with a backslash, and ``\b`` between a preceding ``"`` and that
# ``\`` does not exist (neither is a word character) — so the whole-word keys would have
# been exempt from the escape handling above. The lookbehind asks the question that
# actually matters, "is this the tail of a longer name", and still spares status_code and
# error_code (both preceded by ``_``, escaped or not).
_NOT_AFTER_WORD = r"(?<![0-9A-Za-z_])"
_SECRET_KEY_RE = re.compile(
    "(?i)(?P<key>"
    + "|".join(_escapable(key) for key in _SECRET_KEYS_ANYWHERE)
    + "|"
    + _NOT_AFTER_WORD
    + "(?:"
    + "|".join(_escapable(key) for key in _SECRET_KEYS_WHOLE)
    + "))"
    + _SEPARATOR
)
# ``Authorization: Bearer <token>`` needs its own rule: the credential sits AFTER a scheme
# word, so a whitespace-terminated value would redact "Bearer" and leave the token itself
# in the clear. Its unquoted form therefore runs to end of line.
_AUTH_KEY_RE = re.compile(
    "(?i)" + _NOT_AFTER_WORD + "(?P<key>" + _escapable("authorization") + ")" + _SEPARATOR
)
_BARE_TERMINATORS = " \t\r\n,&)}]"
_CONTAINER_CLOSERS = {"[": "]", "{": "}"}


def _value_end(text: str, start: int, *, to_end_of_line: bool) -> int:
    """Index just past the value beginning at ``start`` — one forward pass, no backtracking.

    Quoted values run to their matching quote, honouring backslash escapes. Containers run
    to their balanced close, ignoring brackets that sit inside quoted elements. An
    unterminated quote or container consumes the REST OF THE INPUT: we cannot know where
    such a value ends, and masking only part of it would leave the remainder in the clear
    (log_safe later folds newlines, so "just this line" is no protection either). Bare
    values stop at a delimiter — or at end of line for ``Authorization``, whose value is a
    scheme word followed by the credential."""
    n = len(text)
    if start >= n:
        return start
    opener = text[start]
    if opener in "\"'":
        i = start + 1
        while i < n:
            if text[i] == "\\":
                i += 2
                continue
            if text[i] == opener:
                return i + 1
            i += 1
        return n  # unterminated
    if opener in "[{":
        # A STACK of expected closers, not a depth counter: counting treats ``}`` as
        # closing a ``[``, so ``access_token=[SUPER}SECRET]`` ended at the ``}`` and left
        # ``SECRET]`` in the clear. A mismatch means the text is malformed, and malformed
        # is exactly the case where we cannot know where the value ends — so it falls in
        # with the unterminated openers below and consumes the rest.
        expected: list[str] = []
        quote = None
        i = start
        while i < n:
            char = text[i]
            if quote is not None:
                if char == "\\":
                    i += 2
                    continue
                if char == quote:
                    quote = None
            elif char in "\"'":
                quote = char
            elif char in _CONTAINER_CLOSERS:
                expected.append(_CONTAINER_CLOSERS[char])
            elif char in "]}":
                if char != expected[-1]:  # non-empty: ``start`` was itself an opener
                    return n  # mismatched
                expected.pop()
                if not expected:
                    return i + 1
            i += 1
        return n  # unterminated
    stop = "\r\n" if to_end_of_line else _BARE_TERMINATORS
    i = start
    while i < n and text[i] not in stop:
        i += 1
    return i


def _redacted_value(text: str, start: int, end: int) -> str:
    """``<redacted>`` wearing the same quotes the original value wore, so the surrounding
    JSON or query string still reads as the shape it was. An unterminated quote keeps its
    lone opener rather than gaining a closer we invented — the text was malformed and
    should still look it."""
    if start >= len(text):
        return "<redacted>"
    opener = text[start]
    if opener in "\"'":
        closed = end > start + 1 and text[end - 1] == opener
        return f"{opener}<redacted>{opener}" if closed else f"{opener}<redacted>"
    return "<redacted>"


def redact_secrets(value: object) -> str:
    """``str(value)`` with the values of credential-bearing keys replaced.

    Applied wherever an error that may carry third-party text becomes visible — a log
    record or an API error detail — so making OAuth failures debuggable never turns into
    writing tokens and client secrets to the console. Parsing (e.g.
    ``_registration_status``) still reads the RAW exception; only the surfaced copy is
    scrubbed.

    One left-to-right pass: each key match hands off to ``_value_end``, and the cursor
    jumps past whatever that value turned out to be. Total work is linear in the length
    of the text — a key inside an already-redacted value is skipped rather than rescanned,
    which is what keeps hostile input (repeated unterminated containers) cheap.

    Output is WRITTEN incrementally rather than collected. Accumulating a slice and a
    replacement per match into a list made a body densely packed with ``access_token=x``
    cost far more than the body itself (1 MB in, 7.2 MB peak), because the per-object
    overhead of ~140k short strings dwarfs their contents — and the party choosing that
    body is the remote one, in the single process serving all API and proxy traffic. A
    buffer holds one growing string instead."""
    text = str(value)
    out = io.StringIO()
    cursor = 0
    for key_match in _find_secret_keys(text):
        if key_match.start() < cursor:
            continue  # this key sat inside a value we already masked
        value_start = key_match.end()
        # Which PATTERN matched, not what the text says: an escaped spelling of the key
        # won't compare equal to "authorization", and only this pattern's values run to
        # end of line.
        is_auth_header = key_match.re is _AUTH_KEY_RE
        value_stop = _value_end(text, value_start, to_end_of_line=is_auth_header)
        if value_stop <= value_start:
            continue  # nothing to mask (e.g. the key ended the string)
        out.write(text[cursor:value_start])
        out.write(_redacted_value(text, value_start, value_stop))
        cursor = value_stop
    if cursor == 0:
        return text  # nothing matched — hand back the original rather than copying it
    out.write(text[cursor:])
    return out.getvalue()


def _find_secret_keys(text: str):
    """Credential-bearing key occurrences in positional order, from both patterns (the
    general key list and the ``Authorization`` header, whose value shape differs).

    Lazily merged, never materialized: this runs over provider response bodies of
    unbounded size, and collecting every match into a list to sort it would let a body
    densely packed with ``access_token=x`` amplify memory well beyond the response
    itself — in the single process that also serves every other control-plane and proxy
    request. Both iterators are already ordered, so merging them costs nothing extra."""
    return heapq.merge(
        _SECRET_KEY_RE.finditer(text),
        _AUTH_KEY_RE.finditer(text),
        key=lambda match: match.start(),
    )


# One log record per failure, however long or hostile the upstream text: newlines and
# control characters would let externally-supplied text forge additional log lines
# (CWE-117), and an unbounded body would drown the record it belongs to.
_LOG_TEXT_LIMIT = 500
# C0 controls and DEL, plus the Unicode separators NEL / LINE / PARAGRAPH: Python's own
# ``str.splitlines`` and plenty of line-oriented log tooling break on those too, so
# leaving them through would let provider text split one record into apparent forged
# ones — the very thing this pass exists to prevent.
_CONTROL_CHARS = re.compile("[\x00-\x1f\x7f\x85\u2028\u2029]")


def log_safe(value: object, *, limit: int = _LOG_TEXT_LIMIT) -> str:
    """``redact_secrets`` plus log-injection defence: credentials scrubbed, control
    characters folded to spaces, and the result truncated. Use this — not
    ``redact_secrets`` alone — for anything that reaches a log record and did not
    originate here.

    Redaction runs BEFORE truncation, over the WHOLE string — every cut is taken after
    masking, never before. Cutting first looks safer but isn't: a cut landing inside a
    quoted value leaves the quote unterminated, the complete-string branch then fails, and
    the bare branch masks only up to the first space — emitting
    ``client_secret: <redacted> beta gamma``, a mask sitting next to the tail it claims to
    have covered. That applies to ANY pre-cut, including a "generous" input ceiling: a
    credential long enough to straddle it would be split exactly the same way. The
    patterns are linear by construction (possessive quantifiers), so there is nothing to
    gain by capping the input first."""
    text = _CONTROL_CHARS.sub(" ", redact_secrets(value)).strip()
    return text if len(text) <= limit else f"{text[:limit]}… (truncated)"


class OAuthGrantRejected(RuntimeError):
    """The grant completed, but the UPSTREAM refused the token it produced (the retried
    MCP request came back 401/403). Distinct from a token-exchange failure: the provider
    issued a credential, the resource server won't accept it — typically a scope or
    ``resource`` mismatch, which is a configuration problem on this server rather than a
    transport or protocol fault. Typed so the callback can say which of the two happened
    instead of labelling every post-redirect failure the same way."""


class OAuthFlowCancelled(RuntimeError):
    """The flow was cancelled from outside while its callback was already waiting — a
    config edit, ownership transfer, disconnect, delete, a newer Authenticate click, or
    the sign-in window expiring. Typed so the waiter fails FAST and accurately: without
    it, cancelling the driving task leaves ``done_future`` unresolved, the callback burns
    its full budget, and the operator is told the exchange "timed out" when it was
    deliberately superseded.

    The causes are separated because they ask different things of an operator:
    ``superseded`` means the attempt was replaced or ran out of time (just sign in
    again), ``deleted`` means the server itself is gone (there is nothing left to sign
    in to), and neither means the configuration it belonged to changed (look at what
    changed first). They stay plain booleans so the reason-code vocabulary lives entirely
    in the API layer that owns it."""

    def __init__(self, message: str, *, superseded: bool = False, deleted: bool = False):
        super().__init__(message)
        self.superseded = superseded
        self.deleted = deleted


class OAuthPromotionBlocked(RuntimeError):
    """The grant was obtained but must NOT become this server's credential: the row was
    deleted, its owner changed, or its OAuth configuration changed while the operator was
    signing in (see ``_promotion_blocked``). Nothing is wrong with the grant — it just
    belongs to a configuration that no longer exists, so the remedy is to sign in again.

    ``deleted`` carries the one cause with a different remedy: when the row is gone there
    is no configuration to inspect and nothing to retry, so telling the operator to check
    what changed would send them after a server that no longer exists."""

    def __init__(self, message: str, *, deleted: bool = False):
        super().__init__(message)
        self.deleted = deleted


def _registration_status(exc: OAuthRegistrationError) -> Optional[int]:
    """Pull the HTTP status out of an ``OAuthRegistrationError``. The SDK bakes it into the
    message as ``"Registration failed: <status> <body>"`` (mcp.client.auth.utils) with no
    structured field, so the message is the only place to read it back from."""
    match = re.match(r"Registration failed: (\d{3})\b", str(exc))
    return int(match.group(1)) if match else None


def _classify_begin_error(exc: BaseException) -> OAuthBeginError:
    """Translate a raw begin-flow failure into an operator-facing :class:`OAuthBeginError`.

    Two actionable cases get a clean 4xx with next steps rather than a 502 carrying the
    provider's raw error body (which a fronting CDN like Cloudflare may even swallow and
    replace with its own 502 page, hiding the message entirely):

    * the provider has no Dynamic Client Registration endpoint at all (HTTP 404/405/501 —
      e.g. GitHub, whose MCP server requires a pre-registered OAuth App) or refuses
      anonymous registration by policy (HTTP 401/403, e.g. an endpoint requiring an
      initial access token we don't hold — the remedy is the same), and
    * the provider rate-limiting registration (HTTP 429).

    Everything else keeps the previous generic message + 502."""
    if isinstance(exc, OAuthBeginError):
        return exc
    status = _registration_status(exc) if isinstance(exc, OAuthRegistrationError) else None
    if status in (401, 403, 404, 405, 501):
        return OAuthBeginError(
            "the OAuth provider does not support Dynamic Client Registration "
            f"(HTTP {status} from its registration endpoint), so it requires a pre-registered "
            "client. Register an app with the provider (for GitHub: an OAuth App whose "
            "authorization callback URL is this mcpelevator's /api/oauth/callback), then set "
            "its Client ID and Client Secret on this server and connect again.",
            status_code=400,
        )
    if status == 429:
        return OAuthBeginError(
            "the OAuth provider is rate-limiting Dynamic Client Registration (HTTP 429). "
            "Wait a minute and try connecting again; if it keeps happening, register a client "
            "with the provider and set an explicit Client ID to skip registration.",
            status_code=429,
        )
    # The provider's raw error body can ride along in ``exc`` (see ``redact_secrets``) and
    # this message becomes an API error detail the SPA displays — scrub it too.
    return OAuthBeginError(f"could not start OAuth: {redact_secrets(exc)}", status_code=502)


def _merge_scopes(*scope_strings: Optional[str]) -> Optional[str]:
    """Union space-delimited scope strings, preserving first-seen order and dropping
    duplicates. Returns ``None`` when nothing was supplied (so the SDK omits ``scope``)."""
    merged = dict.fromkeys(s for ss in scope_strings for s in (ss or "").split())
    return " ".join(merged) if merged else None


def _offline_access_default(context) -> bool:
    """Whether to add ``offline_access`` (→ refresh token) to the requested scope.

    SEP-2207: request it by default, UNLESS the authorization server publishes a
    ``scopes_supported`` list that omits it. That exception respects an AS which
    validates scopes strictly — an unadvertised ``offline_access`` would otherwise get
    the whole authorization rejected with ``invalid_scope``. When the AS advertises it,
    or publishes no scope list at all, we ask, so the common case (short-lived access
    token + long-lived refresh token) works with no operator action. An operator whose
    provider honours ``offline_access`` without advertising it can still type it into
    the scopes field — operator scopes are always requested, gate or no gate.
    """
    metadata = context.oauth_metadata
    supported = metadata.scopes_supported if metadata else None
    if supported is None:
        return True
    return OFFLINE_ACCESS in supported


class _ScopedOAuthClientProvider(OAuthClientProvider):
    """``OAuthClientProvider`` that requests a refresh token and keeps the operator's scopes.

    The SDK runs its own "scope selection strategy" during the 401-driven handshake and
    OVERWRITES ``context.client_metadata.scope`` from the WWW-Authenticate header / the
    discovered resource+auth-server metadata (``oauth2.get_client_metadata_scopes``),
    discarding whatever the operator typed. That's wrong when the operator deliberately
    asked for a specific set (e.g. an upstream that doesn't advertise scopes, or one
    that needs a scope it omits from the challenge). Immediately before the authorization
    URL is built we union back in, so they're always requested while still honouring any
    the server volunteered: (1) ``offline_access`` by default (SEP-2207 — see
    ``_offline_access_default``), so the provider issues a refresh token and the session
    doesn't lapse on the access-token clock, and (2) the operator's explicit scopes."""

    def __init__(self, *args, operator_scopes: Optional[str] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._operator_scopes = operator_scopes or None

    async def _perform_authorization_code_grant(self) -> tuple[str, str]:
        extra = self._operator_scopes
        if _offline_access_default(self.context):
            extra = _merge_scopes(extra, OFFLINE_ACCESS)
        if extra:
            self.context.client_metadata.scope = _merge_scopes(
                self.context.client_metadata.scope, extra
            )
        return await super()._perform_authorization_code_grant()


class _MemoryTokenStorage(TokenStorage):
    """Ephemeral, in-process token storage used to DRIVE one interactive grant.

    The flow runs against this instead of the shared file store so that an existing,
    working credential is never destroyed by a re-authorization that then fails or is
    cancelled: the real store is written only on success (see ``_drive``'s promotion).
    Seeded with the existing client info so a re-auth reuses the registered client and
    a static-client grant skips Dynamic Client Registration. Also forces the probe to
    401 (no tokens here), which is what triggers the browser redirect."""

    def __init__(
        self,
        client_info: Optional[OAuthClientInformationFull] = None,
        *,
        persist_registration_to: Optional[ServerTokenStorage] = None,
        persist_allowed=None,
    ):
        self._tokens: Optional[OAuthToken] = None
        self._client_info = client_info
        # When set, a client the SDK newly REGISTERS mid-flow is written straight through to
        # the shared store (client_info only, never tokens). Wired up by begin_authorization
        # solely in the DCR path when the real store holds no tokens, so an abandoned or failed
        # browser step doesn't discard the registration and force the next sign-in to register
        # again (burning the provider's registration quota).
        self._persist_registration_to = persist_registration_to
        # Zero-arg predicate consulted at write time: this pass-through fires
        # MID-FLOW, before the promotion-time deletion check, so without it a
        # server deleted during the flow's early awaits would get its credential
        # file recreated with the newly registered client (secret included).
        self._persist_allowed = persist_allowed

    async def get_tokens(self) -> Optional[OAuthToken]:
        return self._tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._tokens = tokens

    async def get_client_info(self) -> Optional[OAuthClientInformationFull]:
        return self._client_info

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self._client_info = client_info
        if self._persist_registration_to is not None and (
            self._persist_allowed is None or self._persist_allowed()
        ):
            # set_client_info touches only the client_info key of the file, leaving any tokens
            # untouched — and this is only wired when there were no tokens to begin with, so it
            # can never rebind a client that a live credential's refresh depends on.
            await self._persist_registration_to.set_client_info(client_info)


class _Pending:
    """One in-flight authorization the operator has started but not yet completed.

    ``owner_id`` snapshots the server's owner at the moment the flow began: the
    grant belongs to whoever started the sign-in, so promotion re-checks that the
    server still has that owner (see ``_drive``). Cancellation on reassignment
    alone can't cover this — a transfer can land while ``begin_authorization`` is
    still awaiting upstream discovery, BEFORE the flow registers in ``_PENDING``,
    where there is nothing to cancel yet."""

    def __init__(
        self,
        server_id: str,
        owner_id: Optional[str] = None,
        row_existed: bool = False,
        oauth_sig: tuple = (),
    ):
        loop = asyncio.get_running_loop()
        self.id = new_id()
        self.server_id = server_id
        self.owner_id = owner_id
        # The OAuth-relevant config (upstream/scopes/client) the flow was started
        # against — promotion re-judges it, so a grant obtained for the OLD config
        # can't become the credential of a mid-flow-reconfigured server.
        self.oauth_sig = oauth_sig
        # Did the server ROW exist in the DB when the flow began? Promotion treats
        # a now-missing row as "deleted mid-flow" (blocked) only when it did — a
        # flow driven against an unpersisted server object (tests) is exempt.
        self.row_existed = row_existed
        self.state: Optional[str] = None  # OAuth ``state``, learned from the auth URL
        self.code: Optional[str] = None  # filled in by the callback
        self.url_future: asyncio.Future[str] = loop.create_future()
        self.done_future: asyncio.Future[None] = loop.create_future()
        self.callback_event = asyncio.Event()
        self.task: Optional[asyncio.Task] = None
        self.created_at = time.monotonic()


# id -> pending, plus a state -> id index (state is only known once the provider
# yields the authorization URL, i.e. after discovery/registration succeed).
_PENDING: dict[str, _Pending] = {}
_STATE_INDEX: dict[str, str] = {}


def _forget(pending: _Pending) -> None:
    _PENDING.pop(pending.id, None)
    if pending.state is not None:
        _STATE_INDEX.pop(pending.state, None)
    if pending.task is not None and not pending.task.done():
        pending.task.cancel()


def _abort(
    pending: _Pending, why: str, *, superseded: bool = False, deleted: bool = False
) -> None:
    """Forget a flow that something EXTERNAL ended, waking anyone parked on it.

    ``_forget`` alone cancels the driving task, which never resolves ``done_future`` —
    so a callback already waiting there would sit for its full budget and then report a
    timeout for what was actually a deliberate cancellation. The exception is set only
    when the callback has actually arrived (``callback_event``): with no one to retrieve
    it, a future's exception surfaces as a spurious "never retrieved" at collection, so
    an unattended flow is cancelled silently instead."""
    if not pending.done_future.done():
        if pending.callback_event.is_set():
            pending.done_future.set_exception(
                OAuthFlowCancelled(why, superseded=superseded, deleted=deleted)
            )
        else:
            pending.done_future.cancel()
    _forget(pending)


def _reap_stale() -> None:
    now = time.monotonic()
    for pending in list(_PENDING.values()):
        if now - pending.created_at > _FLOW_TIMEOUT:
            # Expiry, not reconfiguration: the remedy is simply to start again.
            _abort(
                pending,
                "the sign-in window expired before it completed",
                superseded=True,
            )


def _cancel_existing(
    server_id: str, why: str, *, superseded: bool, deleted: bool = False
) -> None:
    """Only one authorization can be in flight per server — a second click supersedes
    the first (its state/PKCE would otherwise dangle until it reaps). Callers say WHY,
    because "you clicked Authenticate again" and "this server was reconfigured" need
    different things from the operator."""
    for pending in list(_PENDING.values()):
        if pending.server_id == server_id:
            _abort(pending, why, superseded=superseded, deleted=deleted)


def pending_server_id(state: str) -> Optional[str]:
    """The server id of the authorization parked on ``state``, or ``None`` — so the
    callback can stop that server's running bridge *before* the grant is promoted,
    closing the window where an old bridge's refresh could overwrite the new tokens."""
    pending_id = _STATE_INDEX.get(state)
    pending = _PENDING.get(pending_id) if pending_id else None
    return pending.server_id if pending is not None else None


def cancel_pending(
    server_id: str, *, superseded: bool = False, deleted: bool = False
) -> None:
    """Cancel any in-flight authorization for a server. Called when its OAuth config is
    edited: a background flow started against the OLD upstream/scopes/client must not
    complete and write credentials for the wrong resource back under this id. Also used
    when a provider denial ends a flow — ``superseded=True`` there, since nothing about
    the server's configuration changed — and by the DELETE path, where ``deleted=True``
    keeps the operator from being sent to inspect a server that no longer exists."""
    if deleted:
        why = "the server was deleted during the sign-in"
    elif superseded:
        why = "the sign-in ended before it could complete"
    else:
        why = "the server's OAuth configuration or ownership changed during the sign-in"
    _cancel_existing(server_id, why, superseded=superseded, deleted=deleted)


def _repair_authorization_url(url: str) -> str:
    """Fix authorization URLs the SDK builds for endpoints that already carry a query.

    The SDK joins its parameters as ``f"{auth_endpoint}?{urlencode(params)}"`` — but some
    providers advertise an ``authorization_endpoint`` that itself contains a query string
    (Railway: ``/oauth/auth?resource=https%3A%2F%2Fbackboard.railway.com``). The blind join
    yields a second raw ``?``, and everything after it — ``response_type=code`` first —
    is swallowed into the preceding parameter's value, so the provider rejects the request.

    RFC 3986 permits raw ``?`` inside a query, so the endpoint's own query may legally
    contain more of them — but ``urlencode`` percent-encodes ``?``, so in the joined URL
    the LAST raw ``?`` is always the separator the SDK added. Re-join only that one with
    ``&``, leaving the endpoint's own query byte-for-byte intact."""
    base, sep, params = url.rpartition("?")
    if not sep or "?" not in base:
        return url  # zero or one '?' — already well-formed
    return f"{base}&{params}"


def _ensure_consent_prompt(url: str) -> str:
    """Add ``prompt=consent`` to an authorization URL that requests ``offline_access``.

    OIDC providers only (re)issue a refresh token for offline access when the user actively
    consents, which the spec ties to ``prompt=consent``. Without it a returning, already-
    consented user gets an authorization code but NO refresh token — the very lapse
    requesting ``offline_access`` is meant to avoid (SEP-2207). The SDK's URL builder
    serializes only the standard params plus ``scope``, so we append it here.

    Skipped when the URL already carries a ``prompt`` (don't clobber a provider-specific
    value) or doesn't request offline access. A non-OIDC OAuth2 server simply ignores the
    unknown parameter (RFC 6749 §3.1), so sending it for offline-access requests is safe.
    The existing query is preserved byte-for-byte (see ``_repair_authorization_url``) — only
    the new parameter is appended — and any parse hiccup on an exotic URL just leaves it
    unchanged rather than risking corruption."""
    parts = urlsplit(url)
    try:
        query = parse_qs(parts.query, keep_blank_values=True)
    except ValueError:
        return url
    scope = " ".join(query.get("scope", [])).split()
    if OFFLINE_ACCESS not in scope or query.get("prompt"):
        return url
    # Reached only with offline_access in the scope param, so the query is non-empty;
    # "prompt=consent" needs no escaping, so append it directly.
    new_query = f"{parts.query}&prompt=consent"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


def _client_metadata_url(callback_url: str) -> Optional[str]:
    """The CIMD client-metadata URL to offer alongside the flow, derived from the
    callback URL so both always share one base (the document lists that same callback
    as its redirect_uri — a mismatch would fail the provider's validation). ``None``
    unless the base is https with the expected callback path: CIMD client ids must be
    https URLs the provider can fetch server-side, so a LAN/plain-http instance simply
    doesn't offer one. Offering it is always safe — the SDK only uses it when the
    authorization server advertises ``client_id_metadata_document_supported``, and
    falls back to Dynamic Client Registration otherwise; a static client id (seeded
    client info) bypasses both."""
    suffix = "/api/oauth/callback"
    if not callback_url.startswith("https://") or not callback_url.endswith(suffix):
        return None
    return callback_url.removesuffix(suffix) + "/api/oauth/client-metadata.json"


# Budget for the CIMD self-probe below. Providers fetch the document with a short
# server-side timeout themselves, so a document that takes longer than this is as
# good as unreachable for CIMD purposes anyway.
_CIMD_PROBE_TIMEOUT = 8.0
# The real document is a small JSON object; anything past this bound is not it. A
# truncated over-limit body fails the json parse below and reads as a definitive
# non-document, so a misbehaving proxy can't balloon memory through the probe.
_CIMD_PROBE_MAX_BYTES = 65536


async def _fetch_client_metadata(url: str) -> httpx.Response:
    """One unauthenticated GET of our own client-metadata URL — no cookies, no bearer,
    no redirects — exactly the fetch a CIMD-supporting authorization server performs.
    Bounded twice over: ``asyncio.wait_for`` caps the WHOLE request wall-clock (httpx
    timeouts are per network operation, so a response trickling in under-timeout chunks
    could otherwise hold /oauth/authorize open indefinitely), and the body read stops
    past ``_CIMD_PROBE_MAX_BYTES``.

    The wall-clock deadline shares its budget with httpx's connect timeout and can win
    that race, so a bare deadline expiry is ambiguous. Disambiguate for the caller by
    whether response HEADERS had arrived when it fired: before headers, re-raise as
    ``ConnectTimeout`` — indistinguishable from a hairpin blackhole, so inconclusive —
    and only after headers as ``TimeoutError``, an established-but-undeliverable
    response the caller treats as disqualifying."""
    got_headers = False

    async def _get() -> httpx.Response:
        nonlocal got_headers
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_CIMD_PROBE_TIMEOUT), follow_redirects=False
        ) as client:
            # Accept-Encoding: identity + aiter_raw: the bound must hold on the bytes
            # actually allocated. aiter_bytes DECODES each raw chunk before yielding —
            # a small gzip body can expand ~1000x into one materialized bytes object
            # before any slice runs — so read the raw wire bytes with no decoding and
            # ask the server not to compress. A proxy that compresses anyway despite
            # identity yields a body that fails the JSON parse and reads as a
            # definitive non-document, which is the safe outcome.
            async with client.stream(
                "GET",
                url,
                headers={"Accept": "application/json", "Accept-Encoding": "identity"},
            ) as response:
                got_headers = True
                # Slice each chunk to the remaining budget BEFORE retaining it.
                body = bytearray()
                async for chunk in response.aiter_raw():
                    body.extend(chunk[: _CIMD_PROBE_MAX_BYTES + 1 - len(body)])
                    if len(body) > _CIMD_PROBE_MAX_BYTES:
                        break
                return httpx.Response(response.status_code, content=bytes(body))

    try:
        return await asyncio.wait_for(_get(), timeout=_CIMD_PROBE_TIMEOUT)
    except TimeoutError:
        if got_headers:
            raise
        raise httpx.ConnectTimeout(
            f"no response headers within {_CIMD_PROBE_TIMEOUT:.0f}s"
        ) from None


# Conclusive probe verdicts, cached per URL for a few minutes: every server on an
# instance shares ONE metadata URL, so authenticating several servers (or re-auths
# after token expiry) shouldn't re-pay the probe round-trip each time. Inconclusive
# answers (transport errors, transient statuses) are never cached — the offer stands
# and the next sign-in probes again.
_PROBE_TTL = 300.0
_PROBE_CACHE: dict[str, tuple[Optional[str], float]] = {}
# Statuses that prove nothing about gating: the endpoint (or its fronting proxy) is
# rate-limiting or transiently failing, and the provider's own fetch moments later
# may well succeed. Everything else non-200 — 401/403, login redirects, 404 — is the
# stable shape of an auth gate or a broken path, which withholds the offer.
_PROBE_TRANSIENT_STATUSES = {408, 429}


async def _reachable_client_metadata_url(url: str) -> Optional[str]:
    """``url`` back to offer it as the CIMD client id, or ``None`` to fall back to DCR.

    A self-probe of the document as the authorization server would fetch it. An https
    base alone doesn't prove the document is PUBLICLY fetchable: an instance behind an
    auth-gating proxy (Cloudflare Access, an oauth2-proxy, HTTP basic auth) serves it
    fine to the operator's signed-in browser while answering the provider's
    unauthenticated server-side fetch with a 401/403 or a login redirect — and that
    failure surfaces only AFTER the browser has been sent to the provider, as an opaque
    "client metadata unavailable" page there. Withholding the URL keeps the sign-in on
    Dynamic Client Registration, which needs no inbound fetch at all.

    Only a DEFINITIVE bad answer — an HTTP response in the stable shape of a gate or a
    body that isn't the public document with the matching ``client_id`` — withholds the
    offer, with one addition: a connection that IS established but can't deliver the
    document within the budget (stalled/trickling response) also withholds, uncached —
    the provider's own server-side fetch would exhaust its deadline the same way.
    Inconclusive evidence keeps the offer: a CONNECTION failure proves nothing (plenty
    of deployments can't hairpin their own public hostname from inside the container
    while the provider reaches it fine), and neither does a transient status
    (429/5xx/408)."""
    cached = _PROBE_CACHE.get(url)
    if cached is not None and time.monotonic() < cached[1]:
        return cached[0]
    try:
        response = await _fetch_client_metadata(url)
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        # Couldn't even open a connection from inside the container — the classic
        # no-hairpin-route shape. Not evidence about the provider's path: offer.
        logger.debug(
            "CIMD self-probe of %s could not connect (%s); offering the URL anyway", url, exc
        )
        return url
    except (TimeoutError, httpx.TimeoutException) as exc:
        # The connection was established (ConnectTimeout is excluded above) but the
        # document couldn't be delivered inside the provider-compatible budget — a
        # stalled read or a body trickling past the wall clock. A provider's fetch
        # would fail the same way, so withhold; possibly transient, so don't cache.
        logger.warning(
            "the client-metadata document at %s could not be delivered within %.0fs (%s) — "
            "falling back to Dynamic Client Registration for this sign-in.",
            url,
            _CIMD_PROBE_TIMEOUT,
            type(exc).__name__,
        )
        return None
    except Exception as exc:  # noqa: BLE001 — other transport-level failure: not evidence
        logger.debug("CIMD self-probe of %s errored (%s); offering the URL anyway", url, exc)
        return url
    status = response.status_code
    if status in _PROBE_TRANSIENT_STATUSES or status >= 500:
        logger.debug(
            "CIMD self-probe of %s got transient HTTP %s; offering the URL anyway", url, status
        )
        return url
    document = None
    if status == 200:
        try:
            document = response.json()
        except ValueError:
            document = None
    verdict = url if isinstance(document, dict) and document.get("client_id") == url else None
    if verdict is None:
        logger.warning(
            "the client-metadata document at %s is not publicly fetchable as-is "
            "(HTTP %s%s) — likely an auth-gating proxy (e.g. Cloudflare Access) in front of "
            "this instance. Falling back to Dynamic Client Registration for this sign-in; "
            "to use URL-based client ids, exempt /api/oauth/client-metadata.json from the "
            "gate (the document is public by design and carries no secrets), or set the "
            "'Upstream OAuth client identity' setting to make the choice explicit.",
            url,
            status,
            "" if status != 200 else ", body is not this instance's document",
        )
    _PROBE_CACHE[url] = (verdict, time.monotonic() + _PROBE_TTL)
    return verdict


def _upstream_client_mode() -> str:
    """The ``upstream_oauth_client_mode`` runtime setting, read on a fresh session."""
    from sqlmodel import Session

    from app.db import get_engine
    from app.registry import settings as runtime_settings

    with Session(get_engine()) as session:
        return runtime_settings.upstream_oauth_client_mode(session)


def _effective_client_mode(server) -> str:
    """The client-identity mode governing this server's sign-in: the server's own
    ``oauth_client_mode`` when it picked one, else the instance-wide runtime setting.
    Blank/legacy values ('' from the ADD COLUMN migration) read as inherit."""
    mode = (getattr(server, "oauth_client_mode", "") or "").strip()
    if mode in ("auto", "cimd", "dcr"):
        return mode
    return _upstream_client_mode()


async def _decide_client_metadata_url(callback_url: str, mode: str) -> Optional[str]:
    """What to hand the provider as ``client_metadata_url`` for an UNSEEDED flow,
    honouring the effective client-identity mode: ``dcr`` never offers the URL client
    id, ``cimd`` always offers the derived URL probe-free (the operator knows their
    document is reachable even if this container can't see it), and ``auto`` (default)
    offers it only when the self-probe confirms it's fetchable."""
    if mode == "dcr":
        return None
    url = _client_metadata_url(callback_url)
    if url is None or mode == "cimd":
        return url
    return await _reachable_client_metadata_url(url)


def _extract_state(url: str) -> Optional[str]:
    try:
        values = parse_qs(urlparse(url).query).get("state")
    except ValueError:
        return None
    return values[0] if values else None


def _probe_headers(server) -> dict[str, str]:
    """Headers for the probe request: the base MCP headers plus the server's own extra
    headers — but NEVER a stale ``Authorization``. If the server was switched from static
    Headers auth to OAuth, a leftover token header would make the upstream answer 200
    instead of the 401 that drives the OAuth flow, hanging begin() into a timeout."""
    headers = dict(_INIT_HEADERS)
    for key, value in (server.env or {}).items():
        if key.strip().lower() == "authorization":
            continue
        headers[key] = value
    return headers


def _server_row(server_id: str):
    """The server's COMMITTED row (or None), read on a fresh session against the
    engine (never a request session) so no identity map interferes. Plain columns
    are loaded eagerly, so the returned object is safe to inspect after close."""
    from sqlmodel import Session

    from app.db import get_engine, repo

    with Session(get_engine()) as session:
        return repo.get_server(session, server_id)


def _oauth_signature_of(server) -> tuple:
    """The OAuth-relevant configuration of a server(-like) object — the same shape
    the PATCH handler uses to decide token cleanup. A grant is only valid for the
    exact upstream/scopes/client the flow was STARTED against."""
    return (
        bool(getattr(server, "oauth", False)),
        getattr(server, "command", ""),  # upstream URL — tokens bind to this resource
        getattr(server, "oauth_scopes", "") or "",
        getattr(server, "oauth_client_id", None),
        getattr(server, "oauth_client_secret", None),
    )


def _promotion_blocked(pending: _Pending) -> Optional[OAuthPromotionBlocked]:
    """Why this flow's grant must NOT be promoted (None = go ahead), judged against
    the committed row at promotion time. Returns the typed exception rather than a bare
    string so the DELETION cause survives to the callback: a deleted row and a changed
    config need different things said to the operator, and re-deriving that by matching
    on message text would be a rule in two places. Registered pending flows are cancelled by
    the delete/reassign/config-edit paths, but a flow still in its pre-registration
    awaits escapes all of them — these checks are the backstop:

    - a row that vanished (and existed at begin) means the server was DELETED
      mid-flow; promoting would recreate an orphaned credential file;
    - an owner that changed means the grant belongs to the FORMER owner's
      upstream account;
    - an OAuth config that changed (upstream/scopes/client) means the PATCH that
      changed it already cleared the stored tokens — this grant belongs to the
      OLD configuration and must not become the new upstream's credential.

    A row that never existed (``row_existed`` False — unpersisted test servers)
    is exempt from the deletion check and carries no config to re-judge."""
    row = _server_row(pending.server_id)
    if row is None:
        if pending.row_existed:
            return OAuthPromotionBlocked(
                "server was deleted during authorization", deleted=True
            )
        return None
    if row.owner_id != pending.owner_id:
        return OAuthPromotionBlocked(
            "server ownership changed during authorization — sign in again"
        )
    if _oauth_signature_of(row) != pending.oauth_sig:
        return OAuthPromotionBlocked(
            "server OAuth configuration changed during authorization — sign in again"
        )
    return None


async def _drive(
    server,
    provider: OAuthClientProvider,
    mem: _MemoryTokenStorage,
    real: ServerTokenStorage,
    pending: _Pending,
) -> None:
    """Run the provider end to end: one authenticated request that 401s, triggering
    discovery → registration → (park for browser) → code exchange. Tokens land in the
    ephemeral ``mem`` store; only on success are they PROMOTED to the shared ``real``
    store, so a failed re-auth never destroys a still-working credential."""
    mcp_url = server.command
    # Probe with the SAME transport the bridge will use so an SSE upstream reaches its
    # 401/auth challenge here instead of failing before the redirect.
    transport = canonical_transport((server.args or [None])[0]) or DEFAULT_TRANSPORT
    headers = _probe_headers(server)
    method, kwargs = ("GET", {}) if transport == "sse" else ("POST", {"json": _INIT_BODY})
    inner_error: Optional[BaseException] = None
    final_status: Optional[int] = None
    try:
        async with httpx.AsyncClient(
            auth=provider, timeout=httpx.Timeout(30.0), follow_redirects=True
        ) as client:
            # STREAM (don't read the body): the OAuth handshake runs to completion before this
            # final response is returned, so the body is irrelevant — and an SSE upstream keeps
            # its ``text/event-stream`` response open (heartbeats), which a body-reading
            # ``get()`` would block on forever, hanging the flow past its timeout. We DO read the
            # status line (available once headers arrive, without touching the body) to tell a
            # genuinely usable grant from one the resource still rejects.
            async with client.stream(method, mcp_url, headers=headers, **kwargs) as response:
                final_status = response.status_code
    except asyncio.CancelledError:
        raise
    except BaseException as exc:  # noqa: BLE001 — tolerate; success is decided by whether tokens landed
        inner_error = exc

    # The exchange stores tokens *before* the original request is retried, so the
    # handshake can succeed even if that retry (or the connection) then errors. Judge
    # success on whether tokens actually landed in the ephemeral store.
    tokens = await mem.get_tokens()
    # ...but if the RETRIED MCP request still came back 401/403, the resource rejected the new
    # token (bound to the wrong resource, missing a required scope, etc.). Promoting it would
    # leave the UI reporting "connected" and restart the bridge with a credential the upstream
    # refuses, so treat that as an authorization failure instead of a success.
    if tokens is not None and final_status in (401, 403):
        # Overrides, never defers (same reasoning as the promotion block below): the
        # status line is read once headers arrive, so an exception can still surface
        # afterwards from closing the stream. That incidental transport error would
        # otherwise win and report the failure as unclassified, when the resource has in
        # fact explicitly refused the token — the one verdict here that names a remedy.
        inner_error = OAuthGrantRejected(
            f"the upstream still rejected the new OAuth token (HTTP {final_status}) — the "
            "granted scopes or resource may not match what this server requires"
        )
        tokens = None
    if tokens is not None:
        # Validate-and-promote as ONE step under the config write lock — the same
        # lock every ownership transfer, server delete, and OAuth reconfiguration
        # commits under — so none of them can land between _promotion_blocked's
        # read and the file write (the begin-time snapshots close the
        # pre-registration window; the lock closes the check-to-write one). The
        # promote itself is ONE atomic write that fully replaces any prior state:
        # building it in memory first means a failure leaves the previous
        # (still-working) credential intact — no destructive pre-clear — and it
        # doesn't carry forward an old refresh token (a new grant brings its own).
        # Runs in a worker thread: the lock is a threading lock that bulk imports
        # can hold for seconds, and _drive lives on the event loop.
        client_info = await mem.get_client_info()
        oauth_metadata = getattr(provider.context, "oauth_metadata", None)
        pr_metadata = getattr(provider.context, "protected_resource_metadata", None)

        def _checked_promote() -> Optional[OAuthPromotionBlocked]:
            from app.registry import service  # local import: keep module load cycle-free

            with service.config_write_lock():
                blocked = _promotion_blocked(pending)
                if blocked is not None:
                    return blocked
                real.promote(
                    tokens=tokens,
                    client_info=client_info,
                    metadata=oauth_metadata,
                    protected_resource_metadata=pr_metadata,
                )
                return None

        try:
            blocked = await asyncio.to_thread(_checked_promote)
        except Exception as exc:  # noqa: BLE001 — persistence failure = the grant didn't stick
            inner_error = exc
        else:
            if blocked is not None:
                # Overrides, never defers: tokens landing WITH an inner_error is a
                # tolerated outcome (the exchange stores them before the original
                # request is retried, so a failing retry still leaves a usable grant),
                # so `inner_error or …` would keep that incidental retry/transport
                # error and report the failure as unclassified. A promotion block is
                # the definitive, actionable reason the grant was discarded.
                inner_error = blocked
            else:
                if not pending.done_future.done():
                    pending.done_future.set_result(None)
                return

    error = inner_error or RuntimeError("OAuth flow finished without returning tokens")
    # WARNING, not INFO: this is the root-cause text for a failed operator-initiated
    # sign-in, and under uvicorn's default logging (no root handler, app.* effective
    # level WARNING) an INFO record is dropped before it reaches stderr — leaving the
    # operator with a failure toast and no way to find out why.
    # log_safe, not redact_secrets: this text comes from the provider, so it also needs
    # control-character folding (no forged records) and a length bound (no drowned one).
    logger.warning("OAuth authorization for %s failed: %s", server.id, log_safe(error))
    url_pending = not pending.url_future.done()
    if url_pending:
        # Failed during discovery/registration, before an authorization URL was produced
        # (e.g. the upstream rate-limited DCR with a 429): begin_authorization is awaiting
        # url_future and will surface this to the operator.
        pending.url_future.set_exception(error)
    if not pending.done_future.done():
        if url_pending or not pending.callback_event.is_set():
            # done_future is only ever awaited by complete_authorization, which runs only after
            # a URL was returned AND the browser came back (callback_event set). Having failed
            # before the URL, or after it but with the browser never returning (the callback
            # wait timed out), no one will ever retrieve done_future's exception — setting one
            # would log a spurious "Future exception was never retrieved" when it's collected. A
            # cancelled, never-awaited future is silent, so cancel it instead.
            pending.done_future.cancel()
        else:
            # A URL was handed back and the callback arrived; complete_authorization is awaiting
            # done_future, so surface the failure there.
            pending.done_future.set_exception(error)


async def begin_authorization(server, *, callback_url: str) -> str:
    """Start the interactive flow for ``server`` and return the URL to send the
    operator's browser to. Raises on a discovery/registration failure or timeout."""
    _reap_stale()
    _cancel_existing(
        server.id,
        "a newer sign-in for this server superseded it",
        superseded=True,
    )
    # Snapshot the row's existence BEFORE any await: a deletion landing during the
    # client-info/token-store reads below must still read as "existed at begin", so
    # promotion blocks (_promotion_blocked) instead of recreating an orphan
    # credential file for the deleted server.
    row_existed = _server_row(server.id) is not None

    real = ServerTokenStorage(server.id)
    redirect_uris = [AnyHttpUrl(callback_url)]
    # The client-identity mode for this sign-in (server override, else the runtime
    # setting) — resolved once, before any await, and used both for seed reuse below
    # and for the CIMD offer decision. Irrelevant when a static client id is set.
    client_mode = _effective_client_mode(server)

    # Seed the client info the flow will use. A static, pre-registered client (operator
    # supplied a client id) skips Dynamic Client Registration; otherwise reuse a prior DCR
    # registration if one exists so a re-auth doesn't register a brand-new client each time.
    if server.oauth_client_id:
        secret = server.oauth_client_secret or None
        seed_client_info: Optional[OAuthClientInformationFull] = OAuthClientInformationFull(
            client_id=server.oauth_client_id,
            client_secret=secret,
            redirect_uris=redirect_uris,
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="client_secret_post" if secret else "none",
            scope=server.oauth_scopes or None,
        )
    else:
        # Reuse a prior DCR registration only if it's still usable. Force re-registration
        # when: it was registered for a DIFFERENT callback URL (mcpelevator now reached via a
        # different base URL — localhost vs LAN, or a changed MCPE_PUBLIC_BASE_URL — whose
        # redirect_uri a strict provider would reject), OR its client secret has EXPIRED
        # (a past nonzero client_secret_expires_at), which would otherwise fail the exchange
        # and leave the operator unable to reconnect via Re-authenticate.
        existing = await real.get_client_info()
        registered = {str(u) for u in (getattr(existing, "redirect_uris", None) or [])}
        expires_at = getattr(existing, "client_secret_expires_at", None) if existing else None
        expired = bool(expires_at) and expires_at < time.time()
        # A stored client whose client_id IS this instance's client-metadata URL came from a
        # prior CIMD sign-in (the SDK persists the identity it fabricates). That's not a
        # registration — there's no quota to protect and it's recreated locally for free —
        # so it must NOT seed the flow: seeding bypasses the SDK's CIMD/DCR decision AND the
        # reachability probe below, resending a possibly gated URL client id forever.
        # Symmetrically, an EXPLICIT 'cimd' mode ignores a stored DCR registration — the
        # operator pinned the URL-based identity, and a seeded client would override it.
        cimd_identity = existing is not None and str(existing.client_id) == _client_metadata_url(
            callback_url
        )
        reusable = (
            existing is not None
            and not cimd_identity
            and client_mode != "cimd"
            and callback_url in registered
            and not expired
        )
        seed_client_info = existing if reusable else None

    # Drive the grant against an EPHEMERAL store (no tokens → the probe 401s → the browser
    # redirect fires). The shared store is written only if the grant succeeds (_drive), so a
    # failed or cancelled re-auth can't wipe a still-working credential. The one exception is a
    # freshly DCR-registered client when no seed and no stored tokens exist: persist that
    # registration straight through, so an abandoned browser step doesn't force a re-register
    # next time (quota). Guarded on "no tokens" so it can never rebind a client a live token
    # depends on.
    persist_registration_to = (
        real if seed_client_info is None and (await real.get_tokens()) is None else None
    )
    # The pending record carries the flow's begin-time snapshots (existence, owner,
    # OAuth signature). Constructed BEFORE the ephemeral store so the registration
    # pass-through below can be gated on the SAME judgment as token promotion.
    pending = _Pending(
        server.id,
        owner_id=getattr(server, "owner_id", None),
        row_existed=row_existed,
        oauth_sig=_oauth_signature_of(server),
    )
    # Register BEFORE any further await — notably the CIMD self-probe's network
    # round-trip below. A second Authenticate click (and the delete/reassign/
    # config-edit cancel paths) supersede a flow by finding it here; a flow parked
    # in a pre-registration await would be invisible to them, and two flows for one
    # server could then both run to promotion. The pre-_drive check further down
    # notices the supersession and refuses to start the driven flow.
    _PENDING[pending.id] = pending
    mem = _MemoryTokenStorage(
        client_info=seed_client_info,
        persist_registration_to=persist_registration_to,
        # ONE judgment (_promotion_blocked) governs both this mid-flow write and
        # the final token promotion: the row must still exist with the same owner
        # and OAuth config the flow was started against — a server deleted or
        # reconfigured mid-flow must not get the OLD provider's registration
        # written into (or recreating) its credential file, where a later sign-in
        # would reuse the stale client against the NEW upstream.
        persist_allowed=lambda: _promotion_blocked(pending) is None,
    )

    client_metadata = OAuthClientMetadata(
        client_name=CLIENT_NAME,
        redirect_uris=redirect_uris,
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=server.oauth_scopes or None,
    )

    # URL-based client id (CIMD): the SDK only consumes it when the provider advertises
    # support AND no client is seeded, so DCR/static-client behavior is unchanged
    # everywhere else. The self-probe (with its up-to-8s worst case) runs only when the
    # URL could actually be used — a seeded client bypasses CIMD, so pass the raw
    # derived value there and skip the probe. On any raise, drop the registration made
    # above so a failed begin doesn't leave a dangling pending until the reaper.
    try:
        if seed_client_info is None:
            client_metadata_url = await _decide_client_metadata_url(callback_url, client_mode)
        elif client_mode == "dcr":
            # A seeded client already bypasses CIMD in the SDK, so the URL would be
            # inert either way — but an explicit 'dcr' pick shouldn't offer it at all.
            client_metadata_url = None
        else:
            client_metadata_url = _client_metadata_url(callback_url)
    except BaseException:
        _forget(pending)
        raise

    async def redirect_handler(authorization_url: str) -> None:
        authorization_url = _repair_authorization_url(authorization_url)
        authorization_url = _ensure_consent_prompt(authorization_url)
        state = _extract_state(authorization_url)
        pending.state = state
        if state is not None:
            _STATE_INDEX[state] = pending.id
        if not pending.url_future.done():
            pending.url_future.set_result(authorization_url)

    async def callback_handler() -> tuple[str, Optional[str]]:
        try:
            await asyncio.wait_for(pending.callback_event.wait(), timeout=_FLOW_TIMEOUT)
        except asyncio.TimeoutError as exc:
            raise TimeoutError("timed out waiting for the operator to finish signing in") from exc
        if pending.code is None:
            raise RuntimeError("OAuth callback delivered no authorization code")
        return pending.code, pending.state

    try:
        provider = _ScopedOAuthClientProvider(
            server_url=server.command,
            client_metadata=client_metadata,
            storage=mem,
            redirect_handler=redirect_handler,
            callback_handler=callback_handler,
            timeout=_FLOW_TIMEOUT,
            operator_scopes=server.oauth_scopes or None,
            client_metadata_url=client_metadata_url,
        )
    except BaseException:
        _forget(pending)  # same dangling-registration cleanup as the probe above
        raise

    # Re-check the registration made before the awaits above: if it's gone, a newer
    # Authenticate click (or a delete/config-edit) superseded this flow while it was
    # parked in the self-probe. Refuse to start _drive — the documented invariant is
    # ONE authorization in flight per server, and two driven flows could otherwise
    # both promote, the loser overwriting the winner's tokens.
    if _PENDING.get(pending.id) is not pending:
        raise OAuthBeginError(
            "this sign-in attempt was superseded by a newer one for the same server",
            status_code=409,
        )
    pending.task = asyncio.create_task(_drive(server, provider, mem, real, pending))

    try:
        return await asyncio.wait_for(asyncio.shield(pending.url_future), timeout=_URL_TIMEOUT)
    except asyncio.TimeoutError as exc:
        _forget(pending)
        raise OAuthBeginError(
            "timed out contacting the OAuth provider (metadata discovery / registration)"
        ) from exc
    except Exception as exc:
        _forget(pending)
        raise _classify_begin_error(exc) from exc


async def complete_authorization(state: str, code: str) -> str:
    """Feed the callback's ``(code, state)`` into the parked flow and wait for it to
    finish. Returns the server id on success. Raises ``KeyError`` for an unknown/expired
    state, or the underlying error if the token exchange fails."""
    _reap_stale()
    pending_id = _STATE_INDEX.get(state)
    pending = _PENDING.get(pending_id) if pending_id else None
    if pending is None:
        raise KeyError("unknown or expired OAuth state")
    pending.code = code
    pending.callback_event.set()
    try:
        await asyncio.wait_for(asyncio.shield(pending.done_future), timeout=90.0)
    finally:
        _forget(pending)
    return pending.server_id
