"""Shared test helpers and fakes."""

from __future__ import annotations

from dataclasses import dataclass

from diskguard.config import (
    AppConfig,
    DiskConfig,
    LoggingConfig,
    PollingConfig,
    QbittorrentConfig,
    ResumeConfig,
    ServerConfig,
    TaggingConfig,
)
from diskguard.errors import DiskProbeError, QbittorrentUnavailableError
from diskguard.models import DiskStats, ResumePolicy, TorrentSnapshot


def make_config(
    *,
    policy: ResumePolicy = ResumePolicy.PRIORITY_FIFO,
    strict_fifo: bool = True,
    soft_pause_below_pct: float = 10.0,
    hard_pause_below_pct: float = 5.0,
    resume_floor_pct: float = 10.0,
    safety_buffer_gb: float = 10.0,
    paused_tag: str = "diskguard_paused",
    soft_allowed_tag: str = "soft_allowed",
    downloading_states: tuple[str, ...] = (
        "downloading",
        "metaDL",
        "queuedDL",
        "stalledDL",
        "checkingDL",
        "allocating",
    ),
    interval_seconds: int = 30,
) -> AppConfig:
    """Creates a full AppConfig for tests."""
    return AppConfig(
        qbittorrent=QbittorrentConfig(
            url="http://qbittorrent:8080",
            username="admin",
            password="password",
        ),
        disk=DiskConfig(
            watch_path="/downloads",
            soft_pause_below_pct=soft_pause_below_pct,
            hard_pause_below_pct=hard_pause_below_pct,
            resume_floor_pct=resume_floor_pct,
            safety_buffer_gb=safety_buffer_gb,
            downloading_states=downloading_states,
        ),
        polling=PollingConfig(interval_seconds=interval_seconds),
        resume=ResumeConfig(policy=policy, strict_fifo=strict_fifo),
        tagging=TaggingConfig(paused_tag=paused_tag, soft_allowed_tag=soft_allowed_tag),
        logging=LoggingConfig(level="DEBUG"),
        server=ServerConfig(host="127.0.0.1", port=7070),
    )


def torrent(
    torrent_hash: str,
    *,
    state: str,
    amount_left: int | None,
    priority: int = 0,
    added_on: int = 0,
    tags: tuple[str, ...] = (),
) -> TorrentSnapshot:
    """Factory for TorrentSnapshot."""
    return TorrentSnapshot(
        hash=torrent_hash,
        state=state,
        amount_left=amount_left,
        priority=priority,
        added_on=added_on,
        tags=frozenset(tags),
    )


@dataclass
class FakeDiskProbe:
    """Simple fake disk probe with deterministic outputs."""

    stats_sequence: list[DiskStats] | None = None
    error: Exception | None = None

    def __post_init__(self) -> None:
        self.calls = 0

    def measure(self) -> DiskStats:
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert self.stats_sequence is not None
        index = min(self.calls - 1, len(self.stats_sequence) - 1)
        return self.stats_sequence[index]


class FakeQbClient:
    """Fake async qB client that records actions."""

    def __init__(
        self,
        *,
        torrents_sequence: list[list[TorrentSnapshot]] | None = None,
        fetch_error: Exception | None = None,
        fail_pause: set[str] | None = None,
        fail_resume: set[str] | None = None,
        fail_add_tag: set[tuple[str, str]] | None = None,
        fail_remove_tag: set[tuple[str, str]] | None = None,
    ) -> None:
        self._torrents_sequence = torrents_sequence or [[]]
        self._fetch_error = fetch_error
        self._fetch_calls = 0

        self.fail_pause = fail_pause or set()
        self.fail_resume = fail_resume or set()
        self.fail_add_tag = fail_add_tag or set()
        self.fail_remove_tag = fail_remove_tag or set()

        self.pause_calls: list[str] = []
        self.resume_calls: list[str] = []
        self.add_tag_calls: list[tuple[str, str]] = []
        self.remove_tag_calls: list[tuple[str, str]] = []

    @property
    def fetch_calls(self) -> int:
        return self._fetch_calls

    async def fetch_torrents(self) -> list[TorrentSnapshot]:
        self._fetch_calls += 1
        if self._fetch_error is not None:
            raise self._fetch_error
        index = min(self._fetch_calls - 1, len(self._torrents_sequence) - 1)
        return self._torrents_sequence[index]

    async def pause_torrent(self, torrent_hash: str) -> None:
        self.pause_calls.append(torrent_hash)
        if torrent_hash in self.fail_pause:
            raise QbittorrentUnavailableError(f"pause failed for {torrent_hash}")

    async def resume_torrent(self, torrent_hash: str) -> None:
        self.resume_calls.append(torrent_hash)
        if torrent_hash in self.fail_resume:
            raise QbittorrentUnavailableError(f"resume failed for {torrent_hash}")

    async def add_tag(self, torrent_hash: str, tag: str) -> None:
        self.add_tag_calls.append((torrent_hash, tag))
        if (torrent_hash, tag) in self.fail_add_tag:
            raise QbittorrentUnavailableError(f"add tag failed for {torrent_hash}")

    async def remove_tag(self, torrent_hash: str, tag: str) -> None:
        self.remove_tag_calls.append((torrent_hash, tag))
        if (torrent_hash, tag) in self.fail_remove_tag:
            raise QbittorrentUnavailableError(f"remove tag failed for {torrent_hash}")

    async def close(self) -> None:
        return None


def disk_stats(*, total_bytes: int, free_bytes: int) -> DiskStats:
    """Creates DiskStats with derived percentage."""
    free_pct = (free_bytes / total_bytes) * 100.0
    return DiskStats(total_bytes=total_bytes, free_bytes=free_bytes, free_pct=free_pct)


def missing_path_error(path: str = "/downloads") -> DiskProbeError:
    """Creates a disk probe error for missing path scenarios."""
    return DiskProbeError(f"Unable to read watch path {path!r}")
