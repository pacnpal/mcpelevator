"""SCIM request bodies shaped like the ones Entra ID's and Okta's provisioning clients
send, attribute for attribute (including what mcpelevator ignores: name, emails, roles,
password, the enterprise extension)."""

from __future__ import annotations

USER = "urn:ietf:params:scim:schemas:core:2.0:User"
ENTERPRISE = "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"
PATCH_OP = "urn:ietf:params:scim:api:messages:2.0:PatchOp"


def entra_create(user_name: str, external_id: str = "0a21f0f2-8d2a-4f8e-bf98-7363c4aed4ef") -> dict:
    return {
        "schemas": [USER, ENTERPRISE],
        "externalId": external_id,
        "userName": user_name,
        "active": True,
        "displayName": "Ada Lovelace",
        "emails": [{"primary": True, "type": "work", "value": user_name}],
        "meta": {"resourceType": "User"},
        "name": {"formatted": "Ada Lovelace", "familyName": "Lovelace", "givenName": "Ada"},
        "roles": [],
        ENTERPRISE: {"department": "Engineering"},
    }


def entra_active(value: str) -> dict:
    """Entra's deactivate and reactivate: a capitalized op and a string boolean."""
    return {
        "schemas": [PATCH_OP],
        "Operations": [{"op": "Replace", "path": "active", "value": value}],
    }


def entra_replace_attributes() -> dict:
    """Entra's attribute update: several path ops at once, some on attributes we don't
    keep."""
    return {
        "schemas": [PATCH_OP],
        "Operations": [
            {"op": "Replace", "path": "displayName", "value": "Ada King"},
            {"op": "Add", "path": "externalId", "value": "entra-2"},
            {"op": "Replace", "path": "name.familyName", "value": "King"},
            {"op": "Add", "path": 'emails[type eq "work"].value', "value": "ada@example.com"},
        ],
    }


def okta_create(user_name: str) -> dict:
    return {
        "schemas": [USER],
        "userName": user_name,
        "name": {"givenName": "Ada", "familyName": "Lovelace"},
        "emails": [{"primary": True, "value": user_name, "type": "work"}],
        "displayName": "Ada Lovelace",
        "locale": "en-US",
        "externalId": "00ujl29u0le5T6Aj10h7",
        "groups": [],
        "password": "1mz050nq",
        "active": True,
    }


def okta_active(value: bool) -> dict:
    """Okta's deactivate and reactivate: no path, a value object, a real boolean."""
    return {"schemas": [PATCH_OP], "Operations": [{"op": "replace", "value": {"active": value}}]}


def okta_replace(user_id: str, user_name: str, display_name: str) -> dict:
    """Okta's PUT: the whole resource back, with its id."""
    return {
        "schemas": [USER],
        "id": user_id,
        "userName": user_name,
        "name": {"givenName": "Ada", "familyName": "King"},
        "emails": [{"primary": True, "value": user_name, "type": "work"}],
        "displayName": display_name,
        "externalId": "00ujl29u0le5T6Aj10h7",
        "active": True,
        "groups": [],
    }
