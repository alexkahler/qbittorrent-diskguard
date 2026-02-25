"""Tests for CLI entrypoint failure behavior."""

from __future__ import annotations

import importlib

from diskguard.errors import StartupPreflightError
from tests.helpers import make_config

main_module = importlib.import_module("diskguard.main")


def test_main_exits_non_zero_when_startup_preflight_fails(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "load_config", lambda: make_config())
    monkeypatch.setattr(main_module, "_configure_logging", lambda _: None)

    def fake_asyncio_run(coro) -> None:
        coro.close()
        raise StartupPreflightError("preflight failed")

    monkeypatch.setattr(main_module.asyncio, "run", fake_asyncio_run)

    try:
        main_module.main()
    except SystemExit as exc:
        assert exc.code == 1
        return

    raise AssertionError("Expected SystemExit(1)")
