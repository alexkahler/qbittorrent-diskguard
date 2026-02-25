"""Custom exceptions for DiskGuard."""


class DiskGuardError(Exception):
    """Base class for DiskGuard errors."""


class ConfigError(DiskGuardError):
    """Configuration parsing or validation error."""


class StartupPreflightError(DiskGuardError):
    """Startup preflight check failed before service began normal operations."""


class DiskProbeError(DiskGuardError):
    """Disk probe error for an invalid or inaccessible watch path."""


class QbittorrentError(DiskGuardError):
    """Base class for qBittorrent API errors."""


class QbittorrentAuthenticationError(QbittorrentError):
    """Authentication or session failure against qBittorrent."""


class QbittorrentUnavailableError(QbittorrentError):
    """Network or timeout failure communicating with qBittorrent."""


class QbittorrentRequestError(QbittorrentError):
    """Unexpected or invalid qBittorrent API response."""
