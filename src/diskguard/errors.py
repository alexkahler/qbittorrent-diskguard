"""Custom exceptions for DiskGuard."""


class DiskGuardError(Exception):
    """Base class for DiskGuard errors."""


class ConfigError(DiskGuardError):
    """Configuration parsing or validation error."""


class StartupPreflightError(DiskGuardError):
    """Startup preflight check failed before service began normal operations."""


class DiskProbeError(DiskGuardError):
    """Disk probe error for an invalid or inaccessible watch path."""
