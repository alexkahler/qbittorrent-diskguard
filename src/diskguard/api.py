"""HTTP API surface for DiskGuard."""

from __future__ import annotations

import asyncio
import hmac
import logging
import re
import time

from aiohttp import web
import qbittorrentapi

from diskguard.config import AppConfig
from diskguard.errors import DiskProbeError
from diskguard.models import Mode
from diskguard.state import (
    classify_mode,
    is_downloading_ish_state,
    is_forced_download_state,
)

ON_ADD_AUTH_HEADER = "X-DiskGuard-Token"
TORRENT_HASH_PATTERN = re.compile(r"^(?:[A-Fa-f0-9]{40}|[A-Fa-f0-9]{64})$")


class WarningRateLimiter:
    """Small in-memory rate limiter for warning logs.

    Attributes:
        _interval_seconds: Minimum interval between emitted log entries for a key.
        _last_seen_by_key: Monotonic timestamp of the last emitted log per key.
    """

    def __init__(self, interval_seconds: float = 30.0) -> None:
        """Initializes a rate limiter.

        Args:
            interval_seconds: Minimum time between allowed events for the same key.
        """
        self._interval_seconds = interval_seconds
        self._last_seen_by_key: dict[str, float] = {}

    def allow(self, key: str) -> bool:
        """Returns whether a log event should be emitted for a key.

        Args:
            key: Logical bucket key for grouping repeated warnings.

        Returns:
            True when the event should be emitted; otherwise False.
        """
        now = time.monotonic()
        last = self._last_seen_by_key.get(key)
        if last is None or now - last >= self._interval_seconds:
            self._last_seen_by_key[key] = now
            return True
        return False


class OnAddHandler:
    """Handles qBittorrent on-add callbacks."""

    def __init__(
        self,
        config: AppConfig,
        *,
        qb_client,
        disk_probe,
        logger: logging.Logger | None = None,
        warning_rate_limiter: WarningRateLimiter | None = None,
    ) -> None:
        """Initializes the handler.

        Args:
            config: Validated application configuration.
            qb_client: qBittorrent API client.
            disk_probe: Disk probe used to classify current mode.
            logger: Optional logger instance.
            warning_rate_limiter: Optional warning limiter for noisy failures.
        """
        self._config = config
        self._qb_client = qb_client
        self._disk_probe = disk_probe
        self._logger = logger or logging.getLogger(__name__)
        self._warning_rate_limiter = warning_rate_limiter or WarningRateLimiter()
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._on_add_tasks_by_hash: dict[str, asyncio.Task[None]] = {}
        self._quick_poll_semaphore = asyncio.Semaphore(
            self._config.polling.on_add_quick_poll_max_concurrency
        )

    @property
    def max_request_body_bytes(self) -> int:
        """Returns the maximum accepted request body size for /on-add."""
        return self._config.server.on_add_max_body_bytes

    async def handle(self, request: web.Request) -> web.Response:
        """Processes an on-add callback and schedules background work if needed.

        Args:
            request: Incoming aiohttp request containing torrent metadata.

        Returns:
            JSON response describing the action taken.
        """
        if not self._is_authorized(request):
            self._warn_rate_limited(
                "on_add_auth_failure",
                "on-add unauthorized request rejected",
            )
            return web.json_response(
                {"status": "error", "message": "unauthorized"},
                status=401,
            )

        payload = await self._read_payload(request)
        torrent_hash = str(payload.get("hash", "")).strip()
        if not torrent_hash:
            return web.json_response(
                {"status": "error", "message": "hash is required"},
                status=400,
            )
        if not _is_valid_torrent_hash(torrent_hash):
            return web.json_response(
                {"status": "error", "message": "hash must be a 40 or 64 character hex string"},
                status=400,
            )
        torrent_name = _coerce_log_value(payload.get("name"))
        torrent_category = _coerce_log_value(payload.get("category"))

        try:
            disk_stats = self._disk_probe.measure()
        except DiskProbeError as exc:
            self._warn_rate_limited("on_add_disk_probe", "on-add disk probe failed: %s", exc)
            return web.json_response({"status": "accepted", "action": "deferred"}, status=202)

        mode = classify_mode(
            disk_stats.free_pct,
            soft_pause_below_pct=self._config.disk.soft_pause_below_pct,
            hard_pause_below_pct=self._config.disk.hard_pause_below_pct,
        )
        free_gb = disk_stats.free_bytes / (1024**3)
        used_bytes = max(disk_stats.total_bytes - disk_stats.free_bytes, 0)
        used_gb = used_bytes / (1024**3)
        used_pct = max(100.0 - disk_stats.free_pct, 0.0)

        message = (
            "on_add triggered hash=%s mode=%s free_gb=%.2f used_gb=%.2f free_pct=%.2f used_pct=%.2f"
        )
        args: list[object] = [
            torrent_hash,
            mode.value,
            free_gb,
            used_gb,
            disk_stats.free_pct,
            used_pct,
        ]
        if torrent_name is not None:
            message += " name=%s"
            args.append(torrent_name)
        if torrent_category is not None:
            message += " category=%s"
            args.append(torrent_category)
        self._logger.info(message, *args)

        if mode is Mode.NORMAL:
            return web.json_response({"status": "ok", "action": "none", "mode": mode.value}, status=200)

        # Keep the endpoint non-blocking: quick-poll + pause/tag executes in background.
        existing_task = self._on_add_tasks_by_hash.get(torrent_hash)
        if existing_task is not None and not existing_task.done():
            return web.json_response(
                {"status": "accepted", "action": "quick_poll_already_scheduled", "mode": mode.value},
                status=202,
            )
        if self._pending_task_count() >= self._config.polling.on_add_max_pending_tasks:
            self._warn_rate_limited(
                "on_add_pending_limit",
                "on-add pending task limit reached; rejecting torrent %s",
                torrent_hash,
            )
            return web.json_response(
                {"status": "error", "message": "on-add backlog limit reached"},
                status=429,
            )

        #TODO: Consider whether we can consolidate hashes in the quick poll update to avoid spamming API calls when multiple on-add callbacks are received. qbittorrentapi torrents_info support multiple hashes passed as parameter. This would involve tracking in-flight tasks by torrent hash and skipping scheduling new tasks when an existing one is still pending for the same hash.
        task = asyncio.create_task(self._quick_poll_then_pause_and_mark(torrent_hash, mode))
        self._background_tasks.add(task)
        self._on_add_tasks_by_hash[torrent_hash] = task

        def _cleanup_task(done_task: asyncio.Task[None]) -> None:
            """Removes completed quick-poll tasks from tracking collections.

            Args:
                done_task: Background task that has completed execution.
            """
            self._background_tasks.discard(done_task)
            if self._on_add_tasks_by_hash.get(torrent_hash) is done_task:
                self._on_add_tasks_by_hash.pop(torrent_hash, None)

        task.add_done_callback(_cleanup_task)
        return web.json_response(
            {"status": "accepted", "action": "quick_poll_pause_and_mark", "mode": mode.value},
            status=202,
        )

    async def shutdown(self) -> None:
        """Awaits in-flight on-add background tasks."""
        if not self._background_tasks:
            return
        await asyncio.gather(*self._background_tasks, return_exceptions=True)

    async def _read_payload(self, request: web.Request) -> dict[str, str]:
        """Reads form and query payload values from a request.

        Args:
            request: Incoming aiohttp request.

        Returns:
            A merged payload dictionary where query params fill missing form keys.
        """
        payload: dict[str, str] = {}
        if request.can_read_body:
            try:
                posted = await request.post()
            except web.HTTPRequestEntityTooLarge:
                raise
            except Exception:  # noqa: BLE001
                posted = {}
            for key, value in posted.items():
                payload[str(key)] = str(value)

        for key, value in request.query.items():
            payload.setdefault(str(key), str(value))
        return payload

    async def _pause_and_mark(self, torrent_hash: str, mode: Mode) -> None:
        """Pauses a torrent and applies DiskGuard's managed pause tag.

        Args:
            torrent_hash: qBittorrent torrent hash.
            mode: Mode active when on-add callback was processed.
        """
        paused_tag = self._config.tagging.paused_tag
        try:
            await asyncio.to_thread(self._qb_client.torrents_pause, torrent_hashes=torrent_hash)
            await asyncio.to_thread(
                self._qb_client.torrents_add_tags,
                tags=paused_tag,
                torrent_hashes=torrent_hash,
            )
            self._logger.debug(
                "on-add paused torrent %s and added tag %s in %s mode",
                torrent_hash,
                paused_tag,
                mode.value,
            )
        except qbittorrentapi.APIError as exc:
            self._warn_rate_limited(
                "on_add_qb_failure",
                "on-add failed to pause/tag torrent %s: %s",
                torrent_hash,
                exc,
            )

    async def _quick_poll_then_pause_and_mark(self, torrent_hash: str, mode: Mode) -> None:
        """Quick-polls a single torrent until size is known, then pauses/tags it."""
        max_attempts = self._config.polling.on_add_quick_poll_max_attempts
        interval_seconds = self._config.polling.on_add_quick_poll_interval_seconds
        downloading_states = self._config.disk.downloading_states

        async with self._quick_poll_semaphore:
            for attempt in range(1, max_attempts + 1):
                try:
                    payload = await asyncio.to_thread(
                        self._qb_client.torrents_info,
                        torrent_hashes=torrent_hash,
                    )
                    torrent_dict = payload[0] if payload else None
                except qbittorrentapi.APIError as exc:
                    self._warn_rate_limited(
                        "on_add_qb_failure",
                        "on-add quick poll failed for torrent %s: %s",
                        torrent_hash,
                        exc,
                    )
                    return

                if torrent_dict is not None:
                    amount_left = torrent_dict.amount_left
                    state_value = str(torrent_dict.state)
                    if amount_left is not None and amount_left > 0:
                        if is_forced_download_state(state_value):
                            self._logger.debug(
                                "on-add quick poll skipping forcedDL torrent %s in %s mode",
                                torrent_hash,
                                mode.value,
                            )
                            return

                        if is_downloading_ish_state(state_value, downloading_states):
                            await self._pause_and_mark(torrent_hash, mode)
                            return

                if attempt < max_attempts:
                    await asyncio.sleep(interval_seconds)

        self._logger.debug(
            "on-add quick poll exhausted for %s after %d attempts without pausing",
            torrent_hash,
            max_attempts,
        )

    def _warn_rate_limited(self, key: str, message: str, *args: object) -> None:
        """Logs a warning only if rate limiter allows it.

        Args:
            key: Limiter key for grouping repeated warning messages.
            message: Logging format string.
            *args: Positional logging format arguments.
        """
        if self._warning_rate_limiter.allow(key):
            self._logger.warning(message, *args)

    def _pending_task_count(self) -> int:
        """Returns the number of in-flight on-add background tasks."""
        return sum(1 for task in self._background_tasks if not task.done())

    def _is_authorized(self, request: web.Request) -> bool:
        """Returns whether request token matches configured on-add shared secret."""
        provided_token = request.headers.get(ON_ADD_AUTH_HEADER)
        if not provided_token:
            return False
        expected_token = self._config.server.on_add_auth_token
        return hmac.compare_digest(provided_token, expected_token)


def create_http_app(on_add_handler: OnAddHandler) -> web.Application:
    """Builds the aiohttp app and routes.

    Args:
        on_add_handler: Handler object for `/on-add` requests.

    Returns:
        Configured aiohttp application.
    """
    app = web.Application(client_max_size=on_add_handler.max_request_body_bytes)
    app.router.add_post("/on-add", on_add_handler.handle)

    async def _on_shutdown(_: web.Application) -> None:
        """Awaits handler background tasks during application shutdown."""
        await on_add_handler.shutdown()

    app.on_shutdown.append(_on_shutdown)
    return app


def _coerce_log_value(value: object) -> str | None:
    """Normalizes optional payload values for single-line logs."""
    if value is None:
        return None
    normalized = " ".join(str(value).split())
    if not normalized:
        return None
    return normalized


def _is_valid_torrent_hash(torrent_hash: str) -> bool:
    """Returns whether a torrent hash is valid 40-hex or 64-hex form."""
    return bool(TORRENT_HASH_PATTERN.fullmatch(torrent_hash))
