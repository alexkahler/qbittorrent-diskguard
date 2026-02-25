"""Tests for the /on-add fast path endpoint."""

import asyncio
import logging

from aiohttp.test_utils import TestClient, TestServer

from diskguard.api import OnAddHandler, create_http_app
from tests.helpers import FakeDiskProbe, FakeQbClient, disk_stats, make_config


async def test_on_add_in_normal_mode_does_nothing() -> None:
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
    config = make_config()
    qb = FakeQbClient()
    probe = FakeDiskProbe(stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=90)])
    handler = OnAddHandler(config, qb_client=qb, disk_probe=probe)

    app = create_http_app(handler)
    async with TestServer(app) as server:
        async with TestClient(server) as client:
            response = await client.post("/on-add", data={"hash": "abc"})
            assert response.status == 202
            await handler.shutdown()

    assert qb.pause_calls == ["abc"]
    assert qb.add_tag_calls == [("abc", "diskguard_paused")]


async def test_on_add_requires_hash() -> None:
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
    config = make_config()
    qb = FakeQbClient(fail_pause={"abc"})
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
    config = make_config()
    qb = FakeQbClient()
    probe = FakeDiskProbe(stats_sequence=[disk_stats(total_bytes=1_000, free_bytes=90)])
    handler = OnAddHandler(config, qb_client=qb, disk_probe=probe)

    app = create_http_app(handler)
    hashes = [f"h{i:02d}" for i in range(20)]
    async with TestServer(app) as server:
        async with TestClient(server) as client:
            responses = await asyncio.gather(
                *(client.post("/on-add", data={"hash": torrent_hash}) for torrent_hash in hashes)
            )
            assert all(response.status == 202 for response in responses)
            await handler.shutdown()

    assert sorted(qb.pause_calls) == sorted(hashes)
    assert sorted(hash_value for hash_value, _ in qb.add_tag_calls) == sorted(hashes)


async def test_on_add_logs_info_with_hash_on_hook_call(
    caplog,
) -> None:
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
