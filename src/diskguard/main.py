"""Application entrypoint."""

from __future__ import annotations

import asyncio
import logging
import signal

from diskguard.config import load_config
from diskguard.config import AppConfig
from diskguard.errors import ConfigError
from diskguard.errors import StartupPreflightError
from diskguard.service import DiskGuardService

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def main() -> None:
    """Runs DiskGuard as a CLI program.

    Exits with code 2 when configuration is invalid.
    """
    _configure_logging("INFO")

    try:
        config = load_config()
    except ConfigError as exc:
        logging.getLogger(__name__).error("Configuration error: %s", exc)
        raise SystemExit(2) from exc

    _configure_logging(config.logging.level)

    try:
        asyncio.run(_run_service(config))
    except StartupPreflightError:
        raise SystemExit(1)
    except KeyboardInterrupt:
        pass


async def _run_service(config: AppConfig) -> None:
    """Runs DiskGuard service until a termination signal is received.

    Args:
        config: Validated application configuration.
    """
    logger = logging.getLogger("diskguard")
    service = DiskGuardService(config, logger=logger)
    await service.start()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_stop() -> None:
        stop_event.set()

    # Use event-loop signal handlers when supported by the platform.
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            continue

    try:
        await stop_event.wait()
    finally:
        await service.stop()


def _configure_logging(level: str) -> None:
    """Configures root logging for DiskGuard.

    Args:
        level: Uppercase level name (for example, "INFO" or "DEBUG").
    """
    logging.basicConfig(level=getattr(logging, level, logging.INFO), format=LOG_FORMAT, force=True)
