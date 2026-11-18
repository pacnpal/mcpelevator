"""Standard (non-debug) logging: the control plane's own records reach stderr with a
timestamp, level and source, scrubbed, without MCPE_DEBUG."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from app import logconfig


@pytest.fixture
def app_logger(monkeypatch):
    log = logging.getLogger("app")
    access = logging.getLogger(logconfig.ACCESS_LOGGER)
    saved = (log.level, list(log.handlers), access.level)
    log.handlers = [h for h in log.handlers if not logconfig.is_standard(h)]
    yield log
    log.setLevel(saved[0])
    log.handlers = saved[1]
    access.setLevel(saved[2])


def _use(monkeypatch, **values):
    settings = SimpleNamespace(
        **{"debug": False, "log_level": "INFO", "access_log": False, **values}
    )
    monkeypatch.setattr(logconfig, "get_settings", lambda: settings)
    monkeypatch.setattr(logconfig.debug, "get_settings", lambda: settings)


def test_configure_installs_one_scrubbed_handler(app_logger, monkeypatch, capsys):
    _use(monkeypatch)
    logconfig.configure()
    logconfig.configure()  # idempotent: a second app in the process adds nothing
    assert sum(logconfig.is_standard(h) for h in app_logger.handlers) == 1
    assert app_logger.level == logging.INFO

    token = "mcpe_" + "a" * 40
    logging.getLogger("app.supervisor.unit").info("server x: failed with %s", token)
    logging.getLogger("app.supervisor.unit").debug("not at INFO")
    err = capsys.readouterr().err
    assert "INFO app.supervisor.unit: server x: failed with mcpe_aaaaaaa…" in err
    assert token not in err
    assert "not at INFO" not in err


def test_debug_mode_leaves_logging_to_debug(app_logger, monkeypatch):
    _use(monkeypatch, debug=True)
    logconfig.configure()
    assert not any(logconfig.is_standard(h) for h in app_logger.handlers)


@pytest.mark.parametrize(
    ("value", "level"),
    [("warning", logging.WARNING), (" DEBUG ", logging.DEBUG), ("nonsense", logging.INFO)],
)
def test_log_level_setting(monkeypatch, value, level):
    _use(monkeypatch, log_level=value)
    assert logconfig.level() == level


@pytest.mark.parametrize(("access_log", "shown"), [(False, False), (True, True)])
def test_access_log_hidden_unless_asked_for(app_logger, monkeypatch, access_log, shown):
    # The UI polls; one INFO line per request buries the supervisor's lines.
    _use(monkeypatch, access_log=access_log)
    access = logging.getLogger(logconfig.ACCESS_LOGGER)
    access.setLevel(logging.INFO)  # what uvicorn's own config sets before the lifespan
    logconfig.configure()
    assert access.isEnabledFor(logging.INFO) is shown
    assert access.isEnabledFor(logging.WARNING)


def test_debug_mode_leaves_the_access_log_alone(app_logger, monkeypatch):
    _use(monkeypatch, debug=True)
    access = logging.getLogger(logconfig.ACCESS_LOGGER)
    access.setLevel(logging.INFO)
    logconfig.configure()
    assert access.isEnabledFor(logging.INFO)


def test_client_disconnect_is_not_logged_as_a_server_error(capsys):
    """The MCP SDK logs a client that hangs up mid-POST (a cancelled call, a closed
    laptop) as "Error handling POST request" plus a traceback. Nothing failed on this
    side, so it drops to a one-line DEBUG record; any other exception stays an ERROR."""
    from starlette.requests import ClientDisconnect

    logconfig.quiet_client_disconnects()
    logconfig.quiet_client_disconnects()  # idempotent
    log = logging.getLogger(logconfig.MCP_HTTP_LOGGER)
    assert sum(isinstance(f, logconfig._ClientDisconnects) for f in log.filters) == 1

    records: list[logging.LogRecord] = []

    class Keep(logging.Handler):
        def emit(self, record):
            records.append(record)

    keep = Keep(level=logging.DEBUG)
    log.addHandler(keep)
    try:
        try:
            raise ClientDisconnect()
        except ClientDisconnect:
            log.exception("Error handling POST request")
        try:
            raise RuntimeError("real failure")
        except RuntimeError:
            log.exception("Error handling POST request")
    finally:
        log.removeHandler(keep)

    disconnect, failure = records
    assert disconnect.levelno == logging.DEBUG and not disconnect.exc_info
    assert "client disconnected" in disconnect.getMessage()
    assert failure.levelno == logging.ERROR and failure.exc_info
    # The demoted record falls below Python's last-resort handler (WARNING): no traceback.
    assert "ClientDisconnect" not in capsys.readouterr().err
