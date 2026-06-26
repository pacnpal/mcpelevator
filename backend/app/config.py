"""Bootstrap settings (process-level, from environment).

These are read once at startup and cannot change at runtime. Runtime-mutable
settings (bind_mode, allowed_hosts, auth defaults, docker-runner enable) live in
the SQLite ``setting`` table so the UI can edit them — see app.registry.settings.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MCPE_", env_file=".env", extra="ignore")

    # --- control plane / edge ---
    host: str = "127.0.0.1"  # socket bind for the control plane (in Docker: 0.0.0.0)
    port: int = 8080
    # Absolute base URL clients use to reach this instance. If unset, derived from
    # host:port. Set MCPE_PUBLIC_BASE_URL to e.g. https://mcp.example.com behind a tunnel.
    public_base_url: str | None = None

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
    def backend_root(self) -> Path:
        # .../backend (parent of the app package) — the cwd/PYTHONPATH for child
        # bridge processes so `python -m app.bridge.host` resolves.
        return Path(__file__).resolve().parents[1]


@lru_cache
def get_settings() -> Settings:
    return Settings()
