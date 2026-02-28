"""qBittorrent helpers backed by qbittorrent-api."""

from __future__ import annotations

import qbittorrentapi

from diskguard.config import QbittorrentConfig


def build_qbittorrent_client(config: QbittorrentConfig) -> qbittorrentapi.Client:
    """Builds a configured qbittorrent-api client instance.

    Args:
        config: qBittorrent connection settings.

    Returns:
        Configured qbittorrent-api client.
    """
    # TODO: Add centralized redaction for qbittorrent-api exception text before logging call-site errors.
    # qbittorrentapi.Client signature validated via introspection.
    return qbittorrentapi.Client(
        host=config.url,
        username=config.username,
        password=config.password,
        REQUESTS_ARGS={
            "timeout": (
                config.connect_timeout_seconds,
                config.read_timeout_seconds,
            )
        },
        RAISE_NOTIMPLEMENTEDERROR_FOR_UNIMPLEMENTED_API_ENDPOINTS=True,
        RAISE_ERROR_FOR_UNSUPPORTED_QBITTORRENT_VERSIONS=True,
    )
