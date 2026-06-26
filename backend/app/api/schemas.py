"""API request/response models (the control-plane contract the SPA depends on)."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class Transports(BaseModel):
    mcp_http: bool
    rest_openapi: bool


class Urls(BaseModel):
    mcp: Optional[str] = None
    rest: Optional[str] = None


class ServerSummary(BaseModel):
    id: str
    slug: str
    name: str
    runner: str
    enabled: bool
    state: str
    transports: Transports
    urls: Urls
    last_error: Optional[str] = None
    pid: Optional[int] = None
    port: Optional[int] = None
    tools_count: int = 0


class ServerDetail(ServerSummary):
    command: str
    args: list[str] = []
    env: dict[str, str] = {}
    cwd: Optional[str] = None
    auth_provider: str = "inherit"
    config_hash: str = ""
    source: str = "manual"
    tools: list[dict] = []


class ServerCreate(BaseModel):
    name: str
    runner: str = "npx"
    command: str
    args: list[str] = []
    env: dict[str, str] = {}
    cwd: Optional[str] = None
    mcp_http: bool = True
    rest_openapi: bool = False
    auth_provider: str = "inherit"
    enabled: bool = False


class ServerUpdate(BaseModel):
    name: Optional[str] = None
    runner: Optional[str] = None
    command: Optional[str] = None
    args: Optional[list[str]] = None
    env: Optional[dict[str, str]] = None
    cwd: Optional[str] = None
    mcp_http: Optional[bool] = None
    rest_openapi: Optional[bool] = None
    auth_provider: Optional[str] = None


class ImportSkipped(BaseModel):
    name: str
    reason: str


class ImportResult(BaseModel):
    created: list[ServerSummary]
    skipped: list[ImportSkipped]


class TokenCreate(BaseModel):
    name: str


class TokenInfo(BaseModel):
    id: str
    name: str
    prefix: str
    created_at: datetime


class TokenCreated(TokenInfo):
    token: str  # plaintext — returned exactly once, at creation


class SettingsInfo(BaseModel):
    bind_mode: str
    allowed_hosts: list[str]
    default_auth_provider: str


class SettingsUpdate(BaseModel):
    bind_mode: Optional[str] = None
    allowed_hosts: Optional[list[str]] = None
    default_auth_provider: Optional[str] = None
