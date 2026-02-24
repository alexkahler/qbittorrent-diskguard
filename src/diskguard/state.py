"""Pure state/classification functions used across DiskGuard."""

from __future__ import annotations

import math
from typing import Iterable

from diskguard.models import DiskStats, Mode, ResumePolicy, TorrentSnapshot

PAUSED_DOWNLOAD_STATES = frozenset({"pausedDL", "stoppedDL"})


def parse_tags(raw_tags: str | None) -> frozenset[str]:
    """Parses qBittorrent's comma-separated tags string."""
    if not raw_tags:
        return frozenset()
    tags = [tag.strip() for tag in raw_tags.split(",") if tag.strip()]
    return frozenset(tags)


def classify_mode(
    free_pct: float,
    *,
    soft_pause_below_pct: float,
    hard_pause_below_pct: float,
) -> Mode:
    """Maps free percentage to NORMAL/SOFT/HARD mode."""
    if free_pct < hard_pause_below_pct:
        return Mode.HARD
    if free_pct < soft_pause_below_pct:
        return Mode.SOFT
    return Mode.NORMAL


def is_forced_download_state(state: str) -> bool:
    """Returns whether the torrent is in forced download state."""
    return state == "forcedDL"


def is_downloading_ish_state(state: str, downloading_states: Iterable[str]) -> bool:
    """Returns whether a state should be considered disk-consuming for v1."""
    return state in set(downloading_states)


def is_paused_download_state(state: str) -> bool:
    """Returns whether a torrent is currently paused in a download state."""
    return state in PAUSED_DOWNLOAD_STATES


def is_active_downloader_for_projection(
    torrent: TorrentSnapshot,
    *,
    paused_tag: str,
    downloading_states: Iterable[str],
) -> bool:
    """Returns whether the torrent should count toward active_remaining."""
    if torrent.has_tag(paused_tag):
        return False
    if is_forced_download_state(torrent.state):
        return False
    return is_downloading_ish_state(torrent.state, downloading_states)


def calculate_budget(
    disk_stats: DiskStats,
    *,
    resume_floor_pct: float,
    safety_buffer_gb: float,
    active_remaining: int,
) -> int:
    """Computes resume budget in bytes."""
    floor_bytes = int(disk_stats.total_bytes * (resume_floor_pct / 100.0))
    buffer_bytes = int(safety_buffer_gb * (1024**3))
    return int(disk_stats.free_bytes - floor_bytes - buffer_bytes - active_remaining)


def sort_resume_candidates(
    candidates: list[TorrentSnapshot],
    policy: ResumePolicy,
) -> list[TorrentSnapshot]:
    """Sorts candidates according to the selected resume policy."""
    if policy is ResumePolicy.SMALLEST_FIRST:
        return sorted(
            candidates,
            key=lambda torrent: (
                torrent.amount_left if torrent.amount_left is not None else math.inf,
                torrent.added_on,
                torrent.hash,
            ),
        )

    if policy is ResumePolicy.LARGEST_FIRST:
        return sorted(
            candidates,
            key=lambda torrent: (
                -(torrent.amount_left if torrent.amount_left is not None else -1),
                torrent.added_on,
                torrent.hash,
            ),
        )

    return sorted(
        candidates,
        key=lambda torrent: (
            -torrent.priority,
            torrent.added_on,
            torrent.hash,
        ),
    )
