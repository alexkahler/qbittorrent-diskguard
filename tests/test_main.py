"""Tests for CLI entrypoint failure behavior."""

from __future__ import annotations

import importlib
import logging

import pytest

from diskguard.errors import ConfigError
from diskguard.errors import StartupPreflightError
from diskguard.version import APP_VERSION
from tests.helpers import make_config

main_module = importlib.import_module("diskguard.main")


def test_main_exits_non_zero_when_startup_preflight_fails(monkeypatch) -> None:
    """Tests that main exits non zero when startup preflight fails."""
    monkeypatch.setattr(main_module, "load_config", lambda: make_config())
    monkeypatch.setattr(main_module, "_configure_logging", lambda _: None)

    def fake_asyncio_run(coro) -> None:
        """Fake asyncio run."""
        coro.close()
        raise StartupPreflightError("preflight failed")

    monkeypatch.setattr(main_module.asyncio, "run", fake_asyncio_run)

    try:
        main_module.main()
    except SystemExit as exc:
        assert exc.code == 1
        return

    raise AssertionError("Expected SystemExit(1)")


def test_main_logs_application_version_on_startup(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tests that main logs application version on startup."""
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(main_module, "_configure_logging", lambda _: None)

    def fake_load_config():
        """Fake load config."""
        raise ConfigError("invalid config")

    monkeypatch.setattr(main_module, "load_config", fake_load_config)

    with pytest.raises(SystemExit) as exc_info:
        main_module.main()

    assert exc_info.value.code == 2
    assert any(
        record.levelno == logging.INFO and record.getMessage() == f"Starting DiskGuard v{APP_VERSION}"
        for record in caplog.records
    )
