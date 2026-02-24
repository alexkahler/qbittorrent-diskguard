"""Resume planning and execution for DiskGuard NORMAL mode."""

from __future__ import annotations

import logging

from diskguard.config import AppConfig
from diskguard.errors import QbittorrentError
from diskguard.models import DiskStats, ResumeDecision, ResumeSummary, ResumePolicy, TorrentSnapshot
from diskguard.state import (
    calculate_budget,
    is_active_downloader_for_projection,
    is_paused_download_state,
    sort_resume_candidates,
)


class ResumePlanner:
    """Applies projection-based resume logic using tag-truth candidates."""

    def __init__(self, config: AppConfig, qb_client, *, logger: logging.Logger | None = None) -> None:
        self._config = config
        self._qb_client = qb_client
        self._logger = logger or logging.getLogger(__name__)

    async def execute(self, torrents: list[TorrentSnapshot], disk_stats: DiskStats) -> ResumeSummary:
        """Plans and executes eligible resumes for the current tick."""
        active_remaining = self._calculate_active_remaining(torrents)
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

        candidates = self._eligible_candidates(torrents)
        ordered_candidates = sort_resume_candidates(candidates, self._config.resume.policy)
        self._logger.debug(
            "Resume candidates ordered by %s: %s",
            self._config.resume.policy.value,
            [candidate.hash for candidate in ordered_candidates],
        )

        decisions: list[ResumeDecision] = []
        resumed_hashes: list[str] = []
        planned_remaining = 0

        for candidate in ordered_candidates:
            amount_left = candidate.amount_left
            assert amount_left is not None and amount_left > 0

            fits = amount_left <= (budget - planned_remaining)
            if not fits:
                decisions.append(
                    ResumeDecision(
                        hash=candidate.hash,
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
                        hash=candidate.hash,
                        amount_left=amount_left,
                        fits=True,
                        resumed=resumed,
                        reason="resumed" if resumed else "resume_failed",
                    )
                )
            if resumed:
                planned_remaining += amount_left
                resumed_hashes.append(candidate.hash)

        return ResumeSummary(
            budget=budget,
            active_remaining=active_remaining,
            resumed_hashes=tuple(resumed_hashes),
            decisions=tuple(decisions),
        )

    def _eligible_candidates(self, torrents: list[TorrentSnapshot]) -> list[TorrentSnapshot]:
        paused_tag = self._config.tagging.paused_tag
        candidates: list[TorrentSnapshot] = []
        for torrent in torrents:
            if not torrent.has_tag(paused_tag):
                continue
            if not is_paused_download_state(torrent.state):
                continue
            if torrent.amount_left is None or torrent.amount_left <= 0:
                self._logger.debug(
                    "Skipping candidate %s due to invalid amount_left=%s",
                    torrent.hash,
                    torrent.amount_left,
                )
                continue
            candidates.append(torrent)
        return candidates

    def _calculate_active_remaining(self, torrents: list[TorrentSnapshot]) -> int | None:
        paused_tag = self._config.tagging.paused_tag
        downloading_states = self._config.disk.downloading_states
        total = 0

        for torrent in torrents:
            if not is_active_downloader_for_projection(
                torrent,
                paused_tag=paused_tag,
                downloading_states=downloading_states,
            ):
                continue
            if torrent.amount_left is None or torrent.amount_left < 0:
                return None
            total += torrent.amount_left

        return total

    async def _resume_candidate(self, torrent: TorrentSnapshot) -> bool:
        paused_tag = self._config.tagging.paused_tag
        try:
            await self._qb_client.resume_torrent(torrent.hash)
            self._logger.info("Resumed torrent %s", torrent.hash)
        except QbittorrentError as exc:
            self._logger.warning("Failed to resume torrent %s: %s", torrent.hash, exc)
            return False

        try:
            await self._qb_client.remove_tag(torrent.hash, paused_tag)
        except QbittorrentError as exc:
            self._logger.warning(
                "Torrent %s resumed but failed to remove tag %s: %s",
                torrent.hash,
                paused_tag,
                exc,
            )
        return True
