"""Polling engine for mode enforcement and resume execution."""

from __future__ import annotations

import asyncio
import logging
import time

import qbittorrentapi

from diskguard.config import AppConfig
from diskguard.errors import DiskProbeError
from diskguard.models import Mode
from diskguard.resume_planner import ResumePlanner
from diskguard.state import (
    classify_mode,
    is_completed_or_seeding_state,
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
        """Initializes mode engine dependencies and transition state.

        Args:
            config: Validated DiskGuard configuration.
            qb_client: qBittorrent API client for torrent/tag operations.
            disk_probe: Disk measurement dependency for mode classification.
            resume_planner: Planner used during NORMAL-mode resume evaluation.
            logger: Optional logger used for tick diagnostics.
        """
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

        paused_tag = self._config.tagging.paused_tag
        soft_tag = self._config.tagging.soft_allowed_tag
        try:
            # qbittorrentapi.Client.torrents_info signature validated via introspection.
            torrents: qbittorrentapi.TorrentInfoList = await asyncio.to_thread(
                self._qb_client.torrents_info
            )
        except qbittorrentapi.APIError as exc:
            self._logger.warning("qBittorrent unavailable; skipping tick: %s", exc)
            return

        paused_hashes: set[str] = set()
        soft_allowed_hashes: set[str] = set()
        paused_resume_candidates: list[qbittorrentapi.TorrentDictionary] = []
        for torrent in torrents:
            hash_value = str(torrent.hash).strip()
            if not hash_value:
                continue
            tags = {part.strip() for part in str(torrent.tags).split(",") if part.strip()}
            if paused_tag in tags:
                paused_hashes.add(hash_value)
                paused_resume_candidates.append(torrent)
            if soft_tag in tags:
                soft_allowed_hashes.add(hash_value)
        paused_resume_torrents = paused_resume_candidates

        cleaned_forced_paused_hashes = await self._cleanup_forced_download_paused_torrents(
            torrents,
            paused_hashes=paused_hashes,
        )
        cleaned_soft_allowed_hashes = await self._cleanup_soft_allowed_completed_torrents(
            torrents,
            soft_allowed_hashes=soft_allowed_hashes,
        )

        if self._previous_mode != mode:
            if self._previous_mode is None:
                self._logger.info("DiskGuard mode initialized to %s. Free pct: %.2f", mode.value, disk_stats.free_pct)
            else:
                self._logger.info(
                    "DiskGuard mode transition: %s -> %s. Free pct: %.2f",
                    self._previous_mode.value,
                    mode.value,
                    disk_stats.free_pct,
                )

        if mode is Mode.NORMAL:
            await self._handle_normal_mode(
                torrents,
                disk_stats,
                paused_hashes=paused_hashes,
                soft_allowed_hashes=soft_allowed_hashes,
                cleaned_forced_paused_hashes=cleaned_forced_paused_hashes,
                cleaned_soft_allowed_hashes=cleaned_soft_allowed_hashes,
                paused_resume_torrents=paused_resume_torrents,
            )
        elif mode is Mode.SOFT:
            entering_soft = self._previous_mode is Mode.NORMAL
            await self._handle_soft_mode(
                torrents,
                entering_soft=entering_soft,
                paused_hashes=paused_hashes,
                soft_allowed_hashes=soft_allowed_hashes,
                cleaned_soft_allowed_hashes=cleaned_soft_allowed_hashes,
            )
        else:
            entering_hard = self._previous_mode in {Mode.NORMAL, Mode.SOFT}
            await self._handle_hard_mode(
                torrents,
                entering_hard=entering_hard,
                paused_hashes=paused_hashes,
                soft_allowed_hashes=soft_allowed_hashes,
                cleaned_soft_allowed_hashes=cleaned_soft_allowed_hashes,
            )

        self._previous_mode = mode

    async def _cleanup_soft_allowed_completed_torrents(
        self,
        torrents: qbittorrentapi.TorrentInfoList,
        *,
        soft_allowed_hashes: set[str],
    ) -> set[str]:
        """Removes soft_allowed from torrents that are now completed/seeding."""
        downloading_states = self._config.disk.downloading_states
        removed_hashes: set[str] = set()

        for torrent in torrents:
            hash_value = str(torrent.hash).strip()
            state_value = str(torrent.state)
            if not hash_value:
                continue
            if hash_value not in soft_allowed_hashes:
                continue
            if not is_completed_or_seeding_state(state_value, downloading_states):
                continue
            try:
                await asyncio.to_thread(
                    self._qb_client.torrents_remove_tags,
                    tags=self._config.tagging.soft_allowed_tag,
                    torrent_hashes=hash_value,
                )
                soft_allowed_hashes.discard(hash_value)
                self._logger.info("Removed soft_allowed from %s (now seeding/completed)", hash_value)
                removed_hashes.add(hash_value)
            except qbittorrentapi.APIError as exc:
                self._logger.warning(
                    "Failed to remove tag %s from torrent %s: %s",
                    self._config.tagging.soft_allowed_tag,
                    hash_value,
                    exc,
                )
        return removed_hashes

    async def _cleanup_forced_download_paused_torrents(
        self,
        torrents: qbittorrentapi.TorrentInfoList,
        *,
        paused_hashes: set[str],
    ) -> set[str]:
        """Removes paused tag from forced downloads that users resumed manually.

        Args:
            torrents: Current torrent snapshots.

        Returns:
            Hashes where paused-tag cleanup succeeded this tick.
        """
        forced_hashes: list[str] = []
        for torrent in torrents:
            hash_value = str(torrent.hash).strip()
            state_value = str(torrent.state)
            if not hash_value:
                continue
            if hash_value not in paused_hashes:
                continue
            if not is_forced_download_state(state_value):
                continue
            forced_hashes.append(hash_value)

        removed_hashes = await self._remove_tag_from_hashes(
            tag=self._config.tagging.paused_tag,
            torrent_hashes=forced_hashes,
            reason="forcedDL_user_override",
        )
        for hash_value in removed_hashes:
            paused_hashes.discard(hash_value)
        return removed_hashes

    async def _handle_normal_mode(
        self,
        torrents: qbittorrentapi.TorrentInfoList,
        disk_stats,
        *,
        paused_hashes: set[str],
        soft_allowed_hashes: set[str],
        cleaned_forced_paused_hashes: set[str],
        cleaned_soft_allowed_hashes: set[str],
        paused_resume_torrents: list[qbittorrentapi.TorrentDictionary],
    ) -> None:
        """Applies NORMAL-mode cleanup and resume planner execution.

        Args:
            torrents: Current torrent snapshots.
            disk_stats: Current disk measurements used by resume planner.
            cleaned_forced_paused_hashes: Hashes where forcedDL paused-tag
                cleanup already succeeded this tick.
            cleaned_soft_allowed_hashes: Hashes already cleaned during
                completed/seeding soft-allowed cleanup in this tick.
        """
        known_hashes = {
            hash_value
            for hash_value in (str(torrent.hash).strip() for torrent in torrents)
            if hash_value
        }
        normal_cleanup_hashes = sorted(
            (soft_allowed_hashes - cleaned_soft_allowed_hashes) & known_hashes
        )
        removed_soft_allowed = await self._remove_tag_from_hashes(
            tag=self._config.tagging.soft_allowed_tag,
            torrent_hashes=normal_cleanup_hashes,
            reason="normal_cleanup",
        )
        soft_allowed_hashes.difference_update(removed_soft_allowed)

        # FIXME: Is this code duplicated? Can we refactor out to a helper?
        self_heal_hashes: list[str] = []
        for torrent in torrents:
            hash_value = str(torrent.hash).strip()
            state_value = str(torrent.state)
            if not hash_value:
                continue
            if hash_value not in paused_hashes:
                continue
            if hash_value in cleaned_forced_paused_hashes:
                continue
            if is_paused_download_state(state_value):
                continue
            self_heal_hashes.append(hash_value)
        removed_paused = await self._remove_tag_from_hashes(
            tag=self._config.tagging.paused_tag,
            torrent_hashes=self_heal_hashes,
            reason="self_heal",
        )
        paused_hashes.difference_update(removed_paused)

        await self._resume_planner.execute(
            torrents,
            disk_stats,
            paused_hashes=paused_hashes,
            paused_torrents=paused_resume_torrents,
        )

    async def _handle_soft_mode(
        self,
        torrents: qbittorrentapi.TorrentInfoList,
        *,
        entering_soft: bool,
        paused_hashes: set[str],
        soft_allowed_hashes: set[str],
        cleaned_soft_allowed_hashes: set[str],
    ) -> None:
        """Applies SOFT-mode transition and steady-state enforcement rules.

        Args:
            torrents: Current torrent snapshots.
            entering_soft: Whether this tick is a NORMAL -> SOFT transition.
            cleaned_soft_allowed_hashes: Hashes already cleaned during
                completed/seeding soft-allowed cleanup in this tick.
        """
        downloading_states = self._config.disk.downloading_states

        known_soft_allowed = {
            hash_value
            for hash_value in soft_allowed_hashes
            if hash_value not in cleaned_soft_allowed_hashes
        }

        if entering_soft:
            for torrent in torrents:
                hash_value = str(torrent.hash).strip()
                state_value = str(torrent.state)
                if not hash_value:
                    continue
                if hash_value in paused_hashes:
                    continue
                if is_forced_download_state(state_value):
                    continue
                if not is_downloading_ish_state(state_value, downloading_states):
                    continue
                if hash_value in soft_allowed_hashes:
                    continue
                try:
                    await asyncio.to_thread(
                        self._qb_client.torrents_add_tags,
                        tags=self._config.tagging.soft_allowed_tag,
                        torrent_hashes=hash_value,
                    )
                    soft_allowed_hashes.add(hash_value)
                    self._logger.info(
                        "Added tag %s to torrent %s",
                        self._config.tagging.soft_allowed_tag,
                        hash_value,
                    )
                    known_soft_allowed.add(hash_value)
                except qbittorrentapi.APIError as exc:
                    self._logger.warning(
                        "Failed to add tag %s to torrent %s: %s",
                        self._config.tagging.soft_allowed_tag,
                        hash_value,
                        exc,
                    )

        # FIXME Can we refactor out into helper since it is same as in HARD mode?
        for torrent in torrents:
            hash_value = str(torrent.hash).strip()
            state_value = str(torrent.state)
            amount_left = torrent.amount_left
            if not hash_value:
                continue
            if amount_left is None or amount_left <= 0:
                continue
            if is_forced_download_state(state_value):
                continue
            if not is_downloading_ish_state(state_value, downloading_states):
                continue
            if hash_value in known_soft_allowed:
                continue
            await self._pause_and_mark(hash_value, paused_hashes=paused_hashes)

    async def _handle_hard_mode(
        self,
        torrents: qbittorrentapi.TorrentInfoList,
        *,
        entering_hard: bool,
        paused_hashes: set[str],
        soft_allowed_hashes: set[str],
        cleaned_soft_allowed_hashes: set[str],
    ) -> None:
        """Applies HARD-mode cleanup and full downloader pause enforcement.

        Args:
            torrents: Current torrent snapshots.
            entering_hard: Whether this tick entered HARD mode from NORMAL/SOFT.
            cleaned_soft_allowed_hashes: Hashes already cleaned during
                completed/seeding soft-allowed cleanup in this tick.
        """
        downloading_states = self._config.disk.downloading_states

        if entering_hard:
            self._logger.debug("Applying HARD transition rules")

        for torrent in torrents:
            hash_value = str(torrent.hash).strip()
            if not hash_value:
                continue
            if hash_value in cleaned_soft_allowed_hashes:
                continue
            if hash_value in soft_allowed_hashes:
                try:
                    await asyncio.to_thread(
                        self._qb_client.torrents_remove_tags,
                        tags=self._config.tagging.soft_allowed_tag,
                        torrent_hashes=hash_value,
                    )
                    soft_allowed_hashes.discard(hash_value)
                    self._logger.info(
                        "Removed tag %s from torrent %s (%s)",
                        self._config.tagging.soft_allowed_tag,
                        hash_value,
                        "hard_cleanup",
                    )
                except qbittorrentapi.APIError as exc:
                    self._logger.warning(
                        "Failed to remove tag %s from torrent %s: %s",
                        self._config.tagging.soft_allowed_tag,
                        hash_value,
                        exc,
                    )

        hashes_to_pause: list[str] = []
        for torrent in torrents:
            hash_value = str(torrent.hash).strip()
            state_value = str(torrent.state)
            amount_left = torrent.amount_left
            if not hash_value:
                continue
            if amount_left is None or amount_left <= 0:
                continue
            if is_forced_download_state(state_value):
                continue
            if not is_downloading_ish_state(state_value, downloading_states):
                continue
            hashes_to_pause.append(hash_value)

        await self._pause_and_mark_many(hashes_to_pause, paused_hashes=paused_hashes)

    async def _pause_and_mark(
        self,
        torrent_hash: str,
        *,
        paused_hashes: set[str] | None = None,
    ) -> None:
        """Pauses a torrent and applies DiskGuard's paused management tag.

        Args:
            torrent_hash: Hash of the torrent to pause and mark.
        """
        paused_tag = self._config.tagging.paused_tag
        try:
            await asyncio.to_thread(self._qb_client.torrents_pause, torrent_hashes=torrent_hash)
            self._logger.info("Paused torrent %s", torrent_hash)
        except qbittorrentapi.APIError as exc:
            self._logger.warning("Failed to pause torrent %s: %s", torrent_hash, exc)
            return

        try:
            await asyncio.to_thread(
                self._qb_client.torrents_add_tags,
                tags=paused_tag,
                torrent_hashes=torrent_hash,
            )
            if paused_hashes is not None:
                paused_hashes.add(torrent_hash)
        except qbittorrentapi.APIError as exc:
            self._logger.warning(
                "Torrent %s paused but failed to add tag %s: %s",
                torrent_hash,
                paused_tag,
                exc,
            )

    async def _pause_and_mark_many(
        self,
        torrent_hashes: list[str],
        *,
        paused_hashes: set[str] | None = None,
    ) -> None:
        """Pauses and tags multiple torrents with a batch-first strategy."""
        if not torrent_hashes:
            return

        try:
            # qbittorrentapi.Client.torrents_pause signature validated via introspection.
            await asyncio.to_thread(
                self._qb_client.torrents_pause,
                torrent_hashes=torrent_hashes,
            )
            for torrent_hash in torrent_hashes:
                self._logger.info("Paused torrent %s", torrent_hash)
        except qbittorrentapi.APIError:
            for torrent_hash in torrent_hashes:
                await self._pause_and_mark(torrent_hash, paused_hashes=paused_hashes)
            return

        paused_tag = self._config.tagging.paused_tag
        try:
            # qbittorrentapi.Client.torrents_add_tags signature validated via introspection.
            await asyncio.to_thread(
                self._qb_client.torrents_add_tags,
                tags=paused_tag,
                torrent_hashes=torrent_hashes,
            )
            if paused_hashes is not None:
                paused_hashes.update(torrent_hashes)
        except qbittorrentapi.APIError:
            for torrent_hash in torrent_hashes:
                try:
                    await asyncio.to_thread(
                        self._qb_client.torrents_add_tags,
                        tags=paused_tag,
                        torrent_hashes=torrent_hash,
                    )
                    if paused_hashes is not None:
                        paused_hashes.add(torrent_hash)
                except qbittorrentapi.APIError as exc:
                    self._logger.warning(
                        "Torrent %s paused but failed to add tag %s: %s",
                        torrent_hash,
                        paused_tag,
                        exc,
                    )

    async def _remove_tag_from_hashes(
        self,
        *,
        tag: str,
        torrent_hashes: list[str],
        reason: str,
    ) -> set[str]:
        """Removes a tag from hashes with batch-first fallback to per-torrent calls."""
        if not torrent_hashes:
            return set()

        removed_hashes: set[str] = set()
        try:
            # qbittorrentapi.Client.torrents_remove_tags signature validated via introspection.
            await asyncio.to_thread(
                self._qb_client.torrents_remove_tags,
                tags=tag,
                torrent_hashes=torrent_hashes,
            )
            removed_hashes.update(torrent_hashes)
        except qbittorrentapi.APIError:
            for torrent_hash in torrent_hashes:
                try:
                    await asyncio.to_thread(
                        self._qb_client.torrents_remove_tags,
                        tags=tag,
                        torrent_hashes=torrent_hash,
                    )
                    removed_hashes.add(torrent_hash)
                except qbittorrentapi.APIError as exc:
                    self._logger.warning(
                        "Failed to remove tag %s from torrent %s: %s",
                        tag,
                        torrent_hash,
                        exc,
                    )

        for torrent_hash in removed_hashes:
            self._logger.info(
                "Removed tag %s from torrent %s (%s)",
                tag,
                torrent_hash,
                reason,
            )
        return removed_hashes
