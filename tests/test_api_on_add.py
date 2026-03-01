"""Tests for the /on-add fast path endpoint."""

import asyncio
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
import logging
import threading

import pytest
from aiohttp.test_utils import BaseTestServer, TestClient, TestServer

from diskguard.api import OnAddHandler, create_http_app
from tests.helpers import FakeDiskProbe, FakeQbClient, disk_stats, make_config, torrent

AUTH_TOKEN = "test-token"
VALID_HASH_40 = "a" * 40
VALID_HASH_64 = "b" * 64


def _auth_headers(token: str = AUTH_TOKEN) -> dict[str, str]:
    """Returns auth headers for on-add endpoint requests."""
    return {"X-DiskGuard-Token": token}


@asynccontextmanager
async def _test_client(server: BaseTestServer) -> AsyncIterator[TestClient]:
    """Builds a typed aiohttp test client context for strict mypy checks."""
    client: TestClient = TestClient(server)
    async with client:
        yield client


class BlockingQbClient(FakeQbClient):
    """Fake qB client that blocks fetch_torrent_by_hash until released."""

    def __init__(self) -> None:
        super().__init__()
        self._release_event = threading.Event()

    def torrents_info(self, *, torrent_hashes=None):  # type: ignore[override]
        """Blocks until release and then returns a known-size snapshot."""
        if torrent_hashes is None:
            return super().torrents_info()
        requested_hashes = _normalize_hashes(torrent_hashes)
        self.fetch_torrent_request_payloads.append(tuple(requested_hashes))
        self.fetch_torrent_calls.extend(requested_hashes)
        self._release_event.wait()
        return [
            torrent(torrent_hash, state="downloading", amount_left=10)
            for torrent_hash in requested_hashes
        ]

    def release(self) -> None:
        """Releases blocked quick-poll calls."""
        self._release_event.set()


class PauseBlockingQbClient(FakeQbClient):
    """Fake qB client that blocks pause calls until released."""

    def __init__(self, torrent_hash: str) -> None:
        super().__init__(
            torrent_lookup_sequence={
                torrent_hash: [
                    torrent(torrent_hash, state="downloading", amount_left=10)
                ]
            }
        )
        self._pause_started_event = threading.Event()
        self._pause_release_event = threading.Event()

    def torrents_pause(self, *, torrent_hashes=None) -> None:  # type: ignore[override]
        """Blocks pause request processing until release is signaled."""
        if torrent_hashes is None:
            requested_hashes: list[str] = []
        else:
            requested_hashes = _normalize_hashes(torrent_hashes)
        self.pause_request_payloads.append(tuple(requested_hashes))
        self.pause_calls.extend(requested_hashes)
        self._pause_started_event.set()
        self._pause_release_event.wait()

    def wait_for_pause_started(self, timeout_seconds: float) -> bool:
        """Waits until pause request enters the blocking section."""
        return self._pause_started_event.wait(timeout_seconds)

    def release_pause(self) -> None:
        """Releases the blocked pause request."""
        self._pause_release_event.set()


class FailFirstQuickPollQbClient(FakeQbClient):
    """Fake client that fails the first hash quick-poll request with a non-API error."""

    def __init__(self, torrent_hash: str) -> None:
        super().__init__(
            torrent_lookup_sequence={
                torrent_hash: [
                    torrent(torrent_hash, state="downloading", amount_left=10)
                ]
            }
        )
        self._failed_once_hashes: set[str] = set()

    def torrents_info(self, *, torrent_hashes=None):  # type: ignore[override]
        """Raises once for hash-filtered fetches, then behaves normally."""
        if torrent_hashes is not None:
            requested_hashes = _normalize_hashes(torrent_hashes)
            should_fail = any(
                torrent_hash not in self._failed_once_hashes
                for torrent_hash in requested_hashes
            )
            if should_fail:
                self.fetch_torrent_request_payloads.append(tuple(requested_hashes))
                self.fetch_torrent_calls.extend(requested_hashes)
                self._failed_once_hashes.update(requested_hashes)
                raise RuntimeError("bad quick poll payload")
        return super().torrents_info(torrent_hashes=torrent_hashes)


def _normalize_hashes(torrent_hashes: str | Iterable[str]) -> list[str]:
    """Normalizes hash filters into a list of non-empty hash strings."""
    if isinstance(torrent_hashes, str):
        return [part.strip() for part in torrent_hashes.split("|") if part.strip()]
    return [str(part).strip() for part in torrent_hashes if str(part).strip()]


async def test_on_add_rejects_missing_auth_token() -> None:
    """Tests that on-add rejects requests without shared-secret header."""
    config = make_config()
    qb = FakeQbClient()
    probe = FakeDiskProbe(
        stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=500)]
    )
    handler = OnAddHandler(config, qb_client=qb, disk_probe=probe)

    app = create_http_app(handler)
    async with TestServer(app) as server:
        async with _test_client(server) as client:
            response = await client.post("/on-add", data={"hash": VALID_HASH_40})
            payload = await response.json()

    assert response.status == 401
    assert payload["message"] == "unauthorized"
    assert qb.pause_calls == []


async def test_on_add_rejects_invalid_auth_token() -> None:
    """Tests that on-add rejects requests with incorrect shared-secret header."""
    config = make_config()
    qb = FakeQbClient()
    probe = FakeDiskProbe(
        stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=500)]
    )
    handler = OnAddHandler(config, qb_client=qb, disk_probe=probe)

    app = create_http_app(handler)
    async with TestServer(app) as server:
        async with _test_client(server) as client:
            response = await client.post(
                "/on-add",
                data={"hash": VALID_HASH_40},
                headers=_auth_headers(token="wrong-token"),
            )
            payload = await response.json()

    assert response.status == 401
    assert payload["message"] == "unauthorized"
    assert qb.pause_calls == []


async def test_on_add_in_normal_mode_does_nothing() -> None:
    """Tests that on add in normal mode does nothing."""
    config = make_config()
    qb = FakeQbClient()
    probe = FakeDiskProbe(
        stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=500)]
    )
    handler = OnAddHandler(config, qb_client=qb, disk_probe=probe)

    app = create_http_app(handler)
    async with TestServer(app) as server:
        async with _test_client(server) as client:
            response = await client.post(
                "/on-add",
                data={"hash": VALID_HASH_40},
                headers=_auth_headers(),
            )
            payload = await response.json()

    assert response.status == 200
    assert payload["action"] == "none"
    assert qb.pause_calls == []
    assert qb.add_tag_calls == []


async def test_on_add_accepts_valid_64_character_hash() -> None:
    """Tests that on-add accepts valid 64-char hash format."""
    config = make_config()
    qb = FakeQbClient()
    probe = FakeDiskProbe(
        stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=500)]
    )
    handler = OnAddHandler(config, qb_client=qb, disk_probe=probe)

    app = create_http_app(handler)
    async with TestServer(app) as server:
        async with _test_client(server) as client:
            response = await client.post(
                "/on-add",
                data={"hash": VALID_HASH_64},
                headers=_auth_headers(),
            )
            payload = await response.json()

    assert response.status == 200
    assert payload["action"] == "none"


async def test_on_add_in_soft_mode_pauses_and_tags_hash() -> None:
    """Tests that on add in soft mode pauses and tags hash."""
    config = make_config(
        on_add_quick_poll_interval_seconds=0.01, on_add_quick_poll_max_attempts=2
    )
    qb = FakeQbClient(
        torrent_lookup_sequence={
            VALID_HASH_40: [
                torrent(VALID_HASH_40, state="downloading", amount_left=10)
            ],
        }
    )
    probe = FakeDiskProbe(stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=90)])
    handler = OnAddHandler(config, qb_client=qb, disk_probe=probe)

    app = create_http_app(handler)
    async with TestServer(app) as server:
        async with _test_client(server) as client:
            response = await client.post(
                "/on-add",
                data={"hash": VALID_HASH_40},
                headers=_auth_headers(),
            )
            assert response.status == 202
            payload = await response.json()
            assert payload["action"] == "quick_poll_pause_and_mark"
            await handler.shutdown()

    assert qb.pause_calls == [VALID_HASH_40]
    assert qb.add_tag_calls == [(VALID_HASH_40, "diskguard_paused")]


async def test_on_add_requires_hash() -> None:
    """Tests that on add requires hash."""
    config = make_config()
    qb = FakeQbClient()
    probe = FakeDiskProbe(stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=90)])
    handler = OnAddHandler(config, qb_client=qb, disk_probe=probe)

    app = create_http_app(handler)
    async with TestServer(app) as server:
        async with _test_client(server) as client:
            response = await client.post("/on-add", data={}, headers=_auth_headers())
            payload = await response.json()

    assert response.status == 400
    assert payload["message"] == "hash is required"


@pytest.mark.parametrize(
    "invalid_hash",
    [
        "hash1|hash2",
        "all",
        "abc",
        "g" * 40,
        "a" * 39,
        "a" * 41,
    ],
)
async def test_on_add_rejects_invalid_hash_format(invalid_hash: str) -> None:
    """Tests that on-add validates torrent hash format strictly."""
    config = make_config()
    qb = FakeQbClient()
    probe = FakeDiskProbe(
        stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=500)]
    )
    handler = OnAddHandler(config, qb_client=qb, disk_probe=probe)

    app = create_http_app(handler)
    async with TestServer(app) as server:
        async with _test_client(server) as client:
            response = await client.post(
                "/on-add",
                data={"hash": invalid_hash},
                headers=_auth_headers(),
            )
            payload = await response.json()

    assert response.status == 400
    assert payload["message"] == "hash must be a 40 or 64 character hex string"
    assert qb.pause_calls == []


async def test_on_add_returns_accepted_when_qb_is_temporarily_unavailable() -> None:
    """Tests that on add returns accepted when qb is temporarily unavailable."""
    config = make_config(
        on_add_quick_poll_interval_seconds=0.01, on_add_quick_poll_max_attempts=1
    )
    qb = FakeQbClient(
        torrent_lookup_sequence={
            VALID_HASH_40: [
                torrent(VALID_HASH_40, state="downloading", amount_left=10)
            ],
        },
        fail_pause={VALID_HASH_40},
    )
    probe = FakeDiskProbe(stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=90)])
    handler = OnAddHandler(config, qb_client=qb, disk_probe=probe)

    app = create_http_app(handler)
    async with TestServer(app) as server:
        async with _test_client(server) as client:
            response = await client.post(
                "/on-add",
                data={"hash": VALID_HASH_40},
                headers=_auth_headers(),
            )
            assert response.status == 202
            await handler.shutdown()

    assert qb.pause_calls == [VALID_HASH_40]
    assert qb.add_tag_calls == []


async def test_on_add_handles_parallel_calls_without_state_corruption() -> None:
    """Tests that on add handles parallel calls without state corruption."""
    config = make_config(
        on_add_quick_poll_interval_seconds=0.01,
        on_add_quick_poll_max_attempts=2,
    )
    hashes = [f"{index:040x}" for index in range(20)]
    qb = FakeQbClient(
        torrent_lookup_sequence={
            torrent_hash: [torrent(torrent_hash, state="downloading", amount_left=10)]
            for torrent_hash in hashes
        }
    )
    probe = FakeDiskProbe(stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=90)])
    handler = OnAddHandler(config, qb_client=qb, disk_probe=probe)

    app = create_http_app(handler)
    async with TestServer(app) as server:
        async with _test_client(server) as client:
            responses = await asyncio.gather(
                *(
                    client.post(
                        "/on-add",
                        data={"hash": torrent_hash},
                        headers=_auth_headers(),
                    )
                    for torrent_hash in hashes
                )
            )
            assert all(response.status == 202 for response in responses)
            await handler.shutdown()

    assert sorted(qb.pause_calls) == sorted(hashes)
    assert sorted(hash_value for hash_value, _ in qb.add_tag_calls) == sorted(hashes)


async def test_on_add_batches_quick_poll_fetches_for_parallel_hashes() -> None:
    """Tests that parallel quick-poll status fetches are sent in one request."""
    config = make_config(
        on_add_quick_poll_interval_seconds=0.01,
        on_add_quick_poll_max_attempts=1,
    )
    hashes = [f"{index:040x}" for index in range(8)]
    qb = FakeQbClient(
        torrent_lookup_sequence={
            torrent_hash: [torrent(torrent_hash, state="downloading", amount_left=10)]
            for torrent_hash in hashes
        }
    )
    probe = FakeDiskProbe(stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=90)])
    handler = OnAddHandler(config, qb_client=qb, disk_probe=probe)

    app = create_http_app(handler)
    async with TestServer(app) as server:
        async with _test_client(server) as client:
            responses = await asyncio.gather(
                *(
                    client.post(
                        "/on-add", data={"hash": torrent_hash}, headers=_auth_headers()
                    )
                    for torrent_hash in hashes
                )
            )
            assert all(response.status == 202 for response in responses)
            await handler.shutdown()

    assert len(qb.fetch_torrent_request_payloads) == 1
    flattened = [
        hash_value
        for payload in qb.fetch_torrent_request_payloads
        for hash_value in payload
    ]
    assert sorted(flattened) == sorted(hashes)


async def test_on_add_parallel_hashes_drop_removed_second_hash_only() -> None:
    """Tests that a missing second hash is dropped while other hashes still pause/tag."""
    config = make_config(
        on_add_quick_poll_interval_seconds=0.01,
        on_add_quick_poll_max_attempts=1,
    )
    hashes = [f"{index:040x}" for index in range(3)]
    second_hash = hashes[1]
    qb = FakeQbClient(
        torrent_lookup_sequence={
            hashes[0]: [torrent(hashes[0], state="downloading", amount_left=10)],
            second_hash: [None],
            hashes[2]: [torrent(hashes[2], state="downloading", amount_left=10)],
        }
    )
    probe = FakeDiskProbe(stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=90)])
    handler = OnAddHandler(config, qb_client=qb, disk_probe=probe)

    app = create_http_app(handler)
    async with TestServer(app) as server:
        async with _test_client(server) as client:
            responses = await asyncio.gather(
                *(
                    client.post(
                        "/on-add", data={"hash": torrent_hash}, headers=_auth_headers()
                    )
                    for torrent_hash in hashes
                )
            )
            assert all(response.status == 202 for response in responses)
            await handler.shutdown()

    assert len(qb.fetch_torrent_request_payloads) == 1
    flattened = [
        hash_value
        for payload in qb.fetch_torrent_request_payloads
        for hash_value in payload
    ]
    assert sorted(flattened) == sorted(hashes)

    assert second_hash not in qb.pause_calls
    assert second_hash not in {hash_value for hash_value, _ in qb.add_tag_calls}

    expected_survivors = [hashes[0], hashes[2]]
    assert sorted(qb.pause_calls) == sorted(expected_survivors)
    assert sorted(hash_value for hash_value, _ in qb.add_tag_calls) == sorted(
        expected_survivors
    )


async def test_on_add_deduplicates_quick_poll_for_same_hash() -> None:
    """Tests that on add deduplicates quick poll for same hash."""
    config = make_config(
        on_add_quick_poll_interval_seconds=0.05, on_add_quick_poll_max_attempts=2
    )
    qb = FakeQbClient(
        torrent_lookup_sequence={
            VALID_HASH_40: [
                torrent(VALID_HASH_40, state="metaDL", amount_left=0),
                torrent(VALID_HASH_40, state="downloading", amount_left=10),
            ]
        }
    )
    probe = FakeDiskProbe(stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=90)])
    handler = OnAddHandler(config, qb_client=qb, disk_probe=probe)

    app = create_http_app(handler)
    async with TestServer(app) as server:
        async with _test_client(server) as client:
            first, second = await asyncio.gather(
                client.post(
                    "/on-add", data={"hash": VALID_HASH_40}, headers=_auth_headers()
                ),
                client.post(
                    "/on-add", data={"hash": VALID_HASH_40}, headers=_auth_headers()
                ),
            )
            assert first.status == 202
            assert second.status == 202
            await handler.shutdown()

    assert qb.pause_calls == [VALID_HASH_40]
    assert qb.add_tag_calls == [(VALID_HASH_40, "diskguard_paused")]


async def test_on_add_deduplicates_hash_while_pause_and_tag_are_in_flight() -> None:
    """Tests that duplicate /on-add calls are deduped during pause/tag in-flight work."""
    config = make_config(
        on_add_quick_poll_interval_seconds=0.01, on_add_quick_poll_max_attempts=1
    )
    qb = PauseBlockingQbClient(VALID_HASH_40)
    probe = FakeDiskProbe(stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=90)])
    handler = OnAddHandler(config, qb_client=qb, disk_probe=probe)

    app = create_http_app(handler)
    async with TestServer(app) as server:
        async with _test_client(server) as client:
            first_response = await client.post(
                "/on-add",
                data={"hash": VALID_HASH_40},
                headers=_auth_headers(),
            )
            assert first_response.status == 202

            pause_started = await asyncio.to_thread(qb.wait_for_pause_started, 1.0)
            assert pause_started

            second_response = await client.post(
                "/on-add",
                data={"hash": VALID_HASH_40},
                headers=_auth_headers(),
            )
            second_payload = await second_response.json()
            assert second_response.status == 202
            assert second_payload["action"] == "quick_poll_already_scheduled"

            qb.release_pause()
            await handler.shutdown()

    assert qb.pause_calls == [VALID_HASH_40]
    assert qb.add_tag_calls == [(VALID_HASH_40, "diskguard_paused")]


async def test_on_add_worker_non_api_quick_poll_failure_does_not_stick_hash() -> None:
    """Tests that non-API quick-poll worker errors do not leave hashes permanently deduped."""
    config = make_config(
        on_add_quick_poll_interval_seconds=0.01, on_add_quick_poll_max_attempts=1
    )
    qb = FailFirstQuickPollQbClient(VALID_HASH_40)
    probe = FakeDiskProbe(stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=90)])
    handler = OnAddHandler(config, qb_client=qb, disk_probe=probe)

    app = create_http_app(handler)
    async with TestServer(app) as server:
        async with _test_client(server) as client:
            first_response = await client.post(
                "/on-add",
                data={"hash": VALID_HASH_40},
                headers=_auth_headers(),
            )
            assert first_response.status == 202

            await asyncio.sleep(0.05)

            second_response = await client.post(
                "/on-add",
                data={"hash": VALID_HASH_40},
                headers=_auth_headers(),
            )
            assert second_response.status == 202
            await handler.shutdown()

    assert qb.fetch_torrent_calls == [VALID_HASH_40, VALID_HASH_40]
    assert qb.pause_calls == [VALID_HASH_40]
    assert qb.add_tag_calls == [(VALID_HASH_40, "diskguard_paused")]


async def test_on_add_returns_429_when_quick_poll_queue_limit_reached() -> None:
    """Tests that on-add rejects new hashes when quick-poll queue is full."""
    config = make_config(
        on_add_quick_poll_max_attempts=1,
        on_add_quick_poll_max_queue_size=1,
    )
    qb = BlockingQbClient()
    probe = FakeDiskProbe(stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=90)])
    handler = OnAddHandler(config, qb_client=qb, disk_probe=probe)

    first_hash = "1" * 40
    second_hash = "2" * 40

    app = create_http_app(handler)
    async with TestServer(app) as server:
        async with _test_client(server) as client:
            first_response = await client.post(
                "/on-add",
                data={"hash": first_hash},
                headers=_auth_headers(),
            )
            assert first_response.status == 202

            second_response = await client.post(
                "/on-add",
                data={"hash": second_hash},
                headers=_auth_headers(),
            )
            payload = await second_response.json()
            assert second_response.status == 429
            assert payload["message"] == "on-add backlog limit reached"

            qb.release()
            await handler.shutdown()


async def test_on_add_logs_info_with_hash_on_hook_call(
    caplog,
) -> None:
    """Tests that on add logs info with hash on hook call."""
    caplog.set_level(logging.INFO)
    config = make_config()
    qb = FakeQbClient()
    probe = FakeDiskProbe(
        stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=500)]
    )
    logger = logging.getLogger("diskguard.api.test")
    handler = OnAddHandler(config, qb_client=qb, disk_probe=probe, logger=logger)

    app = create_http_app(handler)
    async with TestServer(app) as server:
        async with _test_client(server) as client:
            response = await client.post(
                "/on-add",
                data={"hash": VALID_HASH_40},
                headers=_auth_headers(),
            )
            assert response.status == 200

    on_add_logs = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO and "on_add triggered" in record.getMessage()
    ]
    assert len(on_add_logs) == 1
    assert f"hash={VALID_HASH_40}" in on_add_logs[0]
    assert "free_gb=" in on_add_logs[0]
    assert "used_gb=" in on_add_logs[0]
    assert "free_pct=" in on_add_logs[0]


async def test_on_add_logs_optional_name_and_category_when_present(
    caplog,
) -> None:
    """Tests that on add logs optional name and category when present."""
    caplog.set_level(logging.INFO)
    config = make_config()
    qb = FakeQbClient()
    probe = FakeDiskProbe(
        stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=500)]
    )
    logger = logging.getLogger("diskguard.api.test")
    handler = OnAddHandler(config, qb_client=qb, disk_probe=probe, logger=logger)

    app = create_http_app(handler)
    async with TestServer(app) as server:
        async with _test_client(server) as client:
            response = await client.post(
                "/on-add",
                data={"hash": VALID_HASH_40, "name": "Ubuntu ISO", "category": "linux"},
                headers=_auth_headers(),
            )
            assert response.status == 200

    on_add_logs = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO and "on_add triggered" in record.getMessage()
    ]
    assert len(on_add_logs) == 1
    assert f"hash={VALID_HASH_40}" in on_add_logs[0]
    assert "name=Ubuntu ISO" in on_add_logs[0]
    assert "category=linux" in on_add_logs[0]


async def test_on_add_quick_poll_waits_until_known_size_before_pause() -> None:
    """Tests that on add quick poll waits until known size before pause."""
    config = make_config(
        on_add_quick_poll_interval_seconds=0.01, on_add_quick_poll_max_attempts=3
    )
    qb = FakeQbClient(
        torrent_lookup_sequence={
            VALID_HASH_40: [
                torrent(VALID_HASH_40, state="metaDL", amount_left=0),
                torrent(VALID_HASH_40, state="metaDL", amount_left=None),
                torrent(VALID_HASH_40, state="downloading", amount_left=10),
            ]
        }
    )
    probe = FakeDiskProbe(stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=90)])
    handler = OnAddHandler(config, qb_client=qb, disk_probe=probe)

    app = create_http_app(handler)
    async with TestServer(app) as server:
        async with _test_client(server) as client:
            response = await client.post(
                "/on-add",
                data={"hash": VALID_HASH_40},
                headers=_auth_headers(),
            )
            assert response.status == 202
            await handler.shutdown()

    assert qb.fetch_torrent_calls == [VALID_HASH_40, VALID_HASH_40, VALID_HASH_40]
    assert qb.pause_calls == [VALID_HASH_40]
    assert qb.add_tag_calls == [(VALID_HASH_40, "diskguard_paused")]


async def test_on_add_quick_poll_pauses_known_size_even_if_not_downloading() -> None:
    """Tests that known-size torrents are paused in SOFT/HARD regardless of state."""
    config = make_config(
        on_add_quick_poll_interval_seconds=0.01, on_add_quick_poll_max_attempts=1
    )
    qb = FakeQbClient(
        torrent_lookup_sequence={
            VALID_HASH_40: [
                torrent(VALID_HASH_40, state="pausedDL", amount_left=10),
            ]
        }
    )
    probe = FakeDiskProbe(stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=90)])
    handler = OnAddHandler(config, qb_client=qb, disk_probe=probe)

    app = create_http_app(handler)
    async with TestServer(app) as server:
        async with _test_client(server) as client:
            response = await client.post(
                "/on-add",
                data={"hash": VALID_HASH_40},
                headers=_auth_headers(),
            )
            assert response.status == 202
            await handler.shutdown()

    assert qb.pause_calls == [VALID_HASH_40]
    assert qb.add_tag_calls == [(VALID_HASH_40, "diskguard_paused")]


async def test_on_add_quick_poll_skips_forced_download_with_known_size() -> None:
    """Tests that forcedDL torrents are removed from queue without pause/tag."""
    config = make_config(
        on_add_quick_poll_interval_seconds=0.01, on_add_quick_poll_max_attempts=1
    )
    qb = FakeQbClient(
        torrent_lookup_sequence={
            VALID_HASH_40: [
                torrent(VALID_HASH_40, state="forcedDL", amount_left=10),
            ]
        }
    )
    probe = FakeDiskProbe(stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=90)])
    handler = OnAddHandler(config, qb_client=qb, disk_probe=probe)

    app = create_http_app(handler)
    async with TestServer(app) as server:
        async with _test_client(server) as client:
            response = await client.post(
                "/on-add",
                data={"hash": VALID_HASH_40},
                headers=_auth_headers(),
            )
            assert response.status == 202
            await handler.shutdown()

    assert qb.pause_calls == []
    assert qb.add_tag_calls == []


async def test_on_add_quick_poll_drops_hash_when_missing_from_payload() -> None:
    """Tests that hashes missing from payload are removed from quick-poll queue."""
    config = make_config(
        on_add_quick_poll_interval_seconds=0.01, on_add_quick_poll_max_attempts=3
    )
    qb = FakeQbClient(
        torrent_lookup_sequence={
            VALID_HASH_40: [None],
        }
    )
    probe = FakeDiskProbe(stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=90)])
    handler = OnAddHandler(config, qb_client=qb, disk_probe=probe)

    app = create_http_app(handler)
    async with TestServer(app) as server:
        async with _test_client(server) as client:
            response = await client.post(
                "/on-add",
                data={"hash": VALID_HASH_40},
                headers=_auth_headers(),
            )
            assert response.status == 202
            await handler.shutdown()

    assert qb.fetch_torrent_calls == [VALID_HASH_40]
    assert qb.pause_calls == []
    assert qb.add_tag_calls == []


async def test_on_add_quick_poll_does_not_pause_when_size_stays_unknown() -> None:
    """Tests that on add quick poll does not pause when size stays unknown."""
    config = make_config(
        on_add_quick_poll_interval_seconds=0.01, on_add_quick_poll_max_attempts=3
    )
    qb = FakeQbClient(
        torrent_lookup_sequence={
            VALID_HASH_40: [
                torrent(VALID_HASH_40, state="metaDL", amount_left=0),
                torrent(VALID_HASH_40, state="metaDL", amount_left=None),
                torrent(VALID_HASH_40, state="metaDL", amount_left=0),
            ]
        }
    )
    probe = FakeDiskProbe(stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=90)])
    handler = OnAddHandler(config, qb_client=qb, disk_probe=probe)

    app = create_http_app(handler)
    async with TestServer(app) as server:
        async with _test_client(server) as client:
            response = await client.post(
                "/on-add",
                data={"hash": VALID_HASH_40},
                headers=_auth_headers(),
            )
            assert response.status == 202
            await handler.shutdown()

    assert qb.fetch_torrent_calls == [VALID_HASH_40, VALID_HASH_40, VALID_HASH_40]
    assert qb.pause_calls == []
    assert qb.add_tag_calls == []


async def test_on_add_respects_configured_max_request_body_size() -> None:
    """Tests that create_http_app enforces configured max request body size."""
    config = make_config(on_add_max_body_bytes=64)
    qb = FakeQbClient()
    probe = FakeDiskProbe(
        stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=500)]
    )
    handler = OnAddHandler(config, qb_client=qb, disk_probe=probe)

    app = create_http_app(handler)
    async with TestServer(app) as server:
        async with _test_client(server) as client:
            response = await client.post(
                "/on-add",
                data={"hash": VALID_HASH_40, "name": "x" * 512},
                headers=_auth_headers(),
            )

    assert response.status == 413
