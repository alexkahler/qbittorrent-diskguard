"""HTTP API surface for DiskGuard."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
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
    is_forced_download_state,
)

ON_ADD_AUTH_HEADER = "X-DiskGuard-Token"
TORRENT_HASH_PATTERN = re.compile(r"^(?:[A-Fa-f0-9]{40}|[A-Fa-f0-9]{64})$")


@dataclass
class _QueuedOnAdd:
    """Tracks pending quick-poll metadata for one torrent hash."""

    mode: Mode
    attempts: int = 0


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
        self._quick_poll_queue_by_hash: dict[str, _QueuedOnAdd] = {}
        self._quick_poll_worker_task: asyncio.Task[None] | None = None

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
                {
                    "status": "error",
                    "message": "hash must be a 40 or 64 character hex string",
                },
                status=400,
            )
        torrent_name = _coerce_log_value(payload.get("name"))
        torrent_category = _coerce_log_value(payload.get("category"))

        try:
            disk_stats = self._disk_probe.measure()
        except DiskProbeError as exc:
            self._warn_rate_limited(
                "on_add_disk_probe", "on-add disk probe failed: %s", exc
            )
            return web.json_response(
                {"status": "accepted", "action": "deferred"}, status=202
            )

        mode = classify_mode(
            disk_stats.free_pct,
            soft_pause_below_pct=self._config.disk.soft_pause_below_pct,
            hard_pause_below_pct=self._config.disk.hard_pause_below_pct,
        )
        free_gb = disk_stats.free_bytes / (1024**3)
        used_bytes = max(disk_stats.total_bytes - disk_stats.free_bytes, 0)
        used_gb = used_bytes / (1024**3)
        used_pct = max(100.0 - disk_stats.free_pct, 0.0)

        message = "on_add triggered hash=%s mode=%s free_gb=%.2f used_gb=%.2f free_pct=%.2f used_pct=%.2f"
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
            return web.json_response(
                {"status": "ok", "action": "none", "mode": mode.value}, status=200
            )

        # Keep the endpoint non-blocking: quick-poll + pause/tag executes in background.
        if torrent_hash in self._quick_poll_queue_by_hash:
            return web.json_response(
                {
                    "status": "accepted",
                    "action": "quick_poll_already_scheduled",
                    "mode": mode.value,
                },
                status=202,
            )
        if (
            len(self._quick_poll_queue_by_hash)
            >= self._config.polling.on_add_quick_poll_max_queue_size
        ):
            self._warn_rate_limited(
                "on_add_pending_limit",
                "on-add pending task limit reached; rejecting torrent %s",
                torrent_hash,
            )
            return web.json_response(
                {"status": "error", "message": "on-add backlog limit reached"},
                status=429,
            )

        self._quick_poll_queue_by_hash[torrent_hash] = _QueuedOnAdd(mode=mode)
        self._ensure_quick_poll_worker_running()
        return web.json_response(
            {
                "status": "accepted",
                "action": "quick_poll_pause_and_mark",
                "mode": mode.value,
            },
            status=202,
        )

    async def shutdown(self) -> None:
        """Awaits in-flight on-add background tasks."""
        worker_task = self._quick_poll_worker_task
        if worker_task is None:
            return
        await asyncio.gather(worker_task, return_exceptions=True)

    async def _read_payload(self, request: web.Request) -> dict[str, str]:
        """Reads form and query payload values from a request.

        Args:
            request: Incoming aiohttp request.

        Returns:
            A merged payload dictionary where query params fill missing form keys.
        """
        payload: dict[str, str] = {}
        posted_items: list[tuple[str, object]] = []
        if request.can_read_body:
            try:
                posted = await request.post()
                posted_items = [(str(key), value) for key, value in posted.items()]
            except web.HTTPRequestEntityTooLarge:
                raise
            except Exception:  # noqa: BLE001
                posted_items = []
            for key, value in posted_items:
                payload[key] = str(value)

        for key, value in request.query.items():
            payload.setdefault(str(key), str(value))
        return payload

    def _ensure_quick_poll_worker_running(self) -> None:
        """Starts quick-poll worker if one is not currently active."""
        worker_task = self._quick_poll_worker_task
        if worker_task is None or worker_task.done():
            self._quick_poll_worker_task = asyncio.create_task(self._quick_poll_worker())

    async def _quick_poll_worker(self) -> None:
        """Processes queued on-add hashes via single batched quick-poll loop.

        The worker guarantees at most one polling loop at a time. New hashes can be
        appended while the worker runs and will be picked up in subsequent iterations.
        """
        current_task = asyncio.current_task()
        max_attempts = self._config.polling.on_add_quick_poll_max_attempts
        interval_seconds = self._config.polling.on_add_quick_poll_interval_seconds
        try:
            # Debounce the initial fetch so bursty on-add callbacks coalesce into
            # a single first quick-poll batch.
            await asyncio.sleep(interval_seconds)
            while self._quick_poll_queue_by_hash:
                polled_hashes = list(self._quick_poll_queue_by_hash)

                payload_loaded = False
                payload_by_hash: dict[str, object] = {}
                try:
                    # qbittorrentapi.Client.torrents_info(...)
                    payload = await asyncio.to_thread(
                        self._qb_client.torrents_info,
                        torrent_hashes=polled_hashes,
                    )
                    payload_loaded = True
                    payload_by_hash = {
                        str(torrent.hash).strip(): torrent
                        for torrent in payload
                        if str(torrent.hash).strip()
                    }
                except Exception as exc:  # noqa: BLE001
                    self._warn_rate_limited(
                        "on_add_qb_failure",
                        "on-add quick poll failed for %d torrents in batch: %s",
                        len(polled_hashes),
                        exc,
                    )

                known_size_hashes_to_pause: list[str] = []
                mode_by_hash: dict[str, Mode] = {}
                for torrent_hash in polled_hashes:
                    queued = self._quick_poll_queue_by_hash.get(torrent_hash)
                    if queued is None:
                        continue

                    torrent_dict = payload_by_hash.get(torrent_hash)
                    if payload_loaded and torrent_dict is None:
                        self._quick_poll_queue_by_hash.pop(torrent_hash, None)
                        continue
                    if torrent_dict is None:
                        continue

                    amount_left = getattr(torrent_dict, "amount_left", None)
                    if amount_left is None or amount_left <= 0:
                        continue

                    state_value = str(getattr(torrent_dict, "state", ""))
                    if is_forced_download_state(state_value):
                        self._logger.debug(
                            "on-add quick poll skipping forcedDL torrent %s in %s mode",
                            torrent_hash,
                            queued.mode.value,
                        )
                        self._quick_poll_queue_by_hash.pop(torrent_hash, None)
                        continue

                    # Keep hashes queued while pause/tag runs so duplicate /on-add
                    # requests are still deduped during in-flight enforcement.
                    known_size_hashes_to_pause.append(torrent_hash)
                    mode_by_hash[torrent_hash] = queued.mode

                if known_size_hashes_to_pause:
                    try:
                        await self._pause_and_mark_many(
                            known_size_hashes_to_pause,
                            mode_by_hash=mode_by_hash,
                        )
                    except Exception as exc:  # noqa: BLE001
                        self._warn_rate_limited(
                            "on_add_qb_failure",
                            "on-add pause/tag failed for %d queued torrents: %s",
                            len(known_size_hashes_to_pause),
                            exc,
                        )
                    else:
                        for torrent_hash in known_size_hashes_to_pause:
                            self._quick_poll_queue_by_hash.pop(torrent_hash, None)

                # Attempt accounting applies only to hashes still queued from this batch.
                for torrent_hash in polled_hashes:
                    queued = self._quick_poll_queue_by_hash.get(torrent_hash)
                    if queued is None:
                        continue
                    queued.attempts += 1
                    if queued.attempts >= max_attempts:
                        self._quick_poll_queue_by_hash.pop(torrent_hash, None)

                if self._quick_poll_queue_by_hash:
                    await asyncio.sleep(interval_seconds)
        finally:
            # Clear worker reference only if it still points at this task.
            if self._quick_poll_worker_task is current_task:
                self._quick_poll_worker_task = None

    async def _pause_and_mark_many(
        self,
        torrent_hashes: list[str],
        *,
        mode_by_hash: dict[str, Mode] | None = None,
    ) -> None:
        """Pauses and tags multiple torrents using batch API calls only."""
        if not torrent_hashes:
            return
        deduped_hashes = list(set(torrent_hashes))
        try:
            await asyncio.to_thread(
                self._qb_client.torrents_pause,
                torrent_hashes=deduped_hashes,
            )
        except qbittorrentapi.APIError as exc:
            self._warn_rate_limited(
                "on_add_qb_failure",
                "on-add failed to pause %d torrents in batch: %s",
                len(deduped_hashes),
                exc,
            )
            return

        paused_tag = self._config.tagging.paused_tag
        try:
            await asyncio.to_thread(
                self._qb_client.torrents_add_tags,
                tags=paused_tag,
                torrent_hashes=deduped_hashes,
            )
            for torrent_hash in deduped_hashes:
                mode = (
                    mode_by_hash.get(torrent_hash) if mode_by_hash is not None else None
                )
                if mode is None:
                    self._logger.debug(
                        "on-add paused torrent %s and added tag %s",
                        torrent_hash,
                        paused_tag,
                    )
                else:
                    self._logger.debug(
                        "on-add paused torrent %s and added tag %s in %s mode",
                        torrent_hash,
                        paused_tag,
                        mode.value,
                    )
        except qbittorrentapi.APIError as exc:
            self._warn_rate_limited(
                "on_add_qb_failure",
                "on-add paused %d torrents but failed to add tag %s in batch: %s",
                len(deduped_hashes),
                paused_tag,
                exc,
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
