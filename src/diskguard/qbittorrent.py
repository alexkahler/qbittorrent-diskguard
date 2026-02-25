"""qBittorrent Web API client with session and retry handling."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Mapping
from urllib.parse import urlencode

import aiohttp

from diskguard.config import QbittorrentConfig
from diskguard.errors import (
    QbittorrentAuthenticationError,
    QbittorrentRequestError,
    QbittorrentUnavailableError,
)
from diskguard.models import TorrentSnapshot
from diskguard.state import parse_tags


class QbittorrentClient:
    """Thin async qBittorrent API client with bounded auth retry."""

    def __init__(
        self,
        config: QbittorrentConfig,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        """Initializes client configuration and HTTP/session state.

        Args:
            config: qBittorrent connection and timeout settings.
            logger: Optional logger used for client diagnostics.
        """
        self._config = config
        self._logger = logger or logging.getLogger(__name__)
        self._base_url = config.url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None
        self._auth_lock = asyncio.Lock()
        self._logged_in = False
        self._detected_qbittorrent_version: str | None = None
        self._detected_webapi_version: str | None = None

    async def close(self) -> None:
        """Closes the underlying HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()

    async def fetch_torrents(self) -> list[TorrentSnapshot]:
        """Fetches torrents with the fields required by DiskGuard."""
        payload = await self._request("GET", "/api/v2/torrents/info", expect_json=True)
        if not isinstance(payload, list):
            raise QbittorrentRequestError("Unexpected torrents/info payload shape")

        torrents: list[TorrentSnapshot] = []
        for item in payload:
            parsed = _parse_torrent_item(item)
            if parsed is not None:
                torrents.append(parsed)
        return torrents

    async def fetch_torrent_by_hash(self, torrent_hash: str) -> TorrentSnapshot | None:
        """Fetches one torrent by hash from the qBittorrent API."""
        payload = await self._request(
            "GET",
            "/api/v2/torrents/info",
            params={"hashes": torrent_hash},
            expect_json=True,
        )
        if not isinstance(payload, list):
            raise QbittorrentRequestError("Unexpected torrents/info payload shape")

        for item in payload:
            parsed = _parse_torrent_item(item)
            if parsed is None:
                continue
            if parsed.hash == torrent_hash:
                return parsed
        return None

    async def fetch_application_version(self) -> str:
        """Fetches qBittorrent application version via authenticated API call."""
        payload = await self._request("GET", "/api/v2/app/version")
        version = str(payload).strip()
        if not version:
            request_url = self._build_request_url("/api/v2/app/version")
            raise QbittorrentRequestError(
                f"qBittorrent GET {request_url} returned an empty version payload"
                f"{self._format_detected_versions()}"
            )
        return version

    async def fetch_webapi_version(self) -> str:
        """Fetches qBittorrent Web API version via authenticated API call."""
        payload = await self._request("GET", "/api/v2/app/webapiVersion")
        version = str(payload).strip()
        if not version:
            request_url = self._build_request_url("/api/v2/app/webapiVersion")
            raise QbittorrentRequestError(
                f"qBittorrent GET {request_url} returned an empty Web API version payload"
                f"{self._format_detected_versions()}"
            )
        return version

    async def pause_torrent(self, torrent_hash: str) -> None:
        """Pauses a single torrent."""
        await self._request(
            "POST",
            "/api/v2/torrents/stop",
            data={"hashes": torrent_hash},
        )

    async def resume_torrent(self, torrent_hash: str) -> None:
        """Resumes a single torrent."""
        await self._request(
            "POST",
            "/api/v2/torrents/start",
            data={"hashes": torrent_hash},
        )

    async def add_tag(self, torrent_hash: str, tag: str) -> None:
        """Adds a tag to a single torrent."""
        await self._request(
            "POST",
            "/api/v2/torrents/addTags",
            data={"hashes": torrent_hash, "tags": tag},
        )

    async def remove_tag(self, torrent_hash: str, tag: str) -> None:
        """Removes a tag from a single torrent."""
        await self._request(
            "POST",
            "/api/v2/torrents/removeTags",
            data={"hashes": torrent_hash, "tags": tag},
        )

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Returns an active HTTP session, creating one when needed.

        Returns:
            Reusable aiohttp client session configured with bounded timeouts.
        """
        if self._session and not self._session.closed:
            return self._session

        timeout = aiohttp.ClientTimeout(
            total=self._config.total_timeout_seconds,
            connect=self._config.connect_timeout_seconds,
            sock_read=self._config.read_timeout_seconds,
        )
        self._session = aiohttp.ClientSession(timeout=timeout)
        self._logged_in = False
        return self._session

    async def _login(self, *, force: bool = False) -> None:
        """Authenticates the current session against qBittorrent.

        Args:
            force: Re-authenticate even when already marked logged in.

        Raises:
            QbittorrentUnavailableError: If login request could not be sent.
            QbittorrentAuthenticationError: If credentials/session are rejected.
        """
        async with self._auth_lock:
            if self._logged_in and not force:
                return

            session = await self._ensure_session()
            login_url = self._build_endpoint("/api/v2/auth/login")
            try:
                async with session.post(
                    login_url,
                    data={
                        "username": self._config.username,
                        "password": self._config.password,
                    },
                ) as response:
                    self._capture_detected_versions(response.headers)
                    body = await response.text()
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                raise QbittorrentUnavailableError(
                    f"Unable to login to qBittorrent at {login_url}: {exc}"
                ) from exc

            if response.status != 200 or body.strip().lower() != "ok.":
                raise QbittorrentAuthenticationError(
                    f"qBittorrent authentication failed for POST {login_url}"
                    f" (status {response.status})"
                    f"{self._format_detected_versions()}"
                )

            self._logged_in = True

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        expect_json: bool = False,
    ) -> Any:
        """Sends an authenticated qBittorrent API request with one relogin retry.

        Args:
            method: HTTP method.
            path: Relative API path.
            params: Optional query parameters.
            data: Optional form body.
            expect_json: Whether to decode response body as JSON.

        Returns:
            Parsed JSON payload when ``expect_json`` is true; otherwise text body.

        Raises:
            QbittorrentUnavailableError: If request cannot reach qBittorrent.
            QbittorrentRequestError: If response status or payload is invalid.
            QbittorrentAuthenticationError: If auth still fails after relogin.
        """
        request_url = self._build_request_url(path, params=params)
        for attempt in range(2):
            await self._login(force=False)
            session = await self._ensure_session()
            endpoint = self._build_endpoint(path)

            try:
                async with session.request(method, endpoint, params=params, data=data) as response:
                    self._capture_detected_versions(response.headers)
                    if response.status == 403 and attempt == 0:
                        self._logged_in = False
                        await self._login(force=True)
                        continue

                    body = await response.text()
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                self._logged_in = False
                raise QbittorrentUnavailableError(
                    f"qBittorrent request failed for {method} {request_url}: {exc}"
                ) from exc

            if response.status >= 400:
                raise QbittorrentRequestError(
                    f"qBittorrent {method} {request_url} failed with status {response.status}: {body}"
                    f"{self._format_detected_versions()}"
                )

            if expect_json:
                try:
                    return await response.json(content_type=None)
                except Exception as exc:  # noqa: BLE001
                    raise QbittorrentRequestError(
                        f"qBittorrent {method} {request_url} returned invalid JSON"
                        f"{self._format_detected_versions()}"
                    ) from exc

            return body

        raise QbittorrentAuthenticationError(
            f"qBittorrent {method} {request_url} failed after relogin{self._format_detected_versions()}"
        )

    def _build_endpoint(self, path: str) -> str:
        """Builds a canonical API endpoint URL from base URL and path."""
        normalized_path = path if path.startswith("/") else f"/{path}"
        return f"{self._base_url}{normalized_path}"

    def _build_request_url(self, path: str, *, params: Mapping[str, Any] | None = None) -> str:
        """Builds a request URL including a URL-encoded query string."""
        endpoint = self._build_endpoint(path)
        if not params:
            return endpoint
        return f"{endpoint}?{urlencode(params, doseq=True)}"

    def _capture_detected_versions(self, headers: Mapping[str, str]) -> None:
        """Updates detected qBittorrent/WebAPI versions from response headers."""
        qb_header_version = _coerce_optional_string(
            headers.get("X-QBittorrent-Version") or headers.get("X-qBittorrent-Version")
        )
        if qb_header_version:
            self._detected_qbittorrent_version = qb_header_version
        else:
            server_header = _coerce_optional_string(headers.get("Server"))
            if server_header:
                match = re.search(r"qBittorrent/([^\s;]+)", server_header, flags=re.IGNORECASE)
                if match:
                    self._detected_qbittorrent_version = match.group(1)

        webapi_version = _coerce_optional_string(
            headers.get("X-WebAPI-Version")
            or headers.get("X-Webapi-Version")
            or headers.get("X-API-Version")
        )
        if webapi_version:
            self._detected_webapi_version = webapi_version

    def _format_detected_versions(self) -> str:
        """Returns formatted detected version context for error messages."""
        if not self._detected_qbittorrent_version and not self._detected_webapi_version:
            return ""
        qbittorrent_version = self._detected_qbittorrent_version or "unknown"
        webapi_version = self._detected_webapi_version or "unknown"
        return (
            f" (detected versions: qBittorrent={qbittorrent_version}, "
            f"webapi={webapi_version})"
        )


def _coerce_int(value: Any, *, default: int) -> int:
    """Coerces a value to int and falls back to default on parse failure.

    Args:
        value: Raw value to coerce.
        default: Fallback value when coercion fails.

    Returns:
        Parsed integer value or ``default``.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_optional_int(value: Any) -> int | None:
    """Coerces a value to optional int.

    Args:
        value: Raw value from API payload.

    Returns:
        Parsed integer or ``None`` when missing/invalid.
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_optional_string(value: Any) -> str | None:
    """Coerces a value to optional non-empty string.

    Args:
        value: Raw value from API payload.

    Returns:
        Trimmed string or ``None`` when missing/empty.
    """
    if value is None:
        return None
    converted = str(value).strip()
    if not converted:
        return None
    return converted


def _parse_torrent_item(item: Any) -> TorrentSnapshot | None:
    """Parses one torrents/info item into a ``TorrentSnapshot``.

    Args:
        item: Raw API list element.

    Returns:
        Parsed torrent snapshot, or ``None`` when payload is invalid.
    """
    if not isinstance(item, dict):
        return None
    torrent_hash = str(item.get("hash", "")).strip()
    if not torrent_hash:
        return None

    amount_left_raw = item.get("amount_left")
    amount_left = _coerce_optional_int(amount_left_raw)

    return TorrentSnapshot(
        hash=torrent_hash,
        state=str(item.get("state", "")),
        amount_left=amount_left,
        priority=_coerce_int(item.get("priority"), default=0),
        added_on=_coerce_int(item.get("added_on"), default=0),
        tags=parse_tags(_coerce_optional_string(item.get("tags"))),
        name=_coerce_optional_string(item.get("name")),
        category=_coerce_optional_string(item.get("category")),
    )
