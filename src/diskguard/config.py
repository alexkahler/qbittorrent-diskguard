"""Configuration loading and validation for DiskGuard."""

from __future__ import annotations

import copy
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from diskguard.errors import ConfigError
from diskguard.models import ResumePolicy

ENV_CONFIG_PATH = "DISKGUARD_CONFIG_PATH"
DEFAULT_CONFIG_PATH = "/config/config.toml"

DEFAULT_DOWNLOADING_STATES = (
    "downloading",
    "metaDL",
    "queuedDL",
    "stalledDL",
    "checkingDL",
    "allocating",
)

VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}


@dataclass(frozen=True)
class QbittorrentConfig:
    """qBittorrent connection settings."""

    url: str
    username: str
    password: str
    connect_timeout_seconds: float = 2.0
    read_timeout_seconds: float = 8.0
    total_timeout_seconds: float = 12.0


@dataclass(frozen=True)
class DiskConfig:
    """Disk threshold and classification settings."""

    watch_path: str
    soft_pause_below_pct: float = 10.0
    hard_pause_below_pct: float = 5.0
    resume_floor_pct: float = 10.0
    safety_buffer_gb: float = 10.0
    downloading_states: tuple[str, ...] = DEFAULT_DOWNLOADING_STATES


@dataclass(frozen=True)
class PollingConfig:
    """Polling loop settings."""

    interval_seconds: int = 30


@dataclass(frozen=True)
class ResumeConfig:
    """Resume planner settings."""

    policy: ResumePolicy = ResumePolicy.PRIORITY_FIFO
    strict_fifo: bool = True


@dataclass(frozen=True)
class TaggingConfig:
    """DiskGuard tag names."""

    paused_tag: str = "diskguard_paused"
    soft_allowed_tag: str = "soft_allowed"


@dataclass(frozen=True)
class LoggingConfig:
    """Application logging settings."""

    level: str = "INFO"


@dataclass(frozen=True)
class ServerConfig:
    """HTTP listener settings."""

    host: str = "0.0.0.0"
    port: int = 7070


@dataclass(frozen=True)
class AppConfig:
    """Root application configuration."""

    qbittorrent: QbittorrentConfig
    disk: DiskConfig
    polling: PollingConfig
    resume: ResumeConfig
    tagging: TaggingConfig
    logging: LoggingConfig
    server: ServerConfig


EnvParser = Callable[[str], Any]


def _parse_int(raw: str) -> int:
    """Parses a string as an integer.

    Args:
        raw: Raw environment variable string.

    Returns:
        Parsed integer value.

    Raises:
        ConfigError: If the value cannot be parsed as an integer.
    """
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"Invalid int value: {raw!r}") from exc


def _parse_float(raw: str) -> float:
    """Parses a string as a float.

    Args:
        raw: Raw environment variable string.

    Returns:
        Parsed float value.

    Raises:
        ConfigError: If the value cannot be parsed as a float.
    """
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"Invalid float value: {raw!r}") from exc


def _parse_bool(raw: str) -> bool:
    """Parses a permissive boolean string.

    Args:
        raw: Raw environment variable string.

    Returns:
        Parsed boolean value.

    Raises:
        ConfigError: If the value is not a supported boolean token.
    """
    lowered = raw.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"Invalid bool value: {raw!r}")


def _parse_csv(raw: str) -> list[str]:
    """Parses a comma-separated string into non-empty trimmed items.

    Args:
        raw: Raw CSV string.

    Returns:
        List of trimmed values.

    Raises:
        ConfigError: If parsing produces an empty list.
    """
    values = [value.strip() for value in raw.split(",") if value.strip()]
    if not values:
        raise ConfigError("CSV override produced an empty list")
    return values


ENV_OVERRIDES: dict[str, tuple[str, str, EnvParser]] = {
    "DISKGUARD_QBITTORRENT_URL": ("qbittorrent", "url", str),
    "DISKGUARD_QBITTORRENT_USERNAME": ("qbittorrent", "username", str),
    "DISKGUARD_QBITTORRENT_PASSWORD": ("qbittorrent", "password", str),
    "DISKGUARD_QBITTORRENT_CONNECT_TIMEOUT_SECONDS": (
        "qbittorrent",
        "connect_timeout_seconds",
        _parse_float,
    ),
    "DISKGUARD_QBITTORRENT_READ_TIMEOUT_SECONDS": (
        "qbittorrent",
        "read_timeout_seconds",
        _parse_float,
    ),
    "DISKGUARD_QBITTORRENT_TOTAL_TIMEOUT_SECONDS": (
        "qbittorrent",
        "total_timeout_seconds",
        _parse_float,
    ),
    "DISKGUARD_DISK_WATCH_PATH": ("disk", "watch_path", str),
    "DISKGUARD_DISK_SOFT_PAUSE_BELOW_PCT": ("disk", "soft_pause_below_pct", _parse_float),
    "DISKGUARD_DISK_HARD_PAUSE_BELOW_PCT": ("disk", "hard_pause_below_pct", _parse_float),
    "DISKGUARD_DISK_RESUME_FLOOR_PCT": ("disk", "resume_floor_pct", _parse_float),
    "DISKGUARD_DISK_SAFETY_BUFFER_GB": ("disk", "safety_buffer_gb", _parse_float),
    "DISKGUARD_DISK_DOWNLOADING_STATES": ("disk", "downloading_states", _parse_csv),
    "DISKGUARD_POLLING_INTERVAL_SECONDS": ("polling", "interval_seconds", _parse_int),
    "DISKGUARD_RESUME_POLICY": ("resume", "policy", str),
    "DISKGUARD_RESUME_STRICT_FIFO": ("resume", "strict_fifo", _parse_bool),
    "DISKGUARD_TAGGING_PAUSED_TAG": ("tagging", "paused_tag", str),
    "DISKGUARD_TAGGING_SOFT_ALLOWED_TAG": ("tagging", "soft_allowed_tag", str),
    "DISKGUARD_LOGGING_LEVEL": ("logging", "level", str),
    "DISKGUARD_SERVER_HOST": ("server", "host", str),
    "DISKGUARD_SERVER_PORT": ("server", "port", _parse_int),
}


def load_config(config_path: str | None = None) -> AppConfig:
    """Loads and validates DiskGuard configuration.

    Args:
        config_path: Optional explicit TOML path. If omitted, environment and
            defaults are used.

    Returns:
        Fully validated application configuration.

    Raises:
        ConfigError: If file loading, parsing, overrides, or validation fails.
    """
    resolved_path = config_path or os.getenv(ENV_CONFIG_PATH, DEFAULT_CONFIG_PATH)
    raw_config = _read_toml(resolved_path)
    merged_config = copy.deepcopy(raw_config)
    _apply_env_overrides(merged_config)
    return _build_config(merged_config)


def _read_toml(config_path: str) -> dict[str, Any]:
    """Reads and parses a TOML config file.

    Args:
        config_path: Path to TOML config file.

    Returns:
        Parsed root document as a dictionary.

    Raises:
        ConfigError: If the file is missing, invalid TOML, or invalid shape.
    """
    path = Path(config_path)
    if not path.exists():
        raise ConfigError(f"Config file not found: {config_path}")
    try:
        with path.open("rb") as handle:
            loaded = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML at {config_path}: {exc}") from exc

    if not isinstance(loaded, dict):
        raise ConfigError("Root TOML document must be a table")
    return loaded


def _apply_env_overrides(config: dict[str, Any]) -> None:
    """Applies flat environment overrides into a nested config dictionary.

    Args:
        config: Mutable config mapping loaded from TOML.

    Raises:
        ConfigError: If an expected section exists but is not a dictionary.
    """
    for env_name, (section_name, key_name, parser) in ENV_OVERRIDES.items():
        raw_value = os.getenv(env_name)
        if raw_value is None:
            continue
        section = config.setdefault(section_name, {})
        if not isinstance(section, dict):
            raise ConfigError(f"Config section {section_name!r} must be a table")
        section[key_name] = parser(raw_value)


def _build_config(raw: dict[str, Any]) -> AppConfig:
    """Builds typed config objects from a raw config dictionary.

    Args:
        raw: Raw config mapping after environment overrides.

    Returns:
        Fully constructed and validated ``AppConfig``.

    Raises:
        ConfigError: If any field is missing or has an invalid value.
    """
    qb_section = _as_section(raw, "qbittorrent")
    disk_section = _as_section(raw, "disk")
    polling_section = _as_section(raw, "polling", optional=True)
    resume_section = _as_section(raw, "resume", optional=True)
    tagging_section = _as_section(raw, "tagging", optional=True)
    logging_section = _as_section(raw, "logging", optional=True)
    server_section = _as_section(raw, "server", optional=True)

    qb_config = QbittorrentConfig(
        url=_require_non_empty_string(qb_section, "url", "qbittorrent.url"),
        username=_require_non_empty_string(qb_section, "username", "qbittorrent.username"),
        password=_require_non_empty_string(qb_section, "password", "qbittorrent.password"),
        connect_timeout_seconds=_as_float(
            qb_section.get("connect_timeout_seconds", 2.0),
            "qbittorrent.connect_timeout_seconds",
        ),
        read_timeout_seconds=_as_float(
            qb_section.get("read_timeout_seconds", 8.0),
            "qbittorrent.read_timeout_seconds",
        ),
        total_timeout_seconds=_as_float(
            qb_section.get("total_timeout_seconds", 12.0),
            "qbittorrent.total_timeout_seconds",
        ),
    )

    downloading_states_raw = disk_section.get("downloading_states", DEFAULT_DOWNLOADING_STATES)
    downloading_states = _coerce_states(downloading_states_raw)
    watch_path = str(disk_section.get("watch_path", "/downloads")).strip()
    if not watch_path:
        raise ConfigError("disk.watch_path cannot be empty")

    disk_config = DiskConfig(
        watch_path=watch_path,
        soft_pause_below_pct=_as_float(
            disk_section.get("soft_pause_below_pct", 10.0), "disk.soft_pause_below_pct"
        ),
        hard_pause_below_pct=_as_float(
            disk_section.get("hard_pause_below_pct", 5.0), "disk.hard_pause_below_pct"
        ),
        resume_floor_pct=_as_float(
            disk_section.get("resume_floor_pct", 10.0), "disk.resume_floor_pct"
        ),
        safety_buffer_gb=_as_float(disk_section.get("safety_buffer_gb", 10.0), "disk.safety_buffer_gb"),
        downloading_states=downloading_states,
    )

    policy_raw = str(resume_section.get("policy", ResumePolicy.PRIORITY_FIFO.value))
    try:
        resume_policy = ResumePolicy(policy_raw)
    except ValueError as exc:
        supported = ", ".join(policy.value for policy in ResumePolicy)
        raise ConfigError(f"Invalid resume.policy={policy_raw!r}. Supported: {supported}") from exc

    resume_config = ResumeConfig(
        policy=resume_policy,
        strict_fifo=_as_bool(resume_section.get("strict_fifo", True), "resume.strict_fifo"),
    )

    polling_config = PollingConfig(
        interval_seconds=_as_int(polling_section.get("interval_seconds", 30), "polling.interval_seconds")
    )

    tagging_config = TaggingConfig(
        paused_tag=str(tagging_section.get("paused_tag", "diskguard_paused")).strip(),
        soft_allowed_tag=str(tagging_section.get("soft_allowed_tag", "soft_allowed")).strip(),
    )

    log_level = str(logging_section.get("level", "INFO")).strip().upper()
    logging_config = LoggingConfig(level=log_level)

    server_config = ServerConfig(
        host=str(server_section.get("host", "0.0.0.0")).strip(),
        port=_as_int(server_section.get("port", 7070), "server.port"),
    )

    app_config = AppConfig(
        qbittorrent=qb_config,
        disk=disk_config,
        polling=polling_config,
        resume=resume_config,
        tagging=tagging_config,
        logging=logging_config,
        server=server_config,
    )
    _validate(app_config)
    return app_config


def _as_section(root: dict[str, Any], name: str, optional: bool = False) -> dict[str, Any]:
    """Returns a TOML section as a dictionary.

    Args:
        root: Root config mapping.
        name: Section name.
        optional: Unused compatibility flag retained for call-site stability.

    Returns:
        Section dictionary, or an empty dict when section is missing.

    Raises:
        ConfigError: If the section exists but is not a dictionary.
    """
    value = root.get(name)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"Section {name!r} must be a TOML table")
    return value


def _require_non_empty_string(section: dict[str, Any], key: str, full_name: str) -> str:
    """Reads a required non-empty string from a section.

    Args:
        section: Config subsection mapping.
        key: Key name within the section.
        full_name: Human-readable fully qualified key name for errors.

    Returns:
        Trimmed non-empty string value.

    Raises:
        ConfigError: If key is missing or resolves to an empty value.
    """
    if key not in section:
        raise ConfigError(f"Missing required config key: {full_name}")
    value = str(section[key]).strip()
    if not value:
        raise ConfigError(f"Config key {full_name} cannot be empty")
    return value


def _as_float(value: Any, full_name: str) -> float:
    """Coerces a config value to float.

    Args:
        value: Raw config value.
        full_name: Human-readable key name for errors.

    Returns:
        Parsed float value.

    Raises:
        ConfigError: If value cannot be represented as float.
    """
    if isinstance(value, bool):
        raise ConfigError(f"Config key {full_name} must be a float, not bool")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Config key {full_name} must be a float") from exc


def _as_int(value: Any, full_name: str) -> int:
    """Coerces a config value to int.

    Args:
        value: Raw config value.
        full_name: Human-readable key name for errors.

    Returns:
        Parsed integer value.

    Raises:
        ConfigError: If value cannot be represented as int.
    """
    if isinstance(value, bool):
        raise ConfigError(f"Config key {full_name} must be an int, not bool")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Config key {full_name} must be an int") from exc
    return parsed


def _as_bool(value: Any, full_name: str) -> bool:
    """Coerces a config value to bool.

    Args:
        value: Raw config value.
        full_name: Human-readable key name for errors.

    Returns:
        Parsed boolean value.

    Raises:
        ConfigError: If value is not bool or parseable string.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _parse_bool(value)
    raise ConfigError(f"Config key {full_name} must be a bool")


def _coerce_states(value: Any) -> tuple[str, ...]:
    """Coerces and de-duplicates configured downloading states.

    Args:
        value: Raw states value, list/tuple or CSV string.

    Returns:
        Ordered tuple of unique state names.

    Raises:
        ConfigError: If value type is unsupported or empty after parsing.
    """
    if isinstance(value, str):
        states = _parse_csv(value)
    elif isinstance(value, (list, tuple)):
        states = [str(item).strip() for item in value if str(item).strip()]
    else:
        raise ConfigError("disk.downloading_states must be a list or comma-separated string")

    unique = tuple(dict.fromkeys(states))
    if not unique:
        raise ConfigError("disk.downloading_states cannot be empty")
    return unique


def _validate(config: AppConfig) -> None:
    """Validates cross-field config invariants.

    Args:
        config: Typed application configuration.

    Raises:
        ConfigError: If any constraint is violated.
    """
    if not (0 <= config.disk.hard_pause_below_pct <= 100):
        raise ConfigError("disk.hard_pause_below_pct must be between 0 and 100")
    if not (0 <= config.disk.soft_pause_below_pct <= 100):
        raise ConfigError("disk.soft_pause_below_pct must be between 0 and 100")
    if config.disk.hard_pause_below_pct >= config.disk.soft_pause_below_pct:
        raise ConfigError("disk.hard_pause_below_pct must be strictly lower than disk.soft_pause_below_pct")
    if not (0 <= config.disk.resume_floor_pct <= 100):
        raise ConfigError("disk.resume_floor_pct must be between 0 and 100")
    if config.disk.safety_buffer_gb < 0:
        raise ConfigError("disk.safety_buffer_gb must be non-negative")
    if config.polling.interval_seconds <= 0:
        raise ConfigError("polling.interval_seconds must be greater than zero")
    if config.qbittorrent.connect_timeout_seconds <= 0:
        raise ConfigError("qbittorrent.connect_timeout_seconds must be greater than zero")
    if config.qbittorrent.read_timeout_seconds <= 0:
        raise ConfigError("qbittorrent.read_timeout_seconds must be greater than zero")
    if config.qbittorrent.total_timeout_seconds <= 0:
        raise ConfigError("qbittorrent.total_timeout_seconds must be greater than zero")
    if config.server.port <= 0 or config.server.port > 65535:
        raise ConfigError("server.port must be between 1 and 65535")
    if not config.tagging.paused_tag:
        raise ConfigError("tagging.paused_tag cannot be empty")
    if not config.tagging.soft_allowed_tag:
        raise ConfigError("tagging.soft_allowed_tag cannot be empty")
    if config.tagging.paused_tag == config.tagging.soft_allowed_tag:
        raise ConfigError("tagging.paused_tag and tagging.soft_allowed_tag must be distinct")
    if config.logging.level not in VALID_LOG_LEVELS:
        supported = ", ".join(sorted(VALID_LOG_LEVELS))
        raise ConfigError(f"logging.level must be one of: {supported}")
