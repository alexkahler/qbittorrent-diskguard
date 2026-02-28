"""Tests for qBittorrent helper utilities."""

from __future__ import annotations

from typing import Any

import qbittorrentapi

from diskguard.config import QbittorrentConfig
from diskguard.qbittorrent import build_qbittorrent_client


def _config(*, url: str = "http://qbittorrent:8080") -> QbittorrentConfig:
    """Builds qB config used by tests."""
    return QbittorrentConfig(
        url=url,
        username="admin",
        password="password",
        connect_timeout_seconds=2.0,
        read_timeout_seconds=8.0,
    )


def test_build_client_passes_expected_qbittorrent_api_options(
    monkeypatch,
) -> None:
    """Tests that helper initializes qbittorrent-api with expected settings."""
    captured: dict[str, Any] = {}

    def _fake_client(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("diskguard.qbittorrent.qbittorrentapi.Client", _fake_client)

    build_qbittorrent_client(_config())

    assert captured["host"] == "http://qbittorrent:8080"
    assert captured["username"] == "admin"
    assert captured["password"] == "password"
    assert captured["RAISE_NOTIMPLEMENTEDERROR_FOR_UNIMPLEMENTED_API_ENDPOINTS"] is True
    assert captured["RAISE_ERROR_FOR_UNSUPPORTED_QBITTORRENT_VERSIONS"] is True
    assert captured["REQUESTS_ARGS"] == {"timeout": (2.0, 8.0)}


def test_http_403_is_qbittorrent_api_error() -> None:
    """Tests expected qbittorrent-api exception hierarchy used by call-sites."""
    exc = qbittorrentapi.HTTP403Error("Forbidden")
    assert isinstance(exc, qbittorrentapi.APIError)
