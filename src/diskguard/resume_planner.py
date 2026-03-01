"""Resume planning and execution for DiskGuard NORMAL mode."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
import logging

import qbittorrentapi

from diskguard.config import AppConfig
from diskguard.models import DiskStats, ResumeDecision, ResumePolicy, ResumeSummary
from diskguard.state import (
    calculate_budget,
    is_active_downloader_for_projection,
    is_paused_download_state,
    sort_resume_candidates,
)


class ResumePlanner:
    """Applies projection-based resume logic using tag-truth candidates."""

    def __init__(
        self, config: AppConfig, qb_client, *, logger: logging.Logger | None = None
    ) -> None:
        """Initializes planner dependencies.

        Args:
            config: Validated DiskGuard configuration.
            qb_client: qBittorrent API client used for resume and tag operations.
            logger: Optional logger for planner diagnostics.
        """
        self._config = config
        self._qb_client = qb_client
        self._logger = logger or logging.getLogger(__name__)

    async def execute(
        self,
        torrents: qbittorrentapi.TorrentInfoList,
        disk_stats: DiskStats,
        *,
        paused_hashes: set[str],
        paused_torrents: list[qbittorrentapi.TorrentDictionary] | None = None,
    ) -> ResumeSummary:
        """Plans and executes safe resumes for tagged paused torrents.

        The planner uses only tag-truth candidates provided by ``paused_hashes``
        (and optionally ``paused_torrents``) and projects remaining disk usage
        before deciding which candidates can fit.

        Args:
            torrents: Full torrent snapshot for the current tick.
            disk_stats: Current disk measurements used for projection math.
            paused_hashes: Hashes currently carrying DiskGuard's paused tag.
            paused_torrents: Optional pre-filtered torrent list that already
                carries the paused tag. If omitted, ``torrents`` is used.

        Returns:
            A ``ResumeSummary`` describing projected budget, outcomes, and a
            per-candidate decision trace in planner evaluation order.
        """
        candidate_source: Iterable[qbittorrentapi.TorrentDictionary]
        if paused_torrents is None:
            candidate_source = torrents
        else:
            candidate_source = paused_torrents

        candidates = self._eligible_candidates(
            candidate_source,
            paused_hashes=paused_hashes,
        )
        policy = self._config.resume.policy
        ordered_candidates = sort_resume_candidates(candidates, policy)

        active_remaining = self._calculate_active_remaining(
            torrents,
            paused_hashes=paused_hashes,
        )
        if active_remaining is None:
            self._logger.warning(
                "Skipping resumes because active downloader remaining size is unknown"
            )
            unknown_decisions: list[ResumeDecision] = []
            decision_hashes: set[str] = set()
            for torrent in ordered_candidates:
                hash_value = str(torrent.hash).strip()
                if not hash_value or hash_value in decision_hashes:
                    continue
                decision_hashes.add(hash_value)
                unknown_decisions.append(
                    ResumeDecision(
                        hash=hash_value,
                        amount_left=torrent.amount_left,
                        fits=False,
                        resumed=False,
                        reason="active_remaining_unknown",
                    )
                )
            return ResumeSummary(
                budget=0,
                active_remaining=None,
                resumed_hashes=(),
                decisions=tuple(unknown_decisions),
            )

        budget = calculate_budget(
            disk_stats,
            resume_floor_pct=self._config.disk.resume_floor_pct,
            safety_buffer_gb=self._config.disk.safety_buffer_gb,
            active_remaining=active_remaining,
        )
        strict_fifo = (
            policy is ResumePolicy.PRIORITY_FIFO and self._config.resume.strict_fifo
        )
        remaining_budget = budget

        # Keep a trace of considered candidates and resolve their final
        # resumed/resume_failed outcome after the batch resume call.
        considered: list[tuple[str, int, bool]] = []
        planned_hashes: list[str] = []
        seen_hashes: set[str] = set()
        for torrent in ordered_candidates:
            hash_value = str(torrent.hash).strip()
            amount_left = torrent.amount_left
            if not hash_value or hash_value in seen_hashes:
                continue
            seen_hashes.add(hash_value)
            if amount_left is None or amount_left <= 0:
                self._logger.debug(
                    "Skipping candidate %s due to invalid amount_left=%s",
                    hash_value,
                    amount_left,
                )
                continue

            fits = amount_left <= remaining_budget
            considered.append((hash_value, amount_left, fits))
            if not fits:
                if strict_fifo:
                    break
                continue

            planned_hashes.append(hash_value)
            remaining_budget -= amount_left

        resumed_list = await self._resume_hashes(planned_hashes)
        if resumed_list:
            await self._remove_paused_tag_from_hashes(resumed_list)

        resumed_set = set(resumed_list)
        decisions: list[ResumeDecision] = []
        for hash_value, amount_left, fits in considered:
            if not fits:
                decisions.append(
                    ResumeDecision(
                        hash=hash_value,
                        amount_left=amount_left,
                        fits=False,
                        resumed=False,
                        reason="does_not_fit",
                    )
                )
                continue

            resumed = hash_value in resumed_set
            decisions.append(
                ResumeDecision(
                    hash=hash_value,
                    amount_left=amount_left,
                    fits=True,
                    resumed=resumed,
                    reason="resumed" if resumed else "resume_failed",
                )
            )

        return ResumeSummary(
            budget=budget,
            active_remaining=active_remaining,
            resumed_hashes=tuple(resumed_list),
            decisions=tuple(decisions),
        )

    def _eligible_candidates(
        self,
        torrents: Iterable[qbittorrentapi.TorrentDictionary],
        *,
        paused_hashes: set[str],
    ) -> list[qbittorrentapi.TorrentDictionary]:
        """Returns paused and tagged torrents eligible for resume attempts.

        Args:
            torrents: Current torrent snapshots from qBittorrent.

        Returns:
            Resume candidates with known positive remaining size.
        """
        candidates: list[qbittorrentapi.TorrentDictionary] = []
        for torrent in torrents:
            hash_value = str(torrent.hash).strip()
            state_value = str(torrent.state)
            amount_left = torrent.amount_left
            if not hash_value:
                continue
            if hash_value not in paused_hashes:
                continue
            if not is_paused_download_state(state_value):
                continue
            if amount_left is None or amount_left <= 0:
                self._logger.debug(
                    "Skipping candidate %s due to invalid amount_left=%s",
                    hash_value,
                    amount_left,
                )
                continue
            candidates.append(torrent)
        return candidates

    def _calculate_active_remaining(
        self,
        torrents: qbittorrentapi.TorrentInfoList,
        *,
        paused_hashes: set[str],
    ) -> int | None:
        """Calculates remaining bytes for active downloaders used in projection.

        Args:
            torrents: Current torrent snapshots from qBittorrent.

        Returns:
            Sum of remaining bytes for active downloaders, or ``None`` when any
            active downloader has missing/invalid size.
        """
        downloading_states = self._config.disk.downloading_states
        total = 0

        for torrent in torrents:
            if not is_active_downloader_for_projection(
                torrent,
                paused_hashes=paused_hashes,
                downloading_states=downloading_states,
            ):
                continue
            amount_left = torrent.amount_left
            if amount_left is None or amount_left < 0:
                return None
            total += amount_left

        return total

    async def _resume_hashes(self, torrent_hashes: list[str]) -> list[str]:
        """Attempts to resume hashes.

        Hashes are resumed in one batch request; batch failure is logged and
        treated as no resumes.
        """
        deduped_hashes = list(
            dict.fromkeys(
                hash_value
                for hash_value in (
                    str(hash_item).strip() for hash_item in torrent_hashes
                )
                if hash_value
            )
        )
        if not deduped_hashes:
            return []

        try:
            # qbittorrentapi.Client.torrents_resume(...)
            await asyncio.to_thread(
                self._qb_client.torrents_resume, torrent_hashes=deduped_hashes
            )
            for hash_value in deduped_hashes:
                self._logger.info("Resumed torrent %s", hash_value)
            return deduped_hashes
        except qbittorrentapi.APIError as exc:
            self._logger.warning(
                "Failed to resume %d torrents in batch: %s",
                len(deduped_hashes),
                exc,
            )
            return []

    async def _remove_paused_tag_from_hashes(self, torrent_hashes: list[str]) -> None:
        """Removes DiskGuard's managed paused tag from resumed hashes.

        Args:
            torrent_hashes: Hashes that were resumed successfully.
        """
        deduped_hashes = list(
            dict.fromkeys(
                hash_value
                for hash_value in (
                    str(hash_item).strip() for hash_item in torrent_hashes
                )
                if hash_value
            )
        )
        if not deduped_hashes:
            return

        paused_tag = self._config.tagging.paused_tag
        try:
            # qbittorrentapi.Client.torrents_remove_tags(...)
            await asyncio.to_thread(
                self._qb_client.torrents_remove_tags,
                tags=paused_tag,
                torrent_hashes=deduped_hashes,
            )
            return
        except qbittorrentapi.APIError as exc:
            self._logger.warning(
                "Resumed %d torrents but failed to remove tag %s in batch: %s",
                len(deduped_hashes),
                paused_tag,
                exc,
            )
