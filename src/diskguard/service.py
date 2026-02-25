"""Service composition and lifecycle management."""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from diskguard.api import OnAddHandler, create_http_app
from diskguard.config import AppConfig
from diskguard.disk_probe import DiskProbe
from diskguard.engine import ModeEngine
from diskguard.qbittorrent import QbittorrentClient
from diskguard.resume_planner import ResumePlanner
from diskguard.startup import run_qbittorrent_startup_preflight


class DiskGuardService:
    """Builds and runs HTTP + polling components in one process.

    Attributes:
        _config: Validated app configuration.
        _logger: Logger used by service and child components.
    """

    def __init__(self, config: AppConfig, *, logger: logging.Logger | None = None) -> None:
        """Initializes service dependencies and lifecycle state.

        Args:
            config: Validated application configuration.
            logger: Optional logger to reuse across components.
        """
        self._config = config
        self._logger = logger or logging.getLogger(__name__)

        self._qb_client: QbittorrentClient | None = None
        self._on_add_handler: OnAddHandler | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        """Starts HTTP listener and polling loop."""
        self._qb_client = QbittorrentClient(self._config.qbittorrent, logger=self._logger)
        try:
            await run_qbittorrent_startup_preflight(
                self._qb_client,
                qb_url=self._config.qbittorrent.url,
                logger=self._logger,
            )
        except Exception:  # noqa: BLE001
            await self._qb_client.close()
            self._qb_client = None
            raise

        disk_probe = DiskProbe(self._config.disk.watch_path)
        resume_planner = ResumePlanner(self._config, self._qb_client, logger=self._logger)
        mode_engine = ModeEngine(
            self._config,
            qb_client=self._qb_client,
            disk_probe=disk_probe,
            resume_planner=resume_planner,
            logger=self._logger,
        )

        self._on_add_handler = OnAddHandler(
            self._config,
            qb_client=self._qb_client,
            disk_probe=disk_probe,
            logger=self._logger,
        )
        http_app = create_http_app(self._on_add_handler)

        self._runner = web.AppRunner(http_app, access_log=None)
        await self._runner.setup()

        self._site = web.TCPSite(
            self._runner,
            host=self._config.server.host,
            port=self._config.server.port,
        )
        await self._site.start()
        # Polling runs in a background task while aiohttp serves `/on-add`.
        self._poll_task = asyncio.create_task(mode_engine.run_forever(self._stop_event))
        self._logger.info(
            "DiskGuard started on %s:%d",
            self._config.server.host,
            self._config.server.port,
        )

    async def stop(self) -> None:
        """Stops polling task, HTTP listener, and qB client session."""
        self._stop_event.set()

        if self._poll_task:
            self._poll_task.cancel()
            await asyncio.gather(self._poll_task, return_exceptions=True)
            self._poll_task = None

        if self._runner:
            await self._runner.cleanup()
            self._runner = None

        if self._on_add_handler:
            await self._on_add_handler.shutdown()
            self._on_add_handler = None

        if self._qb_client:
            await self._qb_client.close()
            self._qb_client = None

        self._logger.info("DiskGuard stopped")
