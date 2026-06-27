"""Bootstrap settings (process-level, from environment).

These are read once at startup and cannot change at runtime. Runtime-mutable
settings (bind_mode, allowed_hosts, auth defaults, docker-runner enable) live in
the SQLite ``setting`` table so the UI can edit them — see app.registry.settings.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.util import host_only


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MCPE_", env_file=".env", extra="ignore")

    @field_validator("*", mode="before")
    @classmethod
    def _strip_env_whitespace(cls, v: object) -> object:
        # Env values often carry stray surrounding whitespace — a trailing space on
        # a compose/.env line, a newline from a mounted secret file. Pydantic's
        # strict scalar parsers reject e.g. "true " as a bool, so trim before coercion.
        return v.strip() if isinstance(v, str) else v

    # --- control plane / edge ---
    host: str = "127.0.0.1"  # socket bind for the control plane (in Docker: 0.0.0.0)
    port: int = 8080
    # Absolute base URL clients use to reach this instance. If unset, derived from
    # host:port. Set MCPE_PUBLIC_BASE_URL to e.g. https://mcp.example.com behind a tunnel.
    public_base_url: str | None = None
    # Comma-separated CIDRs whose peer IPs are treated as loopback for the Host/Origin
    # guard — e.g. a reverse proxy or the Docker bridge gateway forwarding a
    # loopback-published port. Empty by default; a bare bind trusts only real loopback.
    trusted_proxies: str = ""
    # Treat the container's default gateway — the Docker host as seen from inside a
    # bridge-networked container — as a trusted proxy, without hardcoding the per-network
    # gateway CIDR in MCPE_TRUSTED_PROXIES. Lets a loopback-published port (-p 8080:8080)
    # reaching the container via the host's docker-proxy pass the Host/Origin guard.
    trust_docker_host: bool = False
    # Comma-separated extra hostnames the Host/Origin guard always trusts, exactly like
    # the host of MCPE_PUBLIC_BASE_URL but for additional origins (a reverse proxy, a
    # tunnel, a second domain). Lets a headless box declare its allowed origins via env
    # without UI access; the runtime allowed_hosts setting still applies under expose.
    allowed_hosts: str = ""
    # Break-glass control-plane admin token: if set, it's always accepted on /api
    # (constant-time compared). Recovers a lost minted token; handy for CI/automation.
    admin_token: str | None = None
    # Force-mint a fresh control (admin) token on boot even when one already exists, then
    # print it — the recovery hatch for a headless box whose admin token was lost. Mints
    # a NEW token (existing ones keep working); unset it after grabbing the token from the
    # logs, or a new one is minted on every restart.
    mint_admin_token: bool = False
    # First-boot seed for the `allow_private_lan` runtime setting. Lets a headless box
    # (no loopback browser to reach the UI) enable LAN access declaratively: set this
    # true and, because LAN access turns control-plane auth on, the startup bootstrap
    # mints an admin token and prints it to the logs. Seeds only when the setting has
    # never been written; the Settings UI is authoritative afterwards.
    allow_private_lan: bool = False

    # --- data / persistence ---
    data_dir: Path = Path("./data")
    db_path: Path | None = None  # derived from data_dir if unset

    # --- frontend static build (served by FastAPI in prod) ---
    frontend_dir: Path = Path("../frontend/build")

    # --- bridge / supervisor ---
    bridge_host: str = "127.0.0.1"  # loopback host for per-server bridge processes
    port_range_start: int = 49200
    port_range_end: int = 49400
    max_running: int = 50
    start_timeout_s: float = 120.0  # generous: covers npx/uvx cold-start install
    health_interval_s: float = 10.0
    restart_budget: int = 5  # consecutive failed (re)starts before -> FAILED

    @property
    def resolved_db_path(self) -> Path:
        return self.db_path or (self.data_dir / "mcpelevator.db")

    @property
    def base_url(self) -> str:
        if self.public_base_url:
            return self.public_base_url.rstrip("/")
        # 0.0.0.0 / :: are bind addresses, not reachable hosts — a client following
        # such a URL would send Host: 0.0.0.0 and fail the allowlist. Advertise
        # loopback instead; set MCPE_PUBLIC_BASE_URL for a real off-host URL.
        host = "127.0.0.1" if self.host in ("0.0.0.0", "::", "") else self.host
        if ":" in host:  # bracket an IPv6 literal so the URL is well-formed
            host = f"[{host}]"
        return f"http://{host}:{self.port}"

    @property
    def public_host(self) -> str | None:
        """Hostname of MCPE_PUBLIC_BASE_URL, if set — an operator-declared trusted
        host that the Host/Origin guard always allows (so the advertised public URL
        doesn't 403 itself before the operator can add it to the allowlist)."""
        return host_only(self.public_base_url) if self.public_base_url else None

    @property
    def extra_allowed_hosts(self) -> list[str]:
        """Hostnames from MCPE_ALLOWED_HOSTS, normalized and de-duped — the Host/Origin
        guard always trusts these (see ``app.auth.middleware.request_allowlist``)."""
        seen: list[str] = []
        for raw in self.allowed_hosts.split(","):
            host = host_only(raw)
            if host and host not in seen:
                seen.append(host)
        return seen

    @property
    def backend_root(self) -> Path:
        # .../backend (parent of the app package) — the cwd/PYTHONPATH for child
        # bridge processes so `python -m app.bridge.host` resolves.
        return Path(__file__).resolve().parents[1]


@lru_cache
def get_settings() -> Settings:
    return Settings()
