"""Startup preflight checks run before DiskGuard begins normal operations."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from urllib.parse import urlparse, urlunparse

from packaging.version import InvalidVersion, Version
import qbittorrentapi

from diskguard.errors import StartupPreflightError

DEFAULT_PREFLIGHT_ATTEMPTS = 10
DEFAULT_PREFLIGHT_MAX_BACKOFF_SECONDS = 5.0
DEFAULT_PREFLIGHT_ATTEMPT_TIMEOUT_SECONDS = 2.0
MIN_SUPPORTED_QBITTORRENT_VERSION = Version("4.2.0")
MIN_SUPPORTED_WEBAPI_VERSION = Version("2.3.0")
_VALID_QBITTORRENT_URL_SCHEMES = frozenset({"http", "https"})


def _parse_version_string(version: str) -> Version | None:
    """Parses a version string into a comparable ``packaging.version.Version``."""
    normalized = version.strip()
    if not normalized:
        return None
    if normalized.lower().startswith("v"):
        normalized = normalized[1:]
    try:
        return Version(normalized)
    except InvalidVersion:
        return None

def validate_qbittorrent_url(url: str) -> None:
    """Validates qBittorrent base URL format for startup preflight.

    Args:
        url: Configured qBittorrent base URL.

    Raises:
        StartupPreflightError: If the URL is missing scheme/host or has invalid port syntax.
    """
    parsed = urlparse(url)
    if parsed.scheme not in _VALID_QBITTORRENT_URL_SCHEMES:
        raise StartupPreflightError(
            "Invalid qbittorrent.url: expected an http:// or https:// URL"
        )
    if not parsed.netloc or not parsed.hostname:
        raise StartupPreflightError("Invalid qbittorrent.url: host is required")

    try:
        _ = parsed.port
    except ValueError as exc:
        raise StartupPreflightError("Invalid qbittorrent.url: port must be numeric") from exc

async def run_qbittorrent_startup_preflight(
    qb_client: qbittorrentapi.Client,
    *,
    qb_url: str,
    logger: logging.Logger,
    max_attempts: int = DEFAULT_PREFLIGHT_ATTEMPTS,
    max_backoff_seconds: float = DEFAULT_PREFLIGHT_MAX_BACKOFF_SECONDS,
    attempt_timeout_seconds: float = DEFAULT_PREFLIGHT_ATTEMPT_TIMEOUT_SECONDS,
    sleep_func: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Verifies qBittorrent is reachable and authenticated before startup proceeds.

    Args:
        qb_client: qBittorrent client capable of authenticated version check.
        qb_url: Configured qBittorrent URL for logs and URL validation.
        logger: Service logger.
        max_attempts: Total number of startup attempts.
        max_backoff_seconds: Backoff cap between retries.
        attempt_timeout_seconds: Timeout budget per preflight attempt.
        sleep_func: Async sleep function; injectable for tests.

    Raises:
        StartupPreflightError: If URL is invalid or all attempts fail.
    """
    if max_attempts <= 0:
        raise ValueError("max_attempts must be greater than zero")
    if max_backoff_seconds <= 0:
        raise ValueError("max_backoff_seconds must be greater than zero")
    if attempt_timeout_seconds <= 0:
        raise ValueError("attempt_timeout_seconds must be greater than zero")

    try:
        validate_qbittorrent_url(qb_url)
    except StartupPreflightError as exc:
        logger.error("qBittorrent startup preflight failed: %s", exc)
        raise
    qb_url_for_logs = _redact_url_credentials(qb_url)

    last_error: Exception | None = None
    last_failure_kind = "connection/request error"
    for attempt in range(1, max_attempts + 1):
        attempt_error: Exception | None = None
        failure_kind = "connection/request error"
        try:
            qbittorrent_version, webapi_version = await asyncio.wait_for(
                _fetch_detected_versions(qb_client),
                timeout=attempt_timeout_seconds,
            )
            _validate_minimum_supported_versions(
                qbittorrent_version=qbittorrent_version,
                webapi_version=webapi_version,
            )
        except TimeoutError:
            attempt_error = TimeoutError(
                f"startup preflight request timed out after {attempt_timeout_seconds:.1f}s"
            )
        except qbittorrentapi.UnsupportedQbittorrentVersion as exc:
            unsupported_error = StartupPreflightError(
                "Connected qBittorrent is not fully supported by this qbittorrent-api "
                f"release: {exc}. {_format_minimum_version_requirement()}"
            )
            logger.error("qBittorrent startup preflight failed: %s", unsupported_error)
            raise unsupported_error from exc
        except (qbittorrentapi.LoginFailed, qbittorrentapi.HTTP401Error, qbittorrentapi.HTTP403Error) as exc:
            attempt_error = exc
            failure_kind = "authentication error"
        except qbittorrentapi.HTTPError as exc:
            version_probe_error = StartupPreflightError(
                "Unable to determine qBittorrent compatibility from required version "
                "endpoints (/api/v2/app/version, /api/v2/app/webapiVersion). "
                f"{_format_minimum_version_requirement()} Underlying error: {exc}"
            )
            logger.error("qBittorrent startup preflight failed: %s", version_probe_error)
            raise version_probe_error from exc
        except qbittorrentapi.APIConnectionError as exc:
            attempt_error = exc
        except qbittorrentapi.APIError as exc:
            version_probe_error = StartupPreflightError(
                "Unable to determine qBittorrent compatibility from required version "
                "endpoints (/api/v2/app/version, /api/v2/app/webapiVersion). "
                f"{_format_minimum_version_requirement()} Underlying error: {exc}"
            )
            logger.error("qBittorrent startup preflight failed: %s", version_probe_error)
            raise version_probe_error from exc
        except StartupPreflightError as exc:
            logger.error("qBittorrent startup preflight failed: %s", exc)
            raise

        if attempt_error is not None:
            last_error = attempt_error
            last_failure_kind = failure_kind
            if attempt == max_attempts:
                break

            backoff_seconds = _compute_retry_backoff_seconds(
                attempt=attempt,
                max_backoff_seconds=max_backoff_seconds,
            )
            logger.warning(
                "qBittorrent startup preflight attempt %d/%d failed (%s) for %s: %s; "
                "retrying in %.1fs",
                attempt,
                max_attempts,
                failure_kind,
                qb_url_for_logs,
                attempt_error,
                backoff_seconds,
            )
            await sleep_func(backoff_seconds)
            continue

        logger.info("Connected to qBittorrent at %s", qb_url_for_logs)
        return

    if last_error is None:
        raise StartupPreflightError(
            f"qBittorrent startup preflight failed after {max_attempts} attempts without a captured error"
        )
    logger.error(
        "qBittorrent startup preflight failed after %d attempts (%s) for %s: %s",
        max_attempts,
        last_failure_kind,
        qb_url_for_logs,
        last_error,
    )
    raise StartupPreflightError(
        f"qBittorrent startup preflight failed after {max_attempts} attempts "
        f"({last_failure_kind}): {last_error}"
    ) from last_error


async def _fetch_detected_versions(qb_client: qbittorrentapi.Client) -> tuple[str, str]:
    """Fetches qBittorrent and Web API versions via authenticated endpoints."""
    qbittorrent_version = str(await asyncio.to_thread(qb_client.app_version)).strip()
    webapi_version = str(await asyncio.to_thread(qb_client.app_web_api_version)).strip()
    return qbittorrent_version, webapi_version

def _validate_minimum_supported_versions(*, qbittorrent_version: str, webapi_version: str) -> None:
    """Validates version strings against DiskGuard's minimum supported baseline."""
    parsed_qbittorrent = _parse_version_string(qbittorrent_version)
    parsed_webapi = _parse_version_string(webapi_version)

    if parsed_qbittorrent is None or parsed_webapi is None:
        raise StartupPreflightError(
            "Unable to parse qBittorrent API versions reported by server: "
            f"qBittorrent='{qbittorrent_version}', webapi='{webapi_version}'. "
            f"{_format_minimum_version_requirement()}"
        )

    if (
        parsed_qbittorrent < MIN_SUPPORTED_QBITTORRENT_VERSION
        or parsed_webapi < MIN_SUPPORTED_WEBAPI_VERSION
    ):
        raise StartupPreflightError(
            "Incompatible qBittorrent API versions detected: "
            f"qBittorrent={qbittorrent_version}, webapi={webapi_version}. "
            f"{_format_minimum_version_requirement()}"
        )

def _format_minimum_version_requirement() -> str:
    """Formats the minimum supported qBittorrent/Web API requirement."""
    return (
        "DiskGuard requires qBittorrent >= "
        f"v{MIN_SUPPORTED_QBITTORRENT_VERSION} and Web API >= "
        f"{MIN_SUPPORTED_WEBAPI_VERSION}."
    )


def _compute_retry_backoff_seconds(*, attempt: int, max_backoff_seconds: float) -> float:
    """Computes bounded linear retry backoff for startup preflight warnings."""
    return min(float(attempt), max_backoff_seconds)

def _redact_url_credentials(url: str) -> str:
    """Returns a URL safe for logs by redacting any embedded user credentials."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"

    netloc = host
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    if parsed.username is not None or parsed.password is not None:
        netloc = f"<redacted>@{netloc}"

    return urlunparse(
        (
            parsed.scheme,
            netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )
