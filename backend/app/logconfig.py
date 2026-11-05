"""Standard logging: what the control plane prints when debug mode is OFF.

Without this, nothing is installed on the root logger, so ``app.*`` INFO records go
nowhere and WARNING+ reach stderr through Python's last-resort handler with no
timestamp, level or source. This puts one handler on the ``app`` logger (the control
plane's own modules; libraries keep their own levels and handlers), at
``MCPE_LOG_LEVEL`` (default INFO), so a server starting, failing and why lands in the
container log without turning on debug mode.

uvicorn's per-request access log is silenced here too unless ``MCPE_ACCESS_LOG`` is on:
the UI polls every few seconds, so it would otherwise bury everything else.

Debug mode replaces it: ``debug.configure_logging`` owns the root logger then and
removes this handler, so nothing prints twice; it also re-opens every logger,
uvicorn's access log included.

Every line passes through ``debug.scrub``: a failure report quotes the server's own
output, which can hold a credential.
"""

from __future__ import annotations

import logging
import sys

from app import debug
from app.config import get_settings

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_MARK = "_mcpe_standard"
ACCESS_LOGGER = "uvicorn.access"


class _Scrubbed(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return debug.scrub(super().format(record))


def level() -> int:
    """``MCPE_LOG_LEVEL`` as a logging level; an unknown name falls back to INFO."""
    value = logging.getLevelName(str(get_settings().log_level).strip().upper())
    return value if isinstance(value, int) else logging.INFO


def configure() -> None:
    """Install the standard handler on the ``app`` logger. Idempotent; a no-op in
    debug mode, which configures logging itself."""
    if debug.enabled():
        return
    # uvicorn configured its own loggers before the app's lifespan ran this; raising
    # the access logger's level drops each request line (logged at INFO) at the source.
    # With MCPE_ACCESS_LOG on, uvicorn's own level stands.
    if not get_settings().access_log:
        logging.getLogger(ACCESS_LOGGER).setLevel(logging.WARNING)
    log = logging.getLogger("app")
    log.setLevel(level())
    if any(is_standard(h) for h in log.handlers):
        return
    handler = logging.StreamHandler(sys.stderr)
    setattr(handler, _MARK, True)
    handler.setFormatter(_Scrubbed(FORMAT))
    log.addHandler(handler)


def is_standard(handler: logging.Handler) -> bool:
    return bool(getattr(handler, _MARK, False))
