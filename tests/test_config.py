"""Tests for config loading and env overrides."""

from pathlib import Path

import pytest

from diskguard.config import load_config
from diskguard.errors import ConfigError


def test_load_config_reads_defaults(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[qbittorrent]
url = "http://qbittorrent:8080"
username = "admin"
password = "password"

[disk]
watch_path = "/downloads"
""".strip(),
        encoding="utf-8",
    )

    config = load_config(str(config_file))
    assert config.qbittorrent.url == "http://qbittorrent:8080"
    assert config.disk.watch_path == "/downloads"
    assert config.disk.soft_pause_below_pct == 10.0
    assert config.resume.policy.value == "priority_fifo"
    assert config.server.port == 7070


def test_load_config_defaults_watch_path_when_missing(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[qbittorrent]
url = "http://qbittorrent:8080"
username = "admin"
password = "password"
""".strip(),
        encoding="utf-8",
    )

    config = load_config(str(config_file))
    assert config.disk.watch_path == "/downloads"


def test_load_config_applies_flat_env_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[qbittorrent]
url = "http://qbittorrent:8080"
username = "admin"
password = "password"

[disk]
watch_path = "/downloads"

[resume]
policy = "priority_fifo"
strict_fifo = true
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setenv("DISKGUARD_RESUME_POLICY", "smallest_first")
    monkeypatch.setenv("DISKGUARD_RESUME_STRICT_FIFO", "false")
    monkeypatch.setenv("DISKGUARD_POLLING_INTERVAL_SECONDS", "45")
    monkeypatch.setenv("DISKGUARD_DISK_DOWNLOADING_STATES", "downloading,metaDL")

    config = load_config(str(config_file))
    assert config.resume.policy.value == "smallest_first"
    assert config.resume.strict_fifo is False
    assert config.polling.interval_seconds == 45
    assert config.disk.downloading_states == ("downloading", "metaDL")


def test_load_config_rejects_invalid_thresholds(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[qbittorrent]
url = "http://qbittorrent:8080"
username = "admin"
password = "password"

[disk]
watch_path = "/downloads"
soft_pause_below_pct = 5
hard_pause_below_pct = 5
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError):
        load_config(str(config_file))
