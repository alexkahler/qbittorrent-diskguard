"""Pure state/classification functions used across DiskGuard."""

from __future__ import annotations

import math
from collections.abc import Iterable

import qbittorrentapi

from diskguard.models import DiskStats, Mode, ResumePolicy

PAUSED_DOWNLOAD_STATES = frozenset({"pausedDL", "stoppedDL"})


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


def is_completed_or_seeding_state(state: str, downloading_states: Iterable[str]) -> bool:
    """Returns whether a torrent state indicates completed/seeding behavior."""
    if is_downloading_ish_state(state, downloading_states):
        return False
    if is_paused_download_state(state):
        return False
    if state == "uploading":
        return True
    return state.endswith("UP")


def is_active_downloader_for_projection(
    torrent: qbittorrentapi.TorrentDictionary,
    *,
    paused_hashes: set[str],
    downloading_states: Iterable[str],
) -> bool:
    """Returns whether the torrent should count toward active_remaining."""
    if str(torrent.hash).strip() in paused_hashes:
        return False
    state_value = str(torrent.state)
    if is_forced_download_state(state_value):
        return False
    return is_downloading_ish_state(state_value, downloading_states)


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
    candidates: list[qbittorrentapi.TorrentDictionary],
    policy: ResumePolicy,
) -> list[qbittorrentapi.TorrentDictionary]:
    """Sorts candidates according to the selected resume policy."""

    # FIXME: Secondary sort should be on TorrentDictionary.priority, not added_on.
    if policy is ResumePolicy.SMALLEST_FIRST:
        return sorted(
            candidates,
            key=lambda torrent: (
                torrent.amount_left if torrent.amount_left is not None else math.inf,
                torrent.added_on,
                str(torrent.hash).strip(),
            ),
        )

    # FIXME: Secondary sort should be on TorrentDictionary.priority, not added_on.
    if policy is ResumePolicy.LARGEST_FIRST:
        return sorted(
            candidates,
            key=lambda torrent: (
                -(torrent.amount_left if torrent.amount_left is not None else -1),
                torrent.added_on,
                str(torrent.hash).strip(),
            ),
        )

    return sorted(
        candidates,
        key=lambda torrent: (
            -torrent.priority,
            torrent.added_on,
            str(torrent.hash).strip(),
        ),
    )
