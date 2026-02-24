"""Disk measurement helpers."""

from __future__ import annotations

import os

from diskguard.errors import DiskProbeError
from diskguard.models import DiskStats


class DiskProbe:
    """Reads disk statistics for the configured watch path."""

    def __init__(self, watch_path: str):
        self._watch_path = watch_path

    @property
    def watch_path(self) -> str:
        """Returns the configured watch path."""
        return self._watch_path

    def measure(self) -> DiskStats:
        """Returns total bytes, free bytes, and free percentage."""
        statvfs_fn = getattr(os, "statvfs", None)
        if statvfs_fn is None:
            raise DiskProbeError("os.statvfs is unavailable on this platform")

        try:
            stat = statvfs_fn(self._watch_path)
        except OSError as exc:
            raise DiskProbeError(f"Unable to read watch path {self._watch_path!r}: {exc}") from exc

        total_bytes = int(stat.f_frsize * stat.f_blocks)
        free_bytes = int(stat.f_frsize * stat.f_bavail)
        if total_bytes <= 0:
            raise DiskProbeError(f"Watch path {self._watch_path!r} reported invalid total capacity")

        free_pct = (free_bytes / total_bytes) * 100.0
        return DiskStats(total_bytes=total_bytes, free_bytes=free_bytes, free_pct=free_pct)
