"""Tests for qBittorrent API client robustness."""

from aiohttp import web
from aiohttp.test_utils import TestServer

from diskguard.config import QbittorrentConfig
from diskguard.qbittorrent import QbittorrentClient


async def test_fetch_torrents_retries_once_after_403_with_relogin() -> None:
    state = {
        "login_calls": 0,
        "info_calls": 0,
    }

    async def login_handler(_: web.Request) -> web.Response:
        state["login_calls"] += 1
        return web.Response(text="Ok.")

    async def info_handler(_: web.Request) -> web.StreamResponse:
        state["info_calls"] += 1
        if state["info_calls"] == 1:
            return web.Response(status=403, text="Forbidden")
        return web.json_response(
            [
                {
                    "hash": "abc123",
                    "state": "pausedDL",
                    "amount_left": 42,
                    "priority": 5,
                    "added_on": 100,
                    "tags": "diskguard_paused,soft_allowed",
                }
            ]
        )

    app = web.Application()
    app.router.add_post("/api/v2/auth/login", login_handler)
    app.router.add_get("/api/v2/torrents/info", info_handler)

    async with TestServer(app) as server:
        config = QbittorrentConfig(
            url=str(server.make_url("/")).rstrip("/"),
            username="admin",
            password="password",
        )
        client = QbittorrentClient(config)
        try:
            torrents = await client.fetch_torrents()
        finally:
            await client.close()

    assert state["login_calls"] == 2
    assert state["info_calls"] == 2
    assert len(torrents) == 1
    assert torrents[0].hash == "abc123"
    assert torrents[0].tags == frozenset({"diskguard_paused", "soft_allowed"})


async def test_pause_resume_and_tag_operations_hit_expected_endpoints() -> None:
    captured: dict[str, dict[str, str]] = {}

    async def login_handler(_: web.Request) -> web.Response:
        return web.Response(text="Ok.")

    def make_action_handler(action_name: str):
        async def handler(request: web.Request) -> web.Response:
            form_data = await request.post()
            captured[action_name] = {key: str(value) for key, value in form_data.items()}
            return web.Response(text="")

        return handler

    app = web.Application()
    app.router.add_post("/api/v2/auth/login", login_handler)
    app.router.add_post("/api/v2/torrents/pause", make_action_handler("pause"))
    app.router.add_post("/api/v2/torrents/resume", make_action_handler("resume"))
    app.router.add_post("/api/v2/torrents/addTags", make_action_handler("addTags"))
    app.router.add_post("/api/v2/torrents/removeTags", make_action_handler("removeTags"))

    async with TestServer(app) as server:
        config = QbittorrentConfig(
            url=str(server.make_url("/")).rstrip("/"),
            username="admin",
            password="password",
        )
        client = QbittorrentClient(config)
        try:
            await client.pause_torrent("hash1")
            await client.resume_torrent("hash1")
            await client.add_tag("hash1", "diskguard_paused")
            await client.remove_tag("hash1", "diskguard_paused")
        finally:
            await client.close()

    assert captured["pause"] == {"hashes": "hash1"}
    assert captured["resume"] == {"hashes": "hash1"}
    assert captured["addTags"] == {"hashes": "hash1", "tags": "diskguard_paused"}
    assert captured["removeTags"] == {"hashes": "hash1", "tags": "diskguard_paused"}
