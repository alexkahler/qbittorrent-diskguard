"""Tests for the /on-add fast path endpoint."""

import asyncio
import logging

import pytest
from aiohttp.test_utils import TestClient, TestServer

from diskguard.api import OnAddHandler, create_http_app
from tests.helpers import FakeDiskProbe, FakeQbClient, disk_stats, make_config, torrent

AUTH_TOKEN = "test-token"
VALID_HASH_40 = "a" * 40
VALID_HASH_64 = "b" * 64


def _auth_headers(token: str = AUTH_TOKEN) -> dict[str, str]:
    """Returns auth headers for on-add endpoint requests."""
    return {"X-DiskGuard-Token": token}


class BlockingQbClient(FakeQbClient):
    """Fake qB client that blocks fetch_torrent_by_hash until released."""

    def __init__(self) -> None:
        super().__init__()
        self._release_event = asyncio.Event()

    async def fetch_torrent_by_hash(self, torrent_hash: str):  # type: ignore[override]
        """Blocks until release and then returns a known-size snapshot."""
        self.fetch_torrent_calls.append(torrent_hash)
        await self._release_event.wait()
        return torrent(torrent_hash, state="downloading", amount_left=10)

    def release(self) -> None:
        """Releases blocked quick-poll calls."""
        self._release_event.set()


async def test_on_add_rejects_missing_auth_token() -> None:
    """Tests that on-add rejects requests without shared-secret header."""
    config = make_config()
    qb = FakeQbClient()
    probe = FakeDiskProbe(stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=500)])
    handler = OnAddHandler(config, qb_client=qb, disk_probe=probe)

    app = create_http_app(handler)
    async with TestServer(app) as server:
        async with TestClient(server) as client:
            response = await client.post("/on-add", data={"hash": VALID_HASH_40})
            payload = await response.json()

    assert response.status == 401
    assert payload["message"] == "unauthorized"
    assert qb.pause_calls == []


async def test_on_add_rejects_invalid_auth_token() -> None:
    """Tests that on-add rejects requests with incorrect shared-secret header."""
    config = make_config()
    qb = FakeQbClient()
    probe = FakeDiskProbe(stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=500)])
    handler = OnAddHandler(config, qb_client=qb, disk_probe=probe)

    app = create_http_app(handler)
    async with TestServer(app) as server:
        async with TestClient(server) as client:
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
    probe = FakeDiskProbe(stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=500)])
    handler = OnAddHandler(config, qb_client=qb, disk_probe=probe)

    app = create_http_app(handler)
    async with TestServer(app) as server:
        async with TestClient(server) as client:
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
    probe = FakeDiskProbe(stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=500)])
    handler = OnAddHandler(config, qb_client=qb, disk_probe=probe)

    app = create_http_app(handler)
    async with TestServer(app) as server:
        async with TestClient(server) as client:
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
    config = make_config(on_add_quick_poll_interval_seconds=0.01, on_add_quick_poll_max_attempts=2)
    qb = FakeQbClient(
        torrent_lookup_sequence={
            VALID_HASH_40: [torrent(VALID_HASH_40, state="downloading", amount_left=10)],
        }
    )
    probe = FakeDiskProbe(stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=90)])
    handler = OnAddHandler(config, qb_client=qb, disk_probe=probe)

    app = create_http_app(handler)
    async with TestServer(app) as server:
        async with TestClient(server) as client:
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
        async with TestClient(server) as client:
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
    probe = FakeDiskProbe(stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=500)])
    handler = OnAddHandler(config, qb_client=qb, disk_probe=probe)

    app = create_http_app(handler)
    async with TestServer(app) as server:
        async with TestClient(server) as client:
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
    config = make_config(on_add_quick_poll_interval_seconds=0.01, on_add_quick_poll_max_attempts=1)
    qb = FakeQbClient(
        torrent_lookup_sequence={
            VALID_HASH_40: [torrent(VALID_HASH_40, state="downloading", amount_left=10)],
        },
        fail_pause={VALID_HASH_40},
    )
    probe = FakeDiskProbe(stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=90)])
    handler = OnAddHandler(config, qb_client=qb, disk_probe=probe)

    app = create_http_app(handler)
    async with TestServer(app) as server:
        async with TestClient(server) as client:
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
        on_add_quick_poll_max_concurrency=4,
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
        async with TestClient(server) as client:
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


async def test_on_add_deduplicates_quick_poll_for_same_hash() -> None:
    """Tests that on add deduplicates quick poll for same hash."""
    config = make_config(on_add_quick_poll_interval_seconds=0.05, on_add_quick_poll_max_attempts=2)
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
        async with TestClient(server) as client:
            first, second = await asyncio.gather(
                client.post("/on-add", data={"hash": VALID_HASH_40}, headers=_auth_headers()),
                client.post("/on-add", data={"hash": VALID_HASH_40}, headers=_auth_headers()),
            )
            assert first.status == 202
            assert second.status == 202
            await handler.shutdown()

    assert qb.pause_calls == [VALID_HASH_40]
    assert qb.add_tag_calls == [(VALID_HASH_40, "diskguard_paused")]


async def test_on_add_returns_429_when_pending_task_limit_reached() -> None:
    """Tests that on-add rejects new hashes when pending quick-poll queue is full."""
    config = make_config(
        on_add_quick_poll_max_attempts=1,
        on_add_max_pending_tasks=1,
    )
    qb = BlockingQbClient()
    probe = FakeDiskProbe(stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=90)])
    handler = OnAddHandler(config, qb_client=qb, disk_probe=probe)

    first_hash = "1" * 40
    second_hash = "2" * 40

    app = create_http_app(handler)
    async with TestServer(app) as server:
        async with TestClient(server) as client:
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
    probe = FakeDiskProbe(stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=500)])
    logger = logging.getLogger("diskguard.api.test")
    handler = OnAddHandler(config, qb_client=qb, disk_probe=probe, logger=logger)

    app = create_http_app(handler)
    async with TestServer(app) as server:
        async with TestClient(server) as client:
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
    probe = FakeDiskProbe(stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=500)])
    logger = logging.getLogger("diskguard.api.test")
    handler = OnAddHandler(config, qb_client=qb, disk_probe=probe, logger=logger)

    app = create_http_app(handler)
    async with TestServer(app) as server:
        async with TestClient(server) as client:
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
    config = make_config(on_add_quick_poll_interval_seconds=0.01, on_add_quick_poll_max_attempts=3)
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
        async with TestClient(server) as client:
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


async def test_on_add_quick_poll_does_not_pause_when_size_stays_unknown() -> None:
    """Tests that on add quick poll does not pause when size stays unknown."""
    config = make_config(on_add_quick_poll_interval_seconds=0.01, on_add_quick_poll_max_attempts=3)
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
        async with TestClient(server) as client:
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
    probe = FakeDiskProbe(stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=500)])
    handler = OnAddHandler(config, qb_client=qb, disk_probe=probe)

    app = create_http_app(handler)
    async with TestServer(app) as server:
        async with TestClient(server) as client:
            response = await client.post(
                "/on-add",
                data={"hash": VALID_HASH_40, "name": "x" * 512},
                headers=_auth_headers(),
            )

    assert response.status == 413
