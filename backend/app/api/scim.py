"""SCIM 2.0 provisioning (RFC 7643/7644) at ``/scim/v2``: an identity provider creates,
finds, replaces, patches and deactivates Users. Only ``Users`` and the three discovery
endpoints are served; ``Groups`` isn't (roles come from sign-in group mapping only).

The ONE credential is a token of the ``scim`` scope, checked here directly
(``require_scim``), never through ``principal.resolve``: no control token, Sign-in
session or ``MCPE_ADMIN_TOKEN`` opens SCIM, and a ``scim`` token resolves to no
principal anywhere else. Every write goes through the users API's own mutations
(``change_user``, ``remove_user``) under the config write lock, so deactivation and the
last-admin guard behave exactly as on the Users page."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool
from sqlmodel import Session
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.api.users import change_user, remove_user
from app.api.util import oauth_public_base
from app.auth.control_plane import _bearer, is_env_admin_token
from app.db import get_session, repo
from app.db.models import User
from app.registry import service
from app.util import hash_token, new_id

PREFIX = "/scim/v2"
MEDIA_TYPE = "application/scim+json"
USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
_LIST = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
_ERROR = "urn:ietf:params:scim:api:messages:2.0:Error"
_MAX_RESULTS = 200


class ScimError(Exception):
    """A refusal answered with a SCIM error body (RFC 7644 section 3.12)."""

    def __init__(
        self, status: int, detail: str, scim_type: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status, self.detail, self.scim_type, self.headers = status, detail, scim_type, headers


async def error_response(_request: Request, exc: ScimError) -> JSONResponse:
    body: dict[str, Any] = {"schemas": [_ERROR], "status": str(exc.status), "detail": exc.detail}
    if exc.scim_type:
        body["scimType"] = exc.scim_type
    return JSONResponse(body, status_code=exc.status, media_type=MEDIA_TYPE, headers=exc.headers)


def require_scim(request: Request, session: Session = Depends(get_session)) -> None:
    """Only a ``scim``-scoped token opens SCIM. 401 when none is presented (or it
    matches no token), 403 for any other credential, the break-glass one included."""
    token = _bearer(request)
    row = repo.get_token_by_hash(session, hash_token(token)) if token else None
    if row is not None and row.scope == "scim":
        return
    if row is not None or is_env_admin_token(token):
        raise ScimError(403, "this route needs a token of the scim scope")
    raise ScimError(401, "a scim token is required", headers={"WWW-Authenticate": "Bearer"})


router = APIRouter(prefix=PREFIX, dependencies=[Depends(require_scim)])


def _json(body: Any, status: int = 200, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(body, status_code=status, media_type=MEDIA_TYPE, headers=headers)


def _location(request: Request, path: str) -> str:
    return f"{oauth_public_base(request)}{PREFIX}{path}"


def _when(at: datetime) -> str:
    """ISO 8601 in UTC; SQLite hands stored datetimes back naive."""
    return (at if at.tzinfo else at.replace(tzinfo=timezone.utc)).isoformat()


def _resource(request: Request, user: User) -> dict[str, Any]:
    return {
        "schemas": [USER_SCHEMA],
        "id": user.id,
        "userName": user.name,
        "displayName": user.display_name,
        "externalId": user.scim_external_id,
        "active": bool(user.active),
        "meta": {
            "resourceType": "User",
            "created": _when(user.created_at),
            "lastModified": _when(user.updated_at),
            "location": _location(request, f"/Users/{user.id}"),
        },
    }


def _list(resources: list[dict[str, Any]], *, total: int, start: int = 1) -> dict[str, Any]:
    return {
        "schemas": [_LIST], "totalResults": total, "startIndex": start,
        "itemsPerPage": len(resources), "Resources": resources,
    }


async def _body(request: Request) -> dict[str, Any]:
    """The request's JSON object, whichever of application/scim+json or
    application/json it was sent as."""
    try:
        body = json.loads(await request.body())
    except ValueError:
        raise ScimError(400, "the body is not JSON", "invalidSyntax") from None
    if not isinstance(body, dict):
        raise ScimError(400, "the body must be a JSON object", "invalidSyntax")
    return body


# SCIM attribute names are case-insensitive. Others (name, emails, roles, password, the
# enterprise extension) are accepted and ignored, as RFC 7643 allows a provider to.
_FIELDS = {
    "username": "name", "displayname": "display_name", "externalid": "scim_external_id",
    "active": "active",
}


def _field(attr: str) -> str | None:
    attr = attr.lower()
    if attr.startswith(USER_SCHEMA.lower() + ":"):
        attr = attr[len(USER_SCHEMA) + 1:]
    return _FIELDS.get(attr)


def _value(field: str, value: Any) -> Any:
    if field == "active":
        # Entra sends "True"/"False" strings; Okta sends booleans.
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return value.lower() == "true"
        raise ScimError(400, "active must be a boolean", "invalidValue")
    if field == "name":
        if not isinstance(value, str) or not value.strip():
            raise ScimError(400, "userName must be a non-empty string", "invalidValue")
        return value.strip()
    if value is not None and not isinstance(value, str):
        raise ScimError(400, f"{field} must be a string", "invalidValue")
    return value


def _attrs(values: dict[str, Any]) -> dict[str, Any]:
    changes = {}
    for attr, value in values.items():
        field = _field(attr)
        if field is not None:
            changes[field] = _value(field, value)
    return changes


def _patch(body: dict[str, Any]) -> dict[str, Any]:
    """A PatchOp's changes, keyed by User field: replace, add and remove on userName,
    displayName, externalId and active, with or without a path."""
    ops = {k.lower(): v for k, v in body.items()}.get("operations")
    if not isinstance(ops, list):
        raise ScimError(400, "Operations must be a list", "invalidSyntax")
    changes: dict[str, Any] = {}
    for raw in ops:
        if not isinstance(raw, dict):
            raise ScimError(400, "each operation must be an object", "invalidSyntax")
        op = {k.lower(): v for k, v in raw.items()}
        kind, path, value = str(op.get("op", "")).lower(), op.get("path"), op.get("value")
        if kind not in ("add", "replace", "remove"):
            raise ScimError(400, f"unsupported op {op.get('op')!r}", "invalidSyntax")
        if path is not None and not isinstance(path, str):
            raise ScimError(400, "path must be a string", "invalidPath")
        if kind == "remove":
            field = _field(path or "")
            if field in ("name", "active"):
                raise ScimError(400, f"{path} can't be removed", "mutability")
            if field is not None:
                changes[field] = None
            continue
        if path:
            changes.update(_attrs({path: value}))
        elif isinstance(value, dict):
            changes.update(_attrs(value))
        else:
            raise ScimError(400, "an operation without a path needs an object value", "invalidValue")
    return changes


def _write(session: Session, user: User, changes: dict[str, Any]) -> User:
    """Apply ``changes`` to ``user`` (new or existing) through ``change_user``. The caller
    holds the config write lock. A userName another User already has is 409 uniqueness;
    a deactivation the last-admin guard refuses is 409 with nothing written."""
    name = changes.get("name")
    if name is not None and any(u.id != user.id for u in repo.users_named(session, name)):
        raise ScimError(409, f"a user named {name!r} already exists", "uniqueness")
    for field in ("display_name", "scim_external_id"):
        if field in changes:
            setattr(user, field, changes[field])
    result = change_user(session, user, name=name, active=changes.get("active"))
    if result == "last_admin":
        raise ScimError(409, "cannot deactivate the last admin: no other admin login would remain")
    return result


def _locked(fn):
    """Run ``fn`` under the config write lock in the threadpool, like every users-API
    write (the lock is a threading lock; never wait for it on the event loop)."""

    def run():
        with service.config_write_lock():
            return fn()

    return run_in_threadpool(run)


def _existing(session: Session, user_id: str) -> User:
    session.expire_all()  # see what committed while we waited for the lock
    user = repo.get_user(session, user_id)
    if user is None:
        raise ScimError(404, f"no user {user_id!r}")
    return user


# ---- Users -------------------------------------------------------------------


_FILTER = re.compile(r'\s*username\s+eq\s+"([^"]*)"\s*', re.IGNORECASE)


def _int_param(request: Request, name: str, default: int) -> int:
    raw = request.query_params.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ScimError(400, f"{name} must be an integer", "invalidValue") from None


@router.get("/Users")
async def list_users(request: Request, session: Session = Depends(get_session)):
    filter_ = request.query_params.get("filter")
    if filter_:
        match = _FILTER.fullmatch(filter_)
        if match is None:
            raise ScimError(400, 'the only filter supported is userName eq "..."', "invalidFilter")
        users = repo.users_named(session, match.group(1))
    else:
        users = repo.list_users(session)
    start = max(1, _int_param(request, "startIndex", 1))
    count = min(max(0, _int_param(request, "count", _MAX_RESULTS)), _MAX_RESULTS)
    page = users[start - 1:start - 1 + count]
    return _json(_list([_resource(request, u) for u in page], total=len(users), start=start))


@router.post("/Users")
async def create_user(request: Request, session: Session = Depends(get_session)):
    changes = _attrs(await _body(request))
    if "name" not in changes:
        raise ScimError(400, "userName is required", "invalidValue")
    changes.setdefault("active", True)

    def create():
        session.expire_all()
        # A Member with local runners off, like an SSO-provisioned User. SCIM never
        # sets the role: that comes from sign-in group mapping only.
        user = User(id=new_id(), name=changes["name"], role="member", local_runners=False)
        return _write(session, user, changes)

    user = await _locked(create)
    return _json(_resource(request, user), 201, {"Location": _location(request, f"/Users/{user.id}")})


@router.get("/Users/{user_id}")
async def get_user(user_id: str, request: Request, session: Session = Depends(get_session)):
    return _json(_resource(request, _existing(session, user_id)))


@router.put("/Users/{user_id}")
async def replace_user(user_id: str, request: Request, session: Session = Depends(get_session)):
    changes = _attrs(await _body(request))
    if "name" not in changes:
        raise ScimError(400, "userName is required", "invalidValue")
    # A replace clears what it leaves out, except active: a client that omits it
    # never reactivates anyone by accident.
    changes.setdefault("display_name", None)
    changes.setdefault("scim_external_id", None)
    user = await _locked(lambda: _write(session, _existing(session, user_id), changes))
    return _json(_resource(request, user))


@router.patch("/Users/{user_id}")
async def patch_user(user_id: str, request: Request, session: Session = Depends(get_session)):
    changes = _patch(await _body(request))
    user = await _locked(lambda: _write(session, _existing(session, user_id), changes))
    return _json(_resource(request, user))


@router.delete("/Users/{user_id}")
async def delete_user(user_id: str, session: Session = Depends(get_session)):
    """Deletes a User who owns nothing; a User who owns servers is deactivated instead,
    so their servers keep an owner."""

    def delete():
        user = _existing(session, user_id)
        result = remove_user(session, user_id)
        if result == "last_admin":
            raise ScimError(409, "cannot delete the last admin: no other admin login would remain")
        if isinstance(result, int):
            _write(session, user, {"active": False})

    await _locked(delete)
    return Response(status_code=204)


# ---- discovery -----------------------------------------------------------------


@router.get("/ServiceProviderConfig")
async def service_provider_config(request: Request):
    return _json({
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
        "patch": {"supported": True},
        "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
        "filter": {"supported": True, "maxResults": _MAX_RESULTS},
        "changePassword": {"supported": False},
        "sort": {"supported": False},
        "etag": {"supported": False},
        "authenticationSchemes": [{
            "type": "oauthbearertoken", "name": "Bearer token", "primary": True,
            "description": "A token of the scim scope, minted by an admin in Settings.",
        }],
        "meta": {"resourceType": "ServiceProviderConfig",
                 "location": _location(request, "/ServiceProviderConfig")},
    })


def _attribute(name: str, *, kind: str = "string", required: bool = False,
               uniqueness: str = "none") -> dict[str, Any]:
    return {
        "name": name, "type": kind, "multiValued": False, "required": required,
        "caseExact": False, "mutability": "readWrite", "returned": "default",
        "uniqueness": uniqueness,
    }


@router.get("/Schemas")
async def schemas(request: Request):
    user = {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Schema"],
        "id": USER_SCHEMA, "name": "User", "description": "User Account",
        "attributes": [
            _attribute("userName", required=True, uniqueness="server"),
            _attribute("displayName"),
            _attribute("externalId"),
            _attribute("active", kind="boolean"),
        ],
        "meta": {"resourceType": "Schema", "location": _location(request, f"/Schemas/{USER_SCHEMA}")},
    }
    return _json(_list([user], total=1))


@router.get("/ResourceTypes")
async def resource_types(request: Request):
    user = {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
        "id": "User", "name": "User", "endpoint": "/Users", "schema": USER_SCHEMA,
        "meta": {"resourceType": "ResourceType", "location": _location(request, "/ResourceTypes/User")},
    }
    return _json(_list([user], total=1))


@router.api_route("/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def not_served(rest: str):
    """Everything else, Groups included, is a SCIM 404."""
    raise ScimError(404, f"{PREFIX}/{rest} is not served")
