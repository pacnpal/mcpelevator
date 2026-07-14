"""The group registry — the single source of truth for what ``/g/<name>`` serves.

A group is a named, ordered set of registered servers. The registry is the
``groups`` runtime setting (SQLite kv, like every other runtime-mutable knob):
a mapping from group name to either the wildcard ``"*"`` (every registered
server, present and future) or an explicit list of server ids. There is no
special-case group name — ``all`` is just a conventional entry whose member
value is ``"*"``, resolved through the same code path as any other group.

Deterministic semantics (documented in the README route reference):

- **Unknown group name at request time** -> 404, same body shape as an unknown
  server slug on ``/s``.
- **Empty group** (no members, or no member currently running) -> the endpoint
  serves a valid MCP surface with zero tools; members join as they start.

Validation is layered:

- *Structural* (shape, name grammar, member-id strings) happens in the settings
  SSOT writer (``app.registry.settings``), so a malformed value can never be
  stored.
- *Referential* (every member must be a registered server) happens here, at
  write time (:func:`validate_members`, surfaced as a 400) and again at startup
  (:func:`validate_at_startup`, which fails the boot loudly). Deleting a server
  prunes it from every group (:func:`prune_server`) in the same transaction, so
  the startup check can only trip on a hand-edited database — where refusing to
  serve an inconsistent registry is exactly the right failure.
"""

from __future__ import annotations

from typing import Union

from sqlmodel import Session

from app.db import repo
from app.registry import service
from app.registry import settings as runtime_settings

# Every registered server, present and future. A resolution rule, not a group
# name — any group may use it, so "all" needs no special-case code.
WILDCARD = "*"

Members = Union[str, list[str]]  # WILDCARD | [server_id, ...]


class UnknownMemberError(ValueError):
    """A group references a server id that isn't registered."""

    def __init__(self, group: str, server_id: str) -> None:
        self.group = group
        self.server_id = server_id
        super().__init__(
            f"group {group!r} references unknown server id {server_id!r}"
        )


def read(session: Session) -> dict[str, Members]:
    """The registry as stored: ``{name: "*" | [server_id, ...]}``."""
    return runtime_settings.groups(session)


def exists(session: Session, name: str) -> bool:
    return name in read(session)


def validate_members(session: Session, group: str, members: Members) -> None:
    """Referential check for one group's members. Raises :class:`UnknownMemberError`
    (a ``ValueError``) naming the offending group and server id."""
    if members == WILDCARD:
        return
    known = {s.id for s in repo.list_servers(session)}
    for server_id in members:
        if server_id not in known:
            raise UnknownMemberError(group, server_id)


def validate_at_startup(session: Session) -> None:
    """Validate the whole registry against the server table; called from the app
    lifespan. Fails the boot loudly on an unknown member — normal operation can't
    get here (writes validate, deletes prune), so an inconsistent registry means
    the database was edited out-of-band and must not be served as-is."""
    for group, members in read(session).items():
        try:
            validate_members(session, group, members)
        except UnknownMemberError as exc:
            raise RuntimeError(
                f"invalid group registry: {exc} — remove it from the 'groups' "
                f"setting or re-create the server"
            ) from exc


def write_group(session: Session, name: str, members: Members) -> dict[str, Members]:
    """Create or replace one group (referentially validated), returning the full
    registry. Structural validation/normalization (name grammar, dedupe) runs in
    the settings SSOT writer.

    The referential check + write are serialized against server create/update/delete
    (``config_write_lock``): otherwise a delete could remove a member between
    ``validate_members`` and the commit, persisting a dangling reference the startup
    validation would then refuse to boot on."""
    with service.config_write_lock():
        validate_members(session, name, members)
        updated = dict(read(session))
        updated[name] = members
        runtime_settings.write(session, {"groups": updated})
        return read(session)


def delete_group(session: Session, name: str) -> bool:
    """Remove a group; returns False when it doesn't exist. Serialized with server
    writes so it can't interleave with a concurrent registry-touching operation."""
    with service.config_write_lock():
        current = dict(read(session))
        if name not in current:
            return False
        del current[name]
        runtime_settings.write(session, {"groups": current})
        return True


def prune_server(session: Session, server_id: str) -> None:
    """Drop a (just-deleted) server from every explicit member list, keeping the
    registry referentially intact without stranding groups. Wildcard groups need
    nothing — they resolve against the live server table. Serialized with server
    writes so a concurrent group write can't re-add the id after this prunes it."""
    with service.config_write_lock():
        current = read(session)
        updated = {
            name: members if members == WILDCARD else [m for m in members if m != server_id]
            for name, members in current.items()
        }
        if updated != current:
            runtime_settings.write(session, {"groups": updated})


def resolve(session: Session, name: str) -> list[str] | None:
    """The group's member server ids (wildcard resolved against the server table),
    or ``None`` for an unknown group. An empty list is a valid group with no
    members — callers serve it as an empty bundle, not an error."""
    registry = read(session)
    if name not in registry:
        return None
    members = registry[name]
    if members == WILDCARD:
        return [s.id for s in repo.list_servers(session)]
    return list(members)
