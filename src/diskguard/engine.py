"""Polling engine for mode enforcement and resume execution."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
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
            self._logger.error(
                "Disk probe failed for %s: %s", self._config.disk.watch_path, exc
            )
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
        
        # Exit early if we're in NORMAL mode but have no managed-tagged torrents to enforce on.
        if mode is Mode.NORMAL and self._previous_mode is not None:
            try:
                has_managed_tagged_torrents = (
                    await self._has_managed_tagged_torrents_for_normal_mode(
                        paused_tag=paused_tag,
                        soft_tag=soft_tag,
                    )
                )
            except qbittorrentapi.APIError as exc:
                self._logger.warning("qBittorrent unavailable; skipping tick: %s", exc)
                return
            if not has_managed_tagged_torrents:
                self._log_mode_transition_if_changed(
                    mode=mode,
                    free_pct=disk_stats.free_pct,
                )
                self._previous_mode = mode
                return

        try:
            # qbittorrentapi.Client.torrents_info(...)
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
            tags = {
                part.strip() for part in str(torrent.tags).split(",") if part.strip()
            }
            if paused_tag in tags:
                paused_hashes.add(hash_value)
                paused_resume_candidates.append(torrent)
            if soft_tag in tags:
                soft_allowed_hashes.add(hash_value)
                
        paused_hashes = await self._cleanup_forced_download_paused_torrents(
            torrents,
            paused_hashes=paused_hashes,
        )
        soft_allowed_hashes = await self._cleanup_soft_allowed_completed_torrents(
            torrents,
            soft_allowed_hashes=soft_allowed_hashes,
        )

        self._log_mode_transition_if_changed(mode=mode, free_pct=disk_stats.free_pct)

        if mode is Mode.NORMAL:
            # Remove soft_allowed tags in NORMAL mode since they're only relevant for SOFT-mode allowlisting.
            await self._remove_tag_from_hashes(
                tag=self._config.tagging.soft_allowed_tag,
                torrent_hashes=list(soft_allowed_hashes),
                reason="normal_cleanup",
            )
            
            await self._handle_normal_mode(
                torrents,
                disk_stats,
                paused_hashes=paused_hashes,
                paused_resume_torrents=paused_resume_candidates,
            )
        elif mode is Mode.SOFT:
            entering_soft = self._previous_mode is Mode.NORMAL
            await self._handle_soft_mode(
                torrents,
                entering_soft=entering_soft,
                paused_hashes=paused_hashes,
                soft_allowed_hashes=soft_allowed_hashes,
            )
        else:
            entering_hard = self._previous_mode in {Mode.NORMAL, Mode.SOFT}
            await self._handle_hard_mode(
                torrents,
                entering_hard=entering_hard,
                soft_allowed_hashes=soft_allowed_hashes,
            )

        self._previous_mode = mode

    async def _has_managed_tagged_torrents_for_normal_mode(
        self,
        *,
        paused_tag: str,
        soft_tag: str,
    ) -> bool:
        """Returns whether NORMAL mode has any managed-tagged torrents.

        Args:
            paused_tag: Tag name used for DiskGuard-managed pauses.
            soft_tag: Tag name used for SOFT-mode allowlist behavior.

        Returns:
            True when at least one torrent carries either managed tag.
        """
        # qbittorrentapi.Client.torrents_info(...)
        paused_tagged_torrents = await asyncio.to_thread(
            self._qb_client.torrents_info,
            tag=paused_tag,
        )
        if paused_tagged_torrents:
            return True
        if soft_tag == paused_tag:
            return False

        # qbittorrentapi.Client.torrents_info(...)
        soft_tagged_torrents = await asyncio.to_thread(
            self._qb_client.torrents_info,
            tag=soft_tag,
        )
        return bool(soft_tagged_torrents)

    def _log_mode_transition_if_changed(self, *, mode: Mode, free_pct: float) -> None:
        """Logs mode initialization/transition only when mode changed.

        Args:
            mode: Current mode for this tick.
            free_pct: Current free disk percentage.
        """
        if self._previous_mode == mode:
            return
        if self._previous_mode is None:
            self._logger.info(
                "DiskGuard mode initialized to %s. Free pct: %.2f",
                mode.value,
                free_pct,
            )
            return
        self._logger.info(
            "DiskGuard mode transition: %s -> %s. Free pct: %.2f",
            self._previous_mode.value,
            mode.value,
            free_pct,
        )

    async def _cleanup_soft_allowed_completed_torrents(
        self,
        torrents: qbittorrentapi.TorrentInfoList,
        *,
        soft_allowed_hashes: set[str],
    ) -> set[str]:
        """Removes soft_allowed from torrents that are now completed/seeding.

        Args:
            torrents: Current torrent snapshots.
            soft_allowed_hashes: Current in-memory hashes carrying soft_allowed.

        Returns:
            The updated soft-allowed hash set after cleanup.
        """
        downloading_states = self._config.disk.downloading_states
        cleanup_hashes: list[str] = []
        for torrent in torrents:
            hash_value = str(torrent.hash).strip()
            state_value = str(torrent.state)
            if not hash_value:
                continue
            if hash_value not in soft_allowed_hashes:
                continue
            if not is_completed_or_seeding_state(state_value, downloading_states):
                continue
            cleanup_hashes.append(hash_value)

        removed_hashes = await self._remove_tag_from_hashes(
            tag=self._config.tagging.soft_allowed_tag,
            torrent_hashes=cleanup_hashes,
            reason="completed_or_seeding",
        )
        for hash_value in removed_hashes:
            soft_allowed_hashes.discard(hash_value)
        for hash_value in cleanup_hashes:
            if hash_value in removed_hashes:
                self._logger.info(
                    "Removed soft_allowed from %s (now seeding/completed)", hash_value
                )
        return soft_allowed_hashes

    async def _cleanup_forced_download_paused_torrents(
        self,
        torrents: qbittorrentapi.TorrentInfoList,
        *,
        paused_hashes: set[str],
    ) -> set[str]:
        """Removes paused tag from forced downloads that users resumed manually.

        Args:
            torrents: Current torrent snapshots.
            paused_hashes: Current in-memory hashes carrying paused tag.

        Returns:
            The updated paused-hash set after successful forcedDL cleanup.
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
        return paused_hashes

    async def _handle_normal_mode(
        self,
        torrents: qbittorrentapi.TorrentInfoList,
        disk_stats,
        *,
        paused_hashes: set[str],
        paused_resume_torrents: list[qbittorrentapi.TorrentDictionary],
    ) -> None:
        """Applies NORMAL-mode cleanup and resume planner execution.

        Args:
            torrents: Current torrent snapshots.
            disk_stats: Current disk measurements used by resume planner.
        """

        # Self-heal any torrents that are paused but no longer in paused download states, 
        # likely due to user resuming without tag removal or external changes.
        # This ensures we don't leave such torrents in a paused+tagged state indefinitely, 
        # blocking them from future NORMAL-mode resume attempts.
        self_heal_hashes: list[str] = []
        for torrent in torrents:
            hash_value = str(torrent.hash).strip()
            state_value = str(torrent.state)
            if not hash_value:
                continue
            if hash_value not in paused_hashes:
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
    ) -> None:
        """Applies SOFT-mode transition and steady-state enforcement rules.

        Args:
            torrents: Current torrent snapshots.
            entering_soft: Whether this tick is a NORMAL -> SOFT transition.
        """
        downloading_states = self._config.disk.downloading_states

        known_soft_allowed = set(soft_allowed_hashes)

        if entering_soft:
            transition_hashes: list[str] = []
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
                transition_hashes.append(hash_value)
            added_hashes = await self._add_tag_to_hashes(
                tag=self._config.tagging.soft_allowed_tag,
                torrent_hashes=transition_hashes,
                reason="soft_transition",
            )
            soft_allowed_hashes.update(added_hashes)
            known_soft_allowed.update(added_hashes)

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
            if hash_value in known_soft_allowed:
                continue
            hashes_to_pause.append(hash_value)

        await self._pause_and_mark(hashes_to_pause)

    async def _handle_hard_mode(
        self,
        torrents: qbittorrentapi.TorrentInfoList,
        *,
        entering_hard: bool,
        soft_allowed_hashes: set[str],
    ) -> None:
        """Applies HARD-mode cleanup and full downloader pause enforcement.

        Args:
            torrents: Current torrent snapshots.
            entering_hard: Whether this tick entered HARD mode from NORMAL/SOFT.
        """
        downloading_states = self._config.disk.downloading_states

        if entering_hard:
            self._logger.debug("Applying HARD transition rules")

        # Clean up all known soft-allowed hashes in HARD mode.
        await self._remove_tag_from_hashes(
            tag=self._config.tagging.soft_allowed_tag,
            torrent_hashes=list(soft_allowed_hashes),
            reason="hard_cleanup",
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

        await self._pause_and_mark(hashes_to_pause)

    async def _pause_and_mark(
        self,
        torrent_hashes: Iterable[str],
    ) -> None:
        """Pauses and tags multiple torrents using batch API calls only."""
        deduped_hashes = list(dict.fromkeys(torrent_hashes))
        if not deduped_hashes:
            return

        try:
            # qbittorrentapi.Client.torrents_pause(...)
            await asyncio.to_thread(
                self._qb_client.torrents_pause,
                torrent_hashes=deduped_hashes,
            )
            for torrent_hash in deduped_hashes:
                self._logger.info("Paused torrent %s", torrent_hash)
        except qbittorrentapi.APIError as exc:
            self._logger.warning(
                "Failed to pause %d torrents in batch: %s",
                len(deduped_hashes),
                exc,
            )
            return

        paused_tag = self._config.tagging.paused_tag
        try:
            # qbittorrentapi.Client.torrents_add_tags(...)
            await asyncio.to_thread(
                self._qb_client.torrents_add_tags,
                tags=paused_tag,
                torrent_hashes=deduped_hashes,
            )
        except qbittorrentapi.APIError as exc:
            self._logger.warning(
                "Paused %d torrents but failed to add tag %s in batch: %s",
                len(deduped_hashes),
                paused_tag,
                exc,
            )

    async def _add_tag_to_hashes(
        self,
        *,
        tag: str,
        torrent_hashes: list[str],
        reason: str,
    ) -> set[str]:
        """Adds a tag to hashes using a single batch API call."""
        return await self._update_tag_on_hashes(
            tag=tag,
            torrent_hashes=torrent_hashes,
            reason=reason,
            add=True,
        )

    async def _remove_tag_from_hashes(
        self,
        *,
        tag: str,
        torrent_hashes: list[str],
        reason: str,
    ) -> set[str]:
        """Removes a tag from hashes using a single batch API call."""
        return await self._update_tag_on_hashes(
            tag=tag,
            torrent_hashes=torrent_hashes,
            reason=reason,
            add=False,
        )

    async def _update_tag_on_hashes(
        self,
        *,
        tag: str,
        torrent_hashes: list[str],
        reason: str,
        add: bool,
    ) -> set[str]:
        """Adds or removes a tag from hashes using one batch API call."""
        deduped_hashes = list(dict.fromkeys(torrent_hashes))
        if not deduped_hashes:
            return set()

        action = "add" if add else "remove"
        verb_past = "Added" if add else "Removed"
        preposition = "to" if add else "from"
        try:
            if add:
                # qbittorrentapi.Client.torrents_add_tags(...)
                await asyncio.to_thread(
                    self._qb_client.torrents_add_tags,
                    tags=tag,
                    torrent_hashes=deduped_hashes,
                )
            else:
                # qbittorrentapi.Client.torrents_remove_tags(...)
                await asyncio.to_thread(
                    self._qb_client.torrents_remove_tags,
                    tags=tag,
                    torrent_hashes=deduped_hashes,
                )
        except qbittorrentapi.APIError as exc:
            self._logger.warning(
                "Failed to %s tag %s %s %d torrents in batch: %s",
                action,
                tag,
                preposition,
                len(deduped_hashes),
                exc,
            )
            return set()

        for torrent_hash in deduped_hashes:
            self._logger.info(
                "%s tag %s %s torrent %s (%s)",
                verb_past,
                tag,
                preposition,
                torrent_hash,
                reason,
            )
        return set(deduped_hashes)
