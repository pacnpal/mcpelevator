"""WHAT a principal may do — every multi-user authorization rule, in one module.

Routers call these predicates; none of them re-implements a rule locally, so the
policy is written once and enforced everywhere (servers, tokens, health, import).
Identity comes from ``app.auth.principal``; this module never reads the request.

The rules (deterministic, documented once here):

- **Visibility**: an admin sees every server; a member sees exactly the servers
  they own. ``owner_id`` NULL is admin-owned (the upgrade default). Non-visible
  servers 404 — indistinguishable from nonexistent, so ids don't leak.
- **Runners**: an admin may use any runner; a member may always use ``remote``,
  and the local runners (npx/uvx/command/docker — code execution on this box)
  only when their ``local_runners`` flag is on.
- **Token scopes**: an admin may mint any scope. A member may mint tokens only
  for servers they own — not ``all``, not ``control``, not ``group:*`` (groups
  are a global, admin-owned surface).
- **Token visibility**: an admin sees every token; a member sees only their own.
- **``oauth`` servers**: a provider access token that resolves to an admin passes,
  and one that resolves to the server's owner passes. A resolved member on someone
  else's server is refused under ``oauth_member_access`` ``owner_only`` even when the
  allowlist names them; under ``additive`` they follow the allowlist. A token that
  resolves to no one follows the allowlist in both modes. A group has no owner, so
  under ``owner_only`` a member is refused on every ``oauth`` group.
"""

from __future__ import annotations

from typing import Optional

from fastapi import HTTPException

from app.auth.principal import Principal
from app.db.models import Server, Token

# Runners that execute operator-supplied code in mcpelevator's host environment.
# (docker included: it drives the root-equivalent host Docker daemon.)
LOCAL_RUNNERS = ("npx", "uvx", "command", "docker")


# ---- servers ----------------------------------------------------------------


def can_view_server(principal: Principal, server: Server) -> bool:
    if principal.is_admin:
        return True
    return server.owner_id is not None and server.owner_id == principal.user_id


def visible_servers(principal: Principal, servers: list[Server]) -> list[Server]:
    if principal.is_admin:
        return servers
    return [s for s in servers if can_view_server(principal, s)]


def require_visible_server(principal: Principal, server: Optional[Server]) -> Server:
    """The shared 404 gate for every /api/servers/{id}-shaped route: a server the
    principal can't see is reported exactly like one that doesn't exist."""
    if server is None or not can_view_server(principal, server):
        raise HTTPException(status_code=404, detail="server not found")
    return server


def oauth_refusal(
    caller: Optional[Principal], server: Server, *, allowlisted: bool, owner_only: bool
) -> Optional[str]:
    """Why a verified provider access token may NOT reach this ``oauth`` server or group
    (None = allowed). ``caller`` is who the token resolves to (None = no one);
    ``allowlisted`` is whether ``oauth_allowed_subjects`` lets its claims in."""
    if caller is not None and can_view_server(caller, server):
        return None
    if caller is not None and owner_only:
        if server.id.startswith("group:"):
            return "oauth_member_access is owner_only: groups are open only to admins"
        return "this server belongs to another user"
    return None if allowlisted else "token subject not authorized for this server"


def can_use_runner(principal: Principal, runner: str) -> bool:
    if principal.is_admin or runner not in LOCAL_RUNNERS:
        return True
    return principal.local_runners


def require_runner_allowed(principal: Principal, runner: str) -> None:
    if not can_use_runner(principal, runner):
        raise HTTPException(
            status_code=403,
            detail=(
                f"your account may not configure {runner!r} servers — local runners "
                "execute code on this box; ask an admin to enable them for you"
            ),
        )


# ---- tokens -----------------------------------------------------------------


def can_view_token(principal: Principal, token: Token) -> bool:
    if principal.is_admin:
        return True
    return token.user_id is not None and token.user_id == principal.user_id


def visible_tokens(principal: Principal, tokens: list[Token]) -> list[Token]:
    if principal.is_admin:
        return tokens
    return [t for t in tokens if can_view_token(principal, t)]


def token_scope_error(principal: Principal, scope: str, server: Optional[Server]) -> Optional[str]:
    """Why this principal may NOT mint a token of ``scope`` (None = allowed).
    ``server`` is the resolved row for a server-id scope (None for the named
    scopes). The caller has already validated the scope's own well-formedness."""
    if principal.is_admin:
        return None
    if scope in ("all", "control") or scope.startswith("group:"):
        return f"only admins may mint {scope!r}-scoped tokens"
    if server is None or not can_view_server(principal, server):
        # Same shape as the admin path's dangling-id error: existence is not leaked.
        return f"unknown server scope {scope!r}"
    return None
