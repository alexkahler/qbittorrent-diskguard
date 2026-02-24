"""Polling engine for mode enforcement and resume execution."""

from __future__ import annotations

import asyncio
import logging
import time

from diskguard.config import AppConfig
from diskguard.errors import DiskProbeError, QbittorrentError
from diskguard.models import Mode, TorrentSnapshot
from diskguard.resume_planner import ResumePlanner
from diskguard.state import (
    classify_mode,
    is_downloading_ish_state,
    is_forced_download_state,
    is_paused_download_state,
)


class ModeEngine:
    """Runs periodic enforcement ticks and maintains mode transition memory."""

    def __init__(
        self,
        config: AppConfig,
        *,
        qb_client,
        disk_probe,
        resume_planner: ResumePlanner,
        logger: logging.Logger | None = None,
    ) -> None:
        self._config = config
        self._qb_client = qb_client
        self._disk_probe = disk_probe
        self._resume_planner = resume_planner
        self._logger = logger or logging.getLogger(__name__)
        self._previous_mode: Mode | None = None

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Runs polling ticks until stop_event is set."""
        interval_seconds = self._config.polling.interval_seconds
        while not stop_event.is_set():
            started = time.monotonic()
            try:
                await self.tick()
            except Exception:  # noqa: BLE001
                self._logger.exception("Unhandled exception in polling tick")

            elapsed = time.monotonic() - started
            delay = max(0.0, interval_seconds - elapsed)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
            except TimeoutError:
                pass

    async def tick(self) -> None:
        """Runs a single mode-detection and enforcement tick."""
        try:
            disk_stats = self._disk_probe.measure()
        except DiskProbeError as exc:
            self._logger.error("Disk probe failed for %s: %s", self._config.disk.watch_path, exc)
            return

        mode = classify_mode(
            disk_stats.free_pct,
            soft_pause_below_pct=self._config.disk.soft_pause_below_pct,
            hard_pause_below_pct=self._config.disk.hard_pause_below_pct,
        )

        self._logger.debug(
            "Tick disk stats: free_pct=%.2f free_bytes=%d total_bytes=%d mode=%s",
            disk_stats.free_pct,
            disk_stats.free_bytes,
            disk_stats.total_bytes,
            mode.value,
        )

        try:
            torrents = await self._qb_client.fetch_torrents()
        except QbittorrentError as exc:
            self._logger.warning("qBittorrent unavailable; skipping tick: %s", exc)
            return

        if self._previous_mode != mode:
            if self._previous_mode is None:
                self._logger.info("DiskGuard mode initialized to %s", mode.value)
            else:
                self._logger.info(
                    "DiskGuard mode transition: %s -> %s",
                    self._previous_mode.value,
                    mode.value,
                )

        if mode is Mode.NORMAL:
            await self._handle_normal_mode(torrents, disk_stats)
        elif mode is Mode.SOFT:
            entering_soft = self._previous_mode is Mode.NORMAL
            await self._handle_soft_mode(torrents, entering_soft=entering_soft)
        else:
            entering_hard = self._previous_mode in {Mode.NORMAL, Mode.SOFT}
            await self._handle_hard_mode(torrents, entering_hard=entering_hard)

        self._previous_mode = mode

    async def _handle_normal_mode(self, torrents: list[TorrentSnapshot], disk_stats) -> None:
        soft_tag = self._config.tagging.soft_allowed_tag
        paused_tag = self._config.tagging.paused_tag

        for torrent in torrents:
            if torrent.has_tag(soft_tag):
                await self._remove_tag(
                    torrent.hash,
                    soft_tag,
                    reason="normal_cleanup",
                )

        for torrent in torrents:
            if not torrent.has_tag(paused_tag):
                continue
            if is_paused_download_state(torrent.state):
                continue
            await self._remove_tag(
                torrent.hash,
                paused_tag,
                reason="self_heal",
            )

        await self._resume_planner.execute(torrents, disk_stats)

    async def _handle_soft_mode(self, torrents: list[TorrentSnapshot], *, entering_soft: bool) -> None:
        soft_tag = self._config.tagging.soft_allowed_tag
        paused_tag = self._config.tagging.paused_tag
        downloading_states = self._config.disk.downloading_states

        known_soft_allowed = {torrent.hash for torrent in torrents if torrent.has_tag(soft_tag)}

        if entering_soft:
            for torrent in torrents:
                if torrent.has_tag(paused_tag):
                    continue
                if is_forced_download_state(torrent.state):
                    continue
                if not is_downloading_ish_state(torrent.state, downloading_states):
                    continue
                if torrent.has_tag(soft_tag):
                    continue
                success = await self._add_tag(torrent.hash, soft_tag)
                if success:
                    known_soft_allowed.add(torrent.hash)

        for torrent in torrents:
            if is_forced_download_state(torrent.state):
                continue
            if not is_downloading_ish_state(torrent.state, downloading_states):
                continue
            if torrent.hash in known_soft_allowed:
                continue
            await self._pause_and_mark(torrent.hash)

    async def _handle_hard_mode(self, torrents: list[TorrentSnapshot], *, entering_hard: bool) -> None:
        soft_tag = self._config.tagging.soft_allowed_tag
        downloading_states = self._config.disk.downloading_states

        if entering_hard:
            self._logger.debug("Applying HARD transition rules")

        for torrent in torrents:
            if torrent.has_tag(soft_tag):
                await self._remove_tag(torrent.hash, soft_tag, reason="hard_cleanup")

        for torrent in torrents:
            if is_forced_download_state(torrent.state):
                continue
            if not is_downloading_ish_state(torrent.state, downloading_states):
                continue
            await self._pause_and_mark(torrent.hash)

    async def _pause_and_mark(self, torrent_hash: str) -> None:
        paused_tag = self._config.tagging.paused_tag
        try:
            await self._qb_client.pause_torrent(torrent_hash)
            self._logger.info("Paused torrent %s", torrent_hash)
        except QbittorrentError as exc:
            self._logger.warning("Failed to pause torrent %s: %s", torrent_hash, exc)
            return

        try:
            await self._qb_client.add_tag(torrent_hash, paused_tag)
        except QbittorrentError as exc:
            self._logger.warning(
                "Torrent %s paused but failed to add tag %s: %s",
                torrent_hash,
                paused_tag,
                exc,
            )

    async def _add_tag(self, torrent_hash: str, tag: str) -> bool:
        try:
            await self._qb_client.add_tag(torrent_hash, tag)
            self._logger.info("Added tag %s to torrent %s", tag, torrent_hash)
            return True
        except QbittorrentError as exc:
            self._logger.warning("Failed to add tag %s to torrent %s: %s", tag, torrent_hash, exc)
            return False

    async def _remove_tag(self, torrent_hash: str, tag: str, *, reason: str) -> bool:
        try:
            await self._qb_client.remove_tag(torrent_hash, tag)
            self._logger.info("Removed tag %s from torrent %s (%s)", tag, torrent_hash, reason)
            return True
        except QbittorrentError as exc:
            self._logger.warning("Failed to remove tag %s from torrent %s: %s", tag, torrent_hash, exc)
            return False
