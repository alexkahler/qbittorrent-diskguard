"""Tests for qBittorrent startup preflight behavior."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from packaging.version import Version
import qbittorrentapi
import pytest
from requests import Response

from diskguard.errors import StartupPreflightError
import diskguard.startup as startup_module
from diskguard.startup import run_qbittorrent_startup_preflight
from diskguard.startup import validate_qbittorrent_url


def _http_error(status_code: int, text: str) -> qbittorrentapi.APIConnectionError:
    """Builds a qbittorrent-api HTTP error with a response object."""
    response = Response()
    response.status_code = status_code
    response._content = text.encode("utf-8")
    if status_code == 401:
        return qbittorrentapi.HTTP401Error(text, response=response)
    if status_code == 403:
        return qbittorrentapi.HTTP403Error(text, response=response)
    if status_code == 404:
        return qbittorrentapi.HTTP404Error(text, response=response)
    if status_code == 500:
        return qbittorrentapi.HTTP500Error(text, response=response)
    raise ValueError(f"Unsupported status code for helper: {status_code}")


def test_derived_minimums_cover_used_qbittorrent_api_endpoints() -> None:
    """Tests that derived compatibility minimums include endpoint feature floor."""
    assert startup_module.MIN_SUPPORTED_QBITTORRENT_VERSION >= Version("4.2.0")
    assert startup_module.MIN_SUPPORTED_WEBAPI_VERSION >= Version("2.3.0")


class FakeVersionProbeClient:
    """Fake qBittorrent client returning predefined outcomes per call."""

    def __init__(
        self,
        app_version_outcomes: Sequence[Exception | str],
        *,
        webapi_version_outcomes: Sequence[Exception | str] | None = None,
    ) -> None:
        """Initializes the test helper state."""
        self._app_version_outcomes = list(app_version_outcomes)
        self._webapi_version_outcomes = list(
            webapi_version_outcomes or [str(startup_module.MIN_SUPPORTED_WEBAPI_VERSION)]
        )
        self.calls = 0
        self.webapi_calls = 0

    def app_version(self) -> str:
        """Fetch application version."""
        self.calls += 1
        outcome = self._resolve_outcome(self._app_version_outcomes, self.calls)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def app_web_api_version(self) -> str:
        """Fetch webapi version."""
        self.webapi_calls += 1
        outcome = self._resolve_outcome(self._webapi_version_outcomes, self.webapi_calls)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    @staticmethod
    def _resolve_outcome(outcomes: list[Exception | str], call_count: int) -> Exception | str:
        """Resolve outcome."""
        index = min(call_count - 1, len(outcomes) - 1)
        return outcomes[index]


class SleepRecorder:
    """Captures requested retry sleeps without blocking tests."""

    def __init__(self) -> None:
        """Initializes the test helper state."""
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        """Records a simulated async sleep invocation."""
        self.calls.append(seconds)


def test_validate_qbittorrent_url_rejects_invalid_scheme() -> None:
    """Tests that validate qbittorrent url rejects invalid scheme."""
    with pytest.raises(StartupPreflightError):
        validate_qbittorrent_url("qbittorrent:8080")


def test_validate_qbittorrent_url_rejects_missing_hostname() -> None:
    """Tests that validate qbittorrent url rejects missing hostname."""
    with pytest.raises(StartupPreflightError):
        validate_qbittorrent_url("http://:8080")


async def test_startup_preflight_retries_then_succeeds(caplog: pytest.LogCaptureFixture) -> None:
    """Tests that startup preflight retries then succeeds."""
    caplog.set_level(logging.INFO)
    minimum_qb = str(startup_module.MIN_SUPPORTED_QBITTORRENT_VERSION)
    client = FakeVersionProbeClient(
        [
            qbittorrentapi.APIConnectionError("offline"),
            qbittorrentapi.LoginFailed("invalid credentials"),
            minimum_qb,
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
    """Tests that startup preflight success log redacts url credentials."""
    caplog.set_level(logging.INFO)
    minimum_qb = str(startup_module.MIN_SUPPORTED_QBITTORRENT_VERSION)
    client = FakeVersionProbeClient([minimum_qb])
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


async def test_startup_preflight_compatible_minimum_versions_passes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tests that startup preflight compatible minimum versions passes."""
    caplog.set_level(logging.INFO)
    minimum_qb = str(startup_module.MIN_SUPPORTED_QBITTORRENT_VERSION)
    minimum_webapi = str(startup_module.MIN_SUPPORTED_WEBAPI_VERSION)
    client = FakeVersionProbeClient(
        [minimum_qb],
        webapi_version_outcomes=[minimum_webapi],
    )
    logger = logging.getLogger("diskguard.startup.test")

    await run_qbittorrent_startup_preflight(
        client,
        qb_url="http://qbittorrent:8080",
        logger=logger,
        max_attempts=3,
    )

    assert client.calls == 1
    assert client.webapi_calls == 1
    assert any(record.levelno == logging.INFO for record in caplog.records)


async def test_startup_preflight_incompatible_versions_fail_with_clear_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tests that startup preflight incompatible versions fail with clear error."""
    caplog.set_level(logging.ERROR)
    minimum_requirement = startup_module._format_minimum_version_requirement()  # noqa: SLF001
    client = FakeVersionProbeClient(
        ["4.0.9"],
        webapi_version_outcomes=["1.9"],
    )
    logger = logging.getLogger("diskguard.startup.test")

    with pytest.raises(StartupPreflightError) as exc_info:
        await run_qbittorrent_startup_preflight(
            client,
            qb_url="http://qbittorrent:8080",
            logger=logger,
            max_attempts=5,
        )

    assert client.calls == 1
    assert client.webapi_calls == 1
    error_message = str(exc_info.value)
    assert "Incompatible qBittorrent API versions detected" in error_message
    assert "qBittorrent=4.0.9" in error_message
    assert "webapi=1.9" in error_message
    assert minimum_requirement in error_message
    assert any(record.levelno == logging.ERROR for record in caplog.records)


async def test_startup_preflight_rejects_pre_add_tags_version_floor(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tests that qB/Web API versions before add-tags support are rejected."""
    caplog.set_level(logging.ERROR)
    minimum_requirement = startup_module._format_minimum_version_requirement()  # noqa: SLF001
    client = FakeVersionProbeClient(
        ["4.1.9"],
        webapi_version_outcomes=["2.2.0"],
    )
    logger = logging.getLogger("diskguard.startup.test")

    with pytest.raises(StartupPreflightError) as exc_info:
        await run_qbittorrent_startup_preflight(
            client,
            qb_url="http://qbittorrent:8080",
            logger=logger,
            max_attempts=2,
        )

    error_message = str(exc_info.value)
    assert "Incompatible qBittorrent API versions detected" in error_message
    assert "qBittorrent=4.1.9" in error_message
    assert "webapi=2.2.0" in error_message
    assert minimum_requirement in error_message


async def test_startup_preflight_missing_webapi_version_endpoint_fails_with_actionable_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tests that startup preflight missing webapi version endpoint fails with actionable error."""
    caplog.set_level(logging.ERROR)
    minimum_qb = str(startup_module.MIN_SUPPORTED_QBITTORRENT_VERSION)
    minimum_requirement = startup_module._format_minimum_version_requirement()  # noqa: SLF001
    client = FakeVersionProbeClient(
        [minimum_qb],
        webapi_version_outcomes=[
            _http_error(
                404,
                "qBittorrent GET http://qbittorrent:8080/api/v2/app/webapiVersion "
                "failed with status 404: Not Found",
            )
        ],
    )
    logger = logging.getLogger("diskguard.startup.test")

    with pytest.raises(StartupPreflightError) as exc_info:
        await run_qbittorrent_startup_preflight(
            client,
            qb_url="http://qbittorrent:8080",
            logger=logger,
            max_attempts=5,
        )

    assert client.calls == 1
    assert client.webapi_calls == 1
    error_message = str(exc_info.value)
    assert "required version endpoints" in error_message
    assert "/api/v2/app/webapiVersion" in error_message
    assert minimum_requirement in error_message
    assert any(record.levelno == logging.ERROR for record in caplog.records)


async def test_startup_preflight_retries_then_fails(caplog: pytest.LogCaptureFixture) -> None:
    """Tests that startup preflight retries then fails."""
    caplog.set_level(logging.WARNING)
    client = FakeVersionProbeClient([qbittorrentapi.APIConnectionError("offline")])
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
    """Tests that startup preflight retry and error logs redact url credentials."""
    caplog.set_level(logging.WARNING)
    client = FakeVersionProbeClient([qbittorrentapi.APIConnectionError("offline")])
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
    """Tests that startup preflight auth failure surfaces explicit auth error."""
    caplog.set_level(logging.WARNING)
    client = FakeVersionProbeClient([qbittorrentapi.LoginFailed("invalid credentials")])
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
