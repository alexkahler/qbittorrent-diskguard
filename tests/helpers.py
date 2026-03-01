"""Shared test helpers and fakes."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import qbittorrentapi

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
from diskguard.errors import DiskProbeError
from diskguard.models import DiskStats, ResumePolicy


@dataclass
class FakeTorrent:
    """Lightweight torrent object used by tests."""

    hash: str
    state: str
    amount_left: int | None
    priority: int = 0
    added_on: int = 0
    tags: str = ""
    name: str | None = None
    category: str | None = None


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
    on_add_quick_poll_interval_seconds: float = 1.0,
    on_add_quick_poll_max_attempts: int = 10,
    on_add_quick_poll_max_queue_size: int = 64,
    on_add_auth_token: str = "test-token",
    on_add_max_body_bytes: int = 8192,
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
        polling=PollingConfig(
            interval_seconds=interval_seconds,
            on_add_quick_poll_interval_seconds=on_add_quick_poll_interval_seconds,
            on_add_quick_poll_max_attempts=on_add_quick_poll_max_attempts,
            on_add_quick_poll_max_queue_size=on_add_quick_poll_max_queue_size,
        ),
        resume=ResumeConfig(policy=policy, strict_fifo=strict_fifo),
        tagging=TaggingConfig(paused_tag=paused_tag, soft_allowed_tag=soft_allowed_tag),
        logging=LoggingConfig(level="DEBUG"),
        server=ServerConfig(
            host="127.0.0.1",
            port=7070,
            on_add_auth_token=on_add_auth_token,
            on_add_max_body_bytes=on_add_max_body_bytes,
        ),
    )


def torrent(
    torrent_hash: str,
    *,
    state: str,
    amount_left: int | None,
    priority: int = 0,
    added_on: int = 0,
    tags: tuple[str, ...] = (),
) -> FakeTorrent:
    """Factory for FakeTorrent."""
    return FakeTorrent(
        hash=torrent_hash,
        state=state,
        amount_left=amount_left,
        priority=priority,
        added_on=added_on,
        tags=", ".join(tags),
    )


@dataclass
class FakeDiskProbe:
    """Simple fake disk probe with deterministic outputs."""

    stats_sequence: list[DiskStats] | None = None
    error: Exception | None = None

    def __post_init__(self) -> None:
        """Initializes derived fields after dataclass construction."""
        self.calls = 0

    def measure(self) -> DiskStats:
        """Measure."""
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert self.stats_sequence is not None
        index = min(self.calls - 1, len(self.stats_sequence) - 1)
        return self.stats_sequence[index]


class FakeQbClient:
    """Fake synchronous qB client that records actions."""

    def __init__(
        self,
        *,
        torrents_sequence: list[list[FakeTorrent]] | None = None,
        torrent_lookup_sequence: dict[str, list[FakeTorrent | None]] | None = None,
        fetch_error: Exception | None = None,
        fail_fetch_torrent: set[str] | None = None,
        fail_pause: set[str] | None = None,
        fail_resume: set[str] | None = None,
        fail_add_tag: set[tuple[str, str]] | None = None,
        fail_remove_tag: set[tuple[str, str]] | None = None,
    ) -> None:
        """Initializes the test helper state."""
        self._torrents_sequence = torrents_sequence or [[]]
        self._torrent_lookup_sequence = torrent_lookup_sequence or {}
        self._torrent_lookup_calls_by_hash: dict[str, int] = {}
        self._fetch_error = fetch_error
        self._fetch_calls = 0
        self._current_snapshot: list[FakeTorrent] | None = None

        self.fail_fetch_torrent = fail_fetch_torrent or set()
        self.fail_pause = fail_pause or set()
        self.fail_resume = fail_resume or set()
        self.fail_add_tag = fail_add_tag or set()
        self.fail_remove_tag = fail_remove_tag or set()

        self.fetch_torrent_calls: list[str] = []
        self.fetch_torrent_request_payloads: list[tuple[str, ...]] = []
        self.pause_calls: list[str] = []
        self.pause_request_payloads: list[tuple[str, ...]] = []
        self.resume_calls: list[str] = []
        self.resume_request_payloads: list[tuple[str, ...]] = []
        self.add_tag_calls: list[tuple[str, str]] = []
        self.add_tag_request_payloads: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
        self.remove_tag_calls: list[tuple[str, str]] = []
        self.remove_tag_request_payloads: list[tuple[tuple[str, ...], tuple[str, ...]]] = []

    @property
    def fetch_calls(self) -> int:
        """Fetch calls."""
        return self._fetch_calls

    def torrents_info(
        self,
        *,
        torrent_hashes: str | Iterable[str] | None = None,
        tag: str | None = None,
    ) -> list[FakeTorrent]:
        """Returns torrents, optionally filtered by hash or tag."""
        if torrent_hashes is not None:
            requested_hashes = _normalize_hash_filter(torrent_hashes)
            self.fetch_torrent_request_payloads.append(tuple(requested_hashes))
            self.fetch_torrent_calls.extend(requested_hashes)
            for torrent_hash in requested_hashes:
                if torrent_hash in self.fail_fetch_torrent:
                    raise qbittorrentapi.APIConnectionError(
                        f"fetch torrent failed for {torrent_hash}"
                    )

            resolved_by_hash: dict[str, FakeTorrent] = {}
            used_lookup_sequences = False
            for torrent_hash in requested_hashes:
                lookup_sequence = self._torrent_lookup_sequence.get(torrent_hash)
                if lookup_sequence is None:
                    continue

                used_lookup_sequences = True
                calls = self._torrent_lookup_calls_by_hash.get(torrent_hash, 0)
                index = min(calls, len(lookup_sequence) - 1)
                self._torrent_lookup_calls_by_hash[torrent_hash] = calls + 1
                resolved = lookup_sequence[index]
                if resolved is not None:
                    resolved_by_hash[torrent_hash] = resolved

            if used_lookup_sequences:
                if not self._torrents_sequence:
                    return [resolved_by_hash[torrent_hash] for torrent_hash in requested_hashes if torrent_hash in resolved_by_hash]

                index = min(self._fetch_calls, len(self._torrents_sequence) - 1)
                by_hash = {torrent.hash: torrent for torrent in self._torrents_sequence[index]}
                results: list[FakeTorrent] = []
                for torrent_hash in requested_hashes:
                    if torrent_hash in resolved_by_hash:
                        results.append(resolved_by_hash[torrent_hash])
                        continue
                    if torrent_hash in by_hash:
                        results.append(by_hash[torrent_hash])
                return results

            if not self._torrents_sequence:
                return []
            index = min(self._fetch_calls, len(self._torrents_sequence) - 1)
            by_hash = {torrent.hash: torrent for torrent in self._torrents_sequence[index]}
            return [by_hash[torrent_hash] for torrent_hash in requested_hashes if torrent_hash in by_hash]

        if tag is None:
            self._fetch_calls += 1
            if self._fetch_error is not None:
                raise self._fetch_error
            index = min(self._fetch_calls - 1, len(self._torrents_sequence) - 1)
            self._current_snapshot = list(self._torrents_sequence[index])
            return list(self._current_snapshot)

        if self._current_snapshot is None:
            index = min(max(self._fetch_calls - 1, 0), len(self._torrents_sequence) - 1)
            base_snapshot = self._torrents_sequence[index]
        else:
            base_snapshot = self._current_snapshot
        return [torrent for torrent in base_snapshot if _torrent_has_tag(torrent, str(tag))]

    def torrents_pause(self, *, torrent_hashes: str | Iterable[str] | None = None) -> None:
        """Stops a torrent."""
        normalized_hashes = _normalize_hash_filter(torrent_hashes)
        self.pause_request_payloads.append(tuple(normalized_hashes))
        for torrent_hash in normalized_hashes:
            self.pause_calls.append(torrent_hash)
            if torrent_hash in self.fail_pause:
                raise qbittorrentapi.APIConnectionError(f"pause failed for {torrent_hash}")

    def torrents_resume(self, *, torrent_hashes: str | Iterable[str] | None = None) -> None:
        """Resumes a torrent."""
        normalized_hashes = _normalize_hash_filter(torrent_hashes)
        self.resume_request_payloads.append(tuple(normalized_hashes))
        for torrent_hash in normalized_hashes:
            self.resume_calls.append(torrent_hash)
            if torrent_hash in self.fail_resume:
                raise qbittorrentapi.APIConnectionError(f"resume failed for {torrent_hash}")

    def torrents_add_tags(
        self,
        *,
        tags: str | Iterable[str] | None = None,
        torrent_hashes: str | Iterable[str] | None = None,
    ) -> None:
        """Adds tags to a torrent."""
        normalized_tags = _normalize_tags(tags)
        normalized_hashes = _normalize_hash_filter(torrent_hashes)
        self.add_tag_request_payloads.append((tuple(normalized_hashes), tuple(normalized_tags)))
        for torrent_hash in normalized_hashes:
            for tag in normalized_tags:
                self.add_tag_calls.append((torrent_hash, tag))
                if (torrent_hash, tag) in self.fail_add_tag:
                    raise qbittorrentapi.APIConnectionError(f"add tag failed for {torrent_hash}")

    def torrents_remove_tags(
        self,
        *,
        tags: str | Iterable[str] | None = None,
        torrent_hashes: str | Iterable[str] | None = None,
    ) -> None:
        """Removes tags from a torrent."""
        normalized_tags = _normalize_tags(tags)
        normalized_hashes = _normalize_hash_filter(torrent_hashes)
        self.remove_tag_request_payloads.append((tuple(normalized_hashes), tuple(normalized_tags)))
        for torrent_hash in normalized_hashes:
            for tag in normalized_tags:
                self.remove_tag_calls.append((torrent_hash, tag))
                if (torrent_hash, tag) in self.fail_remove_tag:
                    raise qbittorrentapi.APIConnectionError(f"remove tag failed for {torrent_hash}")

    def app_version(self) -> str:
        """Returns fake application version."""
        return "5.1.0"

    def app_web_api_version(self) -> str:
        """Returns fake Web API version."""
        return "2.3.0"

    def auth_log_out(self) -> None:
        """Logs out fake client session."""
        return None


def disk_stats(*, total_bytes: int, free_bytes: int) -> DiskStats:
    """Creates DiskStats with derived percentage."""
    free_pct = (free_bytes / total_bytes) * 100.0
    return DiskStats(total_bytes=total_bytes, free_bytes=free_bytes, free_pct=free_pct)


def missing_path_error(path: str = "/downloads") -> DiskProbeError:
    """Creates a disk probe error for missing path scenarios."""
    return DiskProbeError(f"Unable to read watch path {path!r}")


def _torrent_has_tag(torrent: FakeTorrent, tag: str) -> bool:
    """Returns whether a fake torrent has a specific tag."""
    tags = {part.strip() for part in torrent.tags.split(",") if part.strip()}
    return tag in tags


def _normalize_hash_filter(torrent_hashes: str | Iterable[str] | None) -> list[str]:
    """Normalizes ``torrent_hashes`` filter into a list of hashes."""
    if torrent_hashes is None:
        return []
    if isinstance(torrent_hashes, str):
        return [part.strip() for part in torrent_hashes.split("|") if part.strip()]
    return [str(part).strip() for part in torrent_hashes if str(part).strip()]


def _normalize_tags(tags: str | Iterable[str] | None) -> list[str]:
    """Normalizes tags payload to non-empty tag names."""
    if tags is None:
        return []
    if isinstance(tags, str):
        return [part.strip() for part in tags.split(",") if part.strip()]
    return [str(part).strip() for part in tags if str(part).strip()]
