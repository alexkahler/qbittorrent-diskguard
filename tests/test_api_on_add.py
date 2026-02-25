"""Tests for the /on-add fast path endpoint."""

import asyncio
import logging

from aiohttp.test_utils import TestClient, TestServer

from diskguard.api import OnAddHandler, create_http_app
from tests.helpers import FakeDiskProbe, FakeQbClient, disk_stats, make_config, torrent


async def test_on_add_in_normal_mode_does_nothing() -> None:
    """Tests that on add in normal mode does nothing."""
    config = make_config()
    qb = FakeQbClient()
    probe = FakeDiskProbe(stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=500)])
    handler = OnAddHandler(config, qb_client=qb, disk_probe=probe)

    app = create_http_app(handler)
    async with TestServer(app) as server:
        async with TestClient(server) as client:
            response = await client.post("/on-add", data={"hash": "abc"})
            payload = await response.json()

    assert response.status == 200
    assert payload["action"] == "none"
    assert qb.pause_calls == []
    assert qb.add_tag_calls == []


async def test_on_add_in_soft_mode_pauses_and_tags_hash() -> None:
    """Tests that on add in soft mode pauses and tags hash."""
    config = make_config(on_add_quick_poll_interval_seconds=0.01, on_add_quick_poll_max_attempts=2)
    qb = FakeQbClient(
        torrent_lookup_sequence={
            "abc": [torrent("abc", state="downloading", amount_left=10)],
        }
    )
    probe = FakeDiskProbe(stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=90)])
    handler = OnAddHandler(config, qb_client=qb, disk_probe=probe)

    app = create_http_app(handler)
    async with TestServer(app) as server:
        async with TestClient(server) as client:
            response = await client.post("/on-add", data={"hash": "abc"})
            assert response.status == 202
            payload = await response.json()
            assert payload["action"] == "quick_poll_pause_and_mark"
            await handler.shutdown()

    assert qb.pause_calls == ["abc"]
    assert qb.add_tag_calls == [("abc", "diskguard_paused")]


async def test_on_add_requires_hash() -> None:
    """Tests that on add requires hash."""
    config = make_config()
    qb = FakeQbClient()
    probe = FakeDiskProbe(stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=90)])
    handler = OnAddHandler(config, qb_client=qb, disk_probe=probe)

    app = create_http_app(handler)
    async with TestServer(app) as server:
        async with TestClient(server) as client:
            response = await client.post("/on-add", data={})
            payload = await response.json()

    assert response.status == 400
    assert payload["message"] == "hash is required"


async def test_on_add_returns_accepted_when_qb_is_temporarily_unavailable() -> None:
    """Tests that on add returns accepted when qb is temporarily unavailable."""
    config = make_config(on_add_quick_poll_interval_seconds=0.01, on_add_quick_poll_max_attempts=1)
    qb = FakeQbClient(
        torrent_lookup_sequence={
            "abc": [torrent("abc", state="downloading", amount_left=10)],
        },
        fail_pause={"abc"},
    )
    probe = FakeDiskProbe(stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=90)])
    handler = OnAddHandler(config, qb_client=qb, disk_probe=probe)

    app = create_http_app(handler)
    async with TestServer(app) as server:
        async with TestClient(server) as client:
            response = await client.post("/on-add", data={"hash": "abc"})
            assert response.status == 202
            await handler.shutdown()

    assert qb.pause_calls == ["abc"]
    assert qb.add_tag_calls == []


async def test_on_add_handles_parallel_calls_without_state_corruption() -> None:
    """Tests that on add handles parallel calls without state corruption."""
    config = make_config(
        on_add_quick_poll_interval_seconds=0.01,
        on_add_quick_poll_max_attempts=2,
        on_add_quick_poll_max_concurrency=4,
    )
    hashes = [f"h{i:02d}" for i in range(20)]
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
                *(client.post("/on-add", data={"hash": torrent_hash}) for torrent_hash in hashes)
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
            "abc": [
                torrent("abc", state="metaDL", amount_left=0),
                torrent("abc", state="downloading", amount_left=10),
            ]
        }
    )
    probe = FakeDiskProbe(stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=90)])
    handler = OnAddHandler(config, qb_client=qb, disk_probe=probe)

    app = create_http_app(handler)
    async with TestServer(app) as server:
        async with TestClient(server) as client:
            first, second = await asyncio.gather(
                client.post("/on-add", data={"hash": "abc"}),
                client.post("/on-add", data={"hash": "abc"}),
            )
            assert first.status == 202
            assert second.status == 202
            await handler.shutdown()

    assert qb.pause_calls == ["abc"]
    assert qb.add_tag_calls == [("abc", "diskguard_paused")]


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
            response = await client.post("/on-add", data={"hash": "abc"})
            assert response.status == 200

    on_add_logs = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO and "on_add triggered" in record.getMessage()
    ]
    assert len(on_add_logs) == 1
    assert "hash=abc" in on_add_logs[0]
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
                data={"hash": "abc", "name": "Ubuntu ISO", "category": "linux"},
            )
            assert response.status == 200

    on_add_logs = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO and "on_add triggered" in record.getMessage()
    ]
    assert len(on_add_logs) == 1
    assert "hash=abc" in on_add_logs[0]
    assert "name=Ubuntu ISO" in on_add_logs[0]
    assert "category=linux" in on_add_logs[0]


async def test_on_add_quick_poll_waits_until_known_size_before_pause() -> None:
    """Tests that on add quick poll waits until known size before pause."""
    config = make_config(on_add_quick_poll_interval_seconds=0.01, on_add_quick_poll_max_attempts=3)
    qb = FakeQbClient(
        torrent_lookup_sequence={
            "abc": [
                torrent("abc", state="metaDL", amount_left=0),
                torrent("abc", state="metaDL", amount_left=None),
                torrent("abc", state="downloading", amount_left=10),
            ]
        }
    )
    probe = FakeDiskProbe(stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=90)])
    handler = OnAddHandler(config, qb_client=qb, disk_probe=probe)

    app = create_http_app(handler)
    async with TestServer(app) as server:
        async with TestClient(server) as client:
            response = await client.post("/on-add", data={"hash": "abc"})
            assert response.status == 202
            await handler.shutdown()

    assert qb.fetch_torrent_calls == ["abc", "abc", "abc"]
    assert qb.pause_calls == ["abc"]
    assert qb.add_tag_calls == [("abc", "diskguard_paused")]


async def test_on_add_quick_poll_does_not_pause_when_size_stays_unknown() -> None:
    """Tests that on add quick poll does not pause when size stays unknown."""
    config = make_config(on_add_quick_poll_interval_seconds=0.01, on_add_quick_poll_max_attempts=3)
    qb = FakeQbClient(
        torrent_lookup_sequence={
            "abc": [
                torrent("abc", state="metaDL", amount_left=0),
                torrent("abc", state="metaDL", amount_left=None),
                torrent("abc", state="metaDL", amount_left=0),
            ]
        }
    )
    probe = FakeDiskProbe(stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=90)])
    handler = OnAddHandler(config, qb_client=qb, disk_probe=probe)

    app = create_http_app(handler)
    async with TestServer(app) as server:
        async with TestClient(server) as client:
            response = await client.post("/on-add", data={"hash": "abc"})
            assert response.status == 202
            await handler.shutdown()

    assert qb.fetch_torrent_calls == ["abc", "abc", "abc"]
    assert qb.pause_calls == []
    assert qb.add_tag_calls == []
