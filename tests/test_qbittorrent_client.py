"""Tests for qBittorrent API client robustness."""

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from diskguard.config import QbittorrentConfig
from diskguard.errors import QbittorrentRequestError
from diskguard.qbittorrent import QbittorrentClient


def test_build_endpoint_joins_base_url_and_path_without_double_slash() -> None:
    config = QbittorrentConfig(
        url="http://qb:8080/",
        username="admin",
        password="password",
    )
    client = QbittorrentClient(config)

    assert (
        client._build_endpoint("/api/v2/torrents/stop")  # noqa: SLF001
        == "http://qb:8080/api/v2/torrents/stop"
    )


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


async def test_fetch_application_version_uses_authenticated_endpoint() -> None:
    state = {
        "login_calls": 0,
        "version_calls": 0,
    }

    async def login_handler(_: web.Request) -> web.Response:
        state["login_calls"] += 1
        return web.Response(text="Ok.")

    async def version_handler(_: web.Request) -> web.Response:
        state["version_calls"] += 1
        return web.Response(text="4.6.5")

    app = web.Application()
    app.router.add_post("/api/v2/auth/login", login_handler)
    app.router.add_get("/api/v2/app/version", version_handler)

    async with TestServer(app) as server:
        config = QbittorrentConfig(
            url=str(server.make_url("/")).rstrip("/"),
            username="admin",
            password="password",
        )
        client = QbittorrentClient(config)
        try:
            version = await client.fetch_application_version()
        finally:
            await client.close()

    assert state["login_calls"] == 1
    assert state["version_calls"] == 1
    assert version == "4.6.5"


async def test_fetch_webapi_version_uses_authenticated_endpoint() -> None:
    state = {
        "login_calls": 0,
        "webapi_version_calls": 0,
    }

    async def login_handler(_: web.Request) -> web.Response:
        state["login_calls"] += 1
        return web.Response(text="Ok.")

    async def webapi_version_handler(_: web.Request) -> web.Response:
        state["webapi_version_calls"] += 1
        return web.Response(text="2.11.3")

    app = web.Application()
    app.router.add_post("/api/v2/auth/login", login_handler)
    app.router.add_get("/api/v2/app/webapiVersion", webapi_version_handler)

    async with TestServer(app) as server:
        config = QbittorrentConfig(
            url=str(server.make_url("/")).rstrip("/"),
            username="admin",
            password="password",
        )
        client = QbittorrentClient(config)
        try:
            webapi_version = await client.fetch_webapi_version()
        finally:
            await client.close()

    assert state["login_calls"] == 1
    assert state["webapi_version_calls"] == 1
    assert webapi_version == "2.11.3"


async def test_pause_resume_and_tag_operations_hit_expected_endpoints() -> None:
    captured: dict[str, dict[str, object]] = {}

    async def login_handler(_: web.Request) -> web.Response:
        return web.Response(text="Ok.")

    def make_action_handler(action_name: str):
        async def handler(request: web.Request) -> web.Response:
            form_data = await request.post()
            captured[action_name] = {
                "method": request.method,
                "path_qs": request.path_qs,
                "raw_path": request.raw_path,
                "query": dict(request.query),
                "form": {key: str(value) for key, value in form_data.items()},
            }
            return web.Response(text="")

        return handler

    app = web.Application()
    app.router.add_post("/api/v2/auth/login", login_handler)
    app.router.add_post("/api/v2/torrents/stop", make_action_handler("pause"))
    app.router.add_post("/api/v2/torrents/start", make_action_handler("resume"))
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

    assert captured["pause"]["method"] == "POST"
    assert captured["pause"]["path_qs"] == "/api/v2/torrents/stop?hashes=hash1"
    assert captured["pause"]["query"] == {"hashes": "hash1"}
    assert captured["pause"]["form"] == {}

    assert captured["resume"]["method"] == "POST"
    assert captured["resume"]["path_qs"] == "/api/v2/torrents/start?hashes=hash1"
    assert captured["resume"]["query"] == {"hashes": "hash1"}
    assert captured["resume"]["form"] == {}

    assert captured["addTags"]["method"] == "POST"
    assert captured["addTags"]["path_qs"] == "/api/v2/torrents/addTags"
    assert captured["addTags"]["query"] == {}
    assert captured["addTags"]["form"] == {"hashes": "hash1", "tags": "diskguard_paused"}

    assert captured["removeTags"]["method"] == "POST"
    assert captured["removeTags"]["path_qs"] == "/api/v2/torrents/removeTags"
    assert captured["removeTags"]["query"] == {}
    assert captured["removeTags"]["form"] == {"hashes": "hash1", "tags": "diskguard_paused"}


async def test_pause_and_resume_allow_pipe_delimited_hash_list() -> None:
    captured: dict[str, dict[str, str]] = {}

    async def login_handler(_: web.Request) -> web.Response:
        return web.Response(text="Ok.")

    def make_action_handler(action_name: str):
        async def handler(request: web.Request) -> web.Response:
            captured[action_name] = {
                "raw_path": request.raw_path,
                "hashes": request.query.get("hashes", ""),
            }
            return web.Response(text="")

        return handler

    app = web.Application()
    app.router.add_post("/api/v2/auth/login", login_handler)
    app.router.add_post("/api/v2/torrents/start", make_action_handler("resume"))
    app.router.add_post("/api/v2/torrents/stop", make_action_handler("pause"))

    async with TestServer(app) as server:
        config = QbittorrentConfig(
            url=str(server.make_url("/")).rstrip("/"),
            username="admin",
            password="password",
        )
        client = QbittorrentClient(config)
        try:
            await client.pause_torrent("hash1|hash2")
            await client.resume_torrent("hash1|hash2")
        finally:
            await client.close()

    assert captured["pause"]["hashes"] == "hash1|hash2"
    assert captured["resume"]["hashes"] == "hash1|hash2"
    assert "%7C" in captured["pause"]["raw_path"]
    assert "%7C" in captured["resume"]["raw_path"]


async def test_pause_404_error_includes_final_endpoint_and_detected_versions() -> None:
    async def login_handler(_: web.Request) -> web.Response:
        return web.Response(text="Ok.")

    async def pause_handler(_: web.Request) -> web.Response:
        return web.Response(
            status=404,
            text="Not Found",
            headers={"Server": "qBittorrent/4.6.4", "X-WebAPI-Version": "2.8.19"},
        )

    app = web.Application()
    app.router.add_post("/api/v2/auth/login", login_handler)
    app.router.add_post("/api/v2/torrents/stop", pause_handler)

    async with TestServer(app) as server:
        base_url = str(server.make_url("/")).rstrip("/")
        config = QbittorrentConfig(
            url=base_url,
            username="admin",
            password="password",
        )
        client = QbittorrentClient(config)
        try:
            with pytest.raises(QbittorrentRequestError) as exc_info:
                await client.pause_torrent("hash404")
        finally:
            await client.close()

    message = str(exc_info.value)
    assert f"{base_url}/api/v2/torrents/stop?hashes=hash404" in message
    assert "status 404: Not Found" in message
    assert "detected versions: qBittorrent=4.6.4, webapi=2.8.19" in message
