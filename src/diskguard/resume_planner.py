"""Resume planning and execution for DiskGuard NORMAL mode."""

from __future__ import annotations

import asyncio
import logging

import qbittorrentapi

from diskguard.config import AppConfig
from diskguard.models import DiskStats, ResumeDecision, ResumeSummary, ResumePolicy
from diskguard.state import (
    calculate_budget,
    is_active_downloader_for_projection,
    is_paused_download_state,
    sort_resume_candidates,
)


class ResumePlanner:
    """Applies projection-based resume logic using tag-truth candidates."""

    def __init__(self, config: AppConfig, qb_client, *, logger: logging.Logger | None = None) -> None:
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
        """Plans and executes eligible resumes for the current tick."""
        active_remaining = self._calculate_active_remaining(
            torrents,
            paused_hashes=paused_hashes,
        )
        if active_remaining is None:
            self._logger.warning(
                "Skipping resume tick because an active downloader has missing/invalid amount_left"
            )
            return ResumeSummary(
                budget=0,
                active_remaining=None,
                resumed_hashes=(),
                decisions=(),
            )

        budget = calculate_budget(
            disk_stats,
            resume_floor_pct=self._config.disk.resume_floor_pct,
            safety_buffer_gb=self._config.disk.safety_buffer_gb,
            active_remaining=active_remaining,
        )

        if budget <= 0:
            self._logger.debug(
                "Resume planner budget is non-positive; budget=%d active_remaining=%d",
                budget,
                active_remaining,
            )
            return ResumeSummary(
                budget=budget,
                active_remaining=active_remaining,
                resumed_hashes=(),
                decisions=(),
            )

        candidates = self._eligible_candidates(
            paused_torrents if paused_torrents is not None else torrents,
            paused_hashes=paused_hashes,
        )
        ordered_candidates = sort_resume_candidates(candidates, self._config.resume.policy)
        self._logger.debug(
            "Resume candidates ordered by %s: %s",
            self._config.resume.policy.value,
            [str(candidate.hash) for candidate in ordered_candidates],
        )

        decisions: list[ResumeDecision] = []
        resumed_hashes: list[str] = []
        planned_remaining = 0

        for candidate in ordered_candidates:
            hash_value = str(candidate.hash).strip()
            amount_left = candidate.amount_left
            if not hash_value:
                continue
            if amount_left is None or amount_left <= 0:
                self._logger.debug(
                    "Skipping candidate %s due to non-positive amount_left=%s",
                    hash_value,
                    amount_left,
                )
                continue

            fits = amount_left <= (budget - planned_remaining)
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
                if (
                    self._config.resume.policy is ResumePolicy.PRIORITY_FIFO
                    and self._config.resume.strict_fifo
                ):
                    break
                continue

            resumed = await self._resume_candidate(candidate)
            decisions.append(
                ResumeDecision(
                    hash=hash_value,
                    amount_left=amount_left,
                    fits=True,
                    resumed=resumed,
                    reason="resumed" if resumed else "resume_failed",
                )
            )
            if resumed:
                planned_remaining += amount_left
                resumed_hashes.append(hash_value)

        return ResumeSummary(
            budget=budget,
            active_remaining=active_remaining,
            resumed_hashes=tuple(resumed_hashes),
            decisions=tuple(decisions),
        )

    def _eligible_candidates(
        self,
        torrents: list[qbittorrentapi.TorrentDictionary],
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

    async def _resume_candidate(self, torrent: qbittorrentapi.TorrentDictionary) -> bool:
        """Resumes a candidate torrent and removes managed paused tag.

        Args:
            torrent: Candidate snapshot selected by the planner.

        Returns:
            ``True`` when resume succeeded, otherwise ``False``.
        """
        hash_value = str(torrent.hash).strip()
        if not hash_value:
            return False
        paused_tag = self._config.tagging.paused_tag
        try:
            await asyncio.to_thread(self._qb_client.torrents_resume, torrent_hashes=hash_value)
            self._logger.info("Resumed torrent %s", hash_value)
        except qbittorrentapi.APIError as exc:
            self._logger.warning("Failed to resume torrent %s: %s", hash_value, exc)
            return False

        try:
            await asyncio.to_thread(
                self._qb_client.torrents_remove_tags,
                tags=paused_tag,
                torrent_hashes=hash_value,
            )
        except qbittorrentapi.APIError as exc:
            self._logger.warning(
                "Torrent %s resumed but failed to remove tag %s: %s",
                hash_value,
                paused_tag,
                exc,
            )
        return True
