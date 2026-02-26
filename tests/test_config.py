"""Tests for config loading and env overrides."""

import logging
from pathlib import Path

import pytest

import diskguard.config as config_module
from diskguard.config import load_config
from diskguard.errors import ConfigError


def _minimal_config(*, password: str = "secret", token: str = "test-token") -> str:
    """Builds a minimal valid config TOML document for tests."""
    return f"""
[qbittorrent]
url = "http://qbittorrent:8080"
username = "admin"
password = "{password}"

[server]
on_add_auth_token = "{token}"
""".strip()


def test_load_config_reads_defaults(tmp_path: Path) -> None:
    """Tests that load config reads defaults."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(_minimal_config(), encoding="utf-8")

    config = load_config(str(config_file))
    assert config.qbittorrent.url == "http://qbittorrent:8080"
    assert config.disk.watch_path == "/downloads"
    assert config.disk.soft_pause_below_pct == 10.0
    assert config.resume.policy.value == "priority_fifo"
    assert config.server.port == 7070
    assert config.server.on_add_auth_token == "test-token"
    assert config.server.on_add_max_body_bytes == 8192
    assert config.polling.on_add_quick_poll_interval_seconds == 1.0
    assert config.polling.on_add_quick_poll_max_attempts == 10
    assert config.polling.on_add_quick_poll_max_concurrency == 32
    assert config.polling.on_add_max_pending_tasks == 64


def test_load_config_defaults_watch_path_when_missing(tmp_path: Path) -> None:
    """Tests that load config defaults watch path when missing."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(_minimal_config(), encoding="utf-8")

    config = load_config(str(config_file))
    assert config.disk.watch_path == "/downloads"


def test_load_config_applies_flat_env_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests that load config applies flat env overrides."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[qbittorrent]
url = "http://qbittorrent:8080"
username = "admin"
password = "secret"

[disk]
watch_path = "/downloads"

[resume]
policy = "priority_fifo"
strict_fifo = true

[server]
on_add_auth_token = "from-config"
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setenv("DISKGUARD_RESUME_POLICY", "smallest_first")
    monkeypatch.setenv("DISKGUARD_RESUME_STRICT_FIFO", "false")
    monkeypatch.setenv("DISKGUARD_POLLING_INTERVAL_SECONDS", "45")
    monkeypatch.setenv("DISKGUARD_ON_ADD_QUICK_POLL_INTERVAL_SECONDS", "0.5")
    monkeypatch.setenv("DISKGUARD_ON_ADD_QUICK_POLL_MAX_ATTEMPTS", "20")
    monkeypatch.setenv("DISKGUARD_ON_ADD_QUICK_POLL_MAX_CONCURRENCY", "12")
    monkeypatch.setenv("DISKGUARD_ON_ADD_MAX_PENDING_TASKS", "24")
    monkeypatch.setenv("DISKGUARD_DISK_DOWNLOADING_STATES", "downloading,metaDL")
    monkeypatch.setenv("DISKGUARD_ON_ADD_AUTH_TOKEN", "from-env")
    monkeypatch.setenv("DISKGUARD_SERVER_ON_ADD_MAX_BODY_BYTES", "4096")

    config = load_config(str(config_file))
    assert config.resume.policy.value == "smallest_first"
    assert config.resume.strict_fifo is False
    assert config.polling.interval_seconds == 45
    assert config.polling.on_add_quick_poll_interval_seconds == 0.5
    assert config.polling.on_add_quick_poll_max_attempts == 20
    assert config.polling.on_add_quick_poll_max_concurrency == 12
    assert config.polling.on_add_max_pending_tasks == 24
    assert config.disk.downloading_states == ("downloading", "metaDL")
    assert config.server.on_add_auth_token == "from-env"
    assert config.server.on_add_max_body_bytes == 4096


def test_load_config_rejects_invalid_thresholds(tmp_path: Path) -> None:
    """Tests that load config rejects invalid thresholds."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[qbittorrent]
url = "http://qbittorrent:8080"
username = "admin"
password = "secret"

[disk]
watch_path = "/downloads"
soft_pause_below_pct = 5
hard_pause_below_pct = 5

[server]
on_add_auth_token = "token"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError):
        load_config(str(config_file))


def test_load_config_bootstraps_default_template_when_missing(tmp_path: Path) -> None:
    """Tests that load config writes default template and fails on empty required secrets."""
    config_file = tmp_path / "config" / "config.toml"

    with pytest.raises(ConfigError, match="qbittorrent.password cannot be empty"):
        load_config(str(config_file))

    assert config_file.exists()
    generated = config_file.read_text(encoding="utf-8")
    assert "[qbittorrent]" in generated
    assert 'url = "http://qbittorrent:8080"' in generated
    assert 'password = ""' in generated
    assert "[server]" in generated
    assert 'on_add_auth_token = ""' in generated


def test_load_config_does_not_overwrite_existing_file(tmp_path: Path) -> None:
    """Tests that load config does not overwrite existing file."""
    config_file = tmp_path / "config.toml"
    original = """
[qbittorrent]
url = "http://existing:8080"
username = "alice"
password = "secret"

[server]
on_add_auth_token = "token"
""".strip()
    config_file.write_text(original, encoding="utf-8")

    config = load_config(str(config_file))

    assert config_file.read_text(encoding="utf-8") == original
    assert config.qbittorrent.url == "http://existing:8080"
    assert config.qbittorrent.username == "alice"


def test_load_config_fails_when_config_directory_not_writable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tests that load config fails when config directory not writable."""
    config_file = tmp_path / "config" / "config.toml"
    monkeypatch.setattr(config_module, "_is_directory_writable", lambda _: False)

    with pytest.raises(ConfigError, match="/config is not writable"):
        load_config(str(config_file))


def test_load_config_warns_when_config_root_is_not_mount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tests that load config warns when config root is not mount."""
    config_root = tmp_path / "config-root"
    monkeypatch.setattr(config_module, "CONFIG_ROOT_PATH", config_root)
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", str(config_root / "config.toml"))
    monkeypatch.setattr(config_module, "_is_mount_point", lambda _: False)
    monkeypatch.delenv(config_module.ENV_CONFIG_PATH, raising=False)
    monkeypatch.delenv(config_module.LEGACY_ENV_CONFIG_PATH, raising=False)
    monkeypatch.setenv("DISKGUARD_QBITTORRENT_PASSWORD", "secret")
    monkeypatch.setenv("DISKGUARD_ON_ADD_AUTH_TOKEN", "token")
    caplog.set_level(logging.WARNING)

    load_config()

    assert any(
        record.levelno == logging.WARNING
        and "/config is not backed by a Docker volume" in record.getMessage()
        for record in caplog.records
    )


def test_load_config_rejects_env_path_outside_config_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tests that load config rejects env path outside config root."""
    monkeypatch.setenv(config_module.ENV_CONFIG_PATH, str(tmp_path / "config.toml"))
    monkeypatch.delenv(config_module.LEGACY_ENV_CONFIG_PATH, raising=False)

    with pytest.raises(ConfigError, match="DISKGUARD_CONFIG must point to a config file inside /config"):
        load_config()


def test_load_config_rejects_missing_on_add_auth_token(tmp_path: Path) -> None:
    """Tests that server.on_add_auth_token is required."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[qbittorrent]
url = "http://qbittorrent:8080"
username = "admin"
password = "secret"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="Missing required config key: server.on_add_auth_token"):
        load_config(str(config_file))


def test_load_config_rejects_empty_qb_password(tmp_path: Path) -> None:
    """Tests that empty qBittorrent password is rejected."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(_minimal_config(password=""), encoding="utf-8")

    with pytest.raises(ConfigError, match="qbittorrent.password cannot be empty"):
        load_config(str(config_file))


def test_load_config_rejects_empty_on_add_token(tmp_path: Path) -> None:
    """Tests that empty on-add auth token is rejected."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(_minimal_config(token=""), encoding="utf-8")

    with pytest.raises(ConfigError, match="server.on_add_auth_token cannot be empty"):
        load_config(str(config_file))


def test_load_config_allows_change_me_as_non_empty_secret_values(tmp_path: Path) -> None:
    """Tests that non-empty secret values are accepted even when equal to CHANGE_ME."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(_minimal_config(password="CHANGE_ME", token="CHANGE_ME"), encoding="utf-8")

    config = load_config(str(config_file))
    assert config.qbittorrent.password == "CHANGE_ME"
    assert config.server.on_add_auth_token == "CHANGE_ME"
