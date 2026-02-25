"""Tests for qBittorrent startup preflight behavior."""

from __future__ import annotations

import logging
from collections.abc import Sequence

import pytest

from diskguard.errors import (
    QbittorrentAuthenticationError,
    QbittorrentUnavailableError,
    StartupPreflightError,
)
from diskguard.startup import run_qbittorrent_startup_preflight
from diskguard.startup import validate_qbittorrent_url


class FakeVersionProbeClient:
    """Fake qBittorrent client returning predefined outcomes per call."""

    def __init__(self, outcomes: Sequence[Exception | str]) -> None:
        self._outcomes = list(outcomes)
        self.calls = 0

    async def fetch_application_version(self) -> str:
        self.calls += 1
        index = min(self.calls - 1, len(self._outcomes) - 1)
        outcome = self._outcomes[index]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class SleepRecorder:
    """Captures requested retry sleeps without blocking tests."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def test_validate_qbittorrent_url_rejects_invalid_scheme() -> None:
    with pytest.raises(StartupPreflightError):
        validate_qbittorrent_url("qbittorrent:8080")


def test_validate_qbittorrent_url_rejects_missing_hostname() -> None:
    with pytest.raises(StartupPreflightError):
        validate_qbittorrent_url("http://:8080")


async def test_startup_preflight_retries_then_succeeds(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    client = FakeVersionProbeClient(
        [
            QbittorrentUnavailableError("offline"),
            QbittorrentAuthenticationError("invalid credentials"),
            "4.6.5",
        ]
    )
    sleep_recorder = SleepRecorder()
    logger = logging.getLogger("diskguard.startup.test")

    await run_qbittorrent_startup_preflight(
        client,
        qb_url="http://qbittorrent:8080",
        logger=logger,
        sleep_func=sleep_recorder,
    )

    assert client.calls == 3
    assert sleep_recorder.calls == [1.0, 2.0]
    assert any(
        record.levelno == logging.INFO and "Connected to qBittorrent at http://qbittorrent:8080" in record.getMessage()
        for record in caplog.records
    )
    assert sum(record.levelno == logging.WARNING for record in caplog.records) == 2


async def test_startup_preflight_success_log_redacts_url_credentials(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    client = FakeVersionProbeClient(["4.6.5"])
    logger = logging.getLogger("diskguard.startup.test")

    await run_qbittorrent_startup_preflight(
        client,
        qb_url="http://user:pass@qbittorrent:8080",
        logger=logger,
    )

    assert any(
        record.levelno == logging.INFO
        and "Connected to qBittorrent at http://<redacted>@qbittorrent:8080"
        in record.getMessage()
        for record in caplog.records
    )
    assert all("user:pass@" not in record.getMessage() for record in caplog.records)


async def test_startup_preflight_retries_then_fails(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    client = FakeVersionProbeClient([QbittorrentUnavailableError("offline")])
    sleep_recorder = SleepRecorder()
    logger = logging.getLogger("diskguard.startup.test")

    with pytest.raises(StartupPreflightError) as exc_info:
        await run_qbittorrent_startup_preflight(
            client,
            qb_url="http://qbittorrent:8080",
            logger=logger,
            sleep_func=sleep_recorder,
        )

    assert client.calls == 10
    assert sleep_recorder.calls == [1.0, 2.0, 3.0, 4.0, 5.0, 5.0, 5.0, 5.0, 5.0]
    assert "after 10 attempts" in str(exc_info.value)
    assert any(record.levelno == logging.ERROR for record in caplog.records)


async def test_startup_preflight_retry_and_error_logs_redact_url_credentials(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    client = FakeVersionProbeClient([QbittorrentUnavailableError("offline")])
    sleep_recorder = SleepRecorder()
    logger = logging.getLogger("diskguard.startup.test")

    with pytest.raises(StartupPreflightError):
        await run_qbittorrent_startup_preflight(
            client,
            qb_url="http://user:pass@qbittorrent:8080",
            logger=logger,
            max_attempts=2,
            sleep_func=sleep_recorder,
        )

    messages = [record.getMessage() for record in caplog.records]
    assert any("http://<redacted>@qbittorrent:8080" in message for message in messages)
    assert all("user:pass@" not in message for message in messages)


async def test_startup_preflight_auth_failure_surfaces_explicit_auth_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    client = FakeVersionProbeClient([QbittorrentAuthenticationError("invalid credentials")])
    sleep_recorder = SleepRecorder()
    logger = logging.getLogger("diskguard.startup.test")

    with pytest.raises(StartupPreflightError) as exc_info:
        await run_qbittorrent_startup_preflight(
            client,
            qb_url="http://qbittorrent:8080",
            logger=logger,
            max_attempts=2,
            sleep_func=sleep_recorder,
        )

    assert "authentication error" in str(exc_info.value).lower()
    assert any(
        record.levelno == logging.WARNING and "authentication error" in record.getMessage().lower()
        for record in caplog.records
    )
