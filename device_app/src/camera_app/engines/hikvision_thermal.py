import logging
from typing import TYPE_CHECKING

import aiohttp
from pydoover.docker.device_agent.models import File

from .base import CameraBase
from ..app_config import Mode
from ..clients import HikvisionClient

if TYPE_CHECKING:
    from ..app_config import CameraConfig


log = logging.getLogger(__name__)


class HikVisionThermal(CameraBase):
    def __init__(self, config: "CameraConfig"):
        super().__init__(config)
        self.client: HikvisionClient = None

    async def setup(self):
        self.client = HikvisionClient(
            self.config.connection.username.value,
            self.config.connection.password.value,
            self.config.connection.address.value,
            self.config.connection.control_port.value,
            self.config.connection.rtsp_port.value,
            aiohttp.ClientSession(),
        )
        try:
            status = await self.client.get_status()
        except TimeoutError:
            log.exception("Failed to get camera status")
            return False
        else:
            if not status:
                log.info("Camera is offline, failed to get status.")
                return False

        return True

    async def get_still_snapshot(self, channel: int) -> File:
        """Use the ISAPI snapshot endpoint instead of ffmpeg."""
        snap = await self.client.get_snapshot(channel)
        return File(
            filename="snapshot.jpg",
            data=snap,
            size=len(snap),
            content_type="image/jpeg",
        )

    async def get_snapshot(self) -> list[File]:
        if Mode(self.config.snapshot.mode.value) is Mode.video:
            files = [await self.get_video_snapshot(self.config.rtsp_uri)]
            if self.config.thermal_rtsp_uri:
                files.append(await self.get_video_snapshot(self.config.thermal_rtsp_uri))
        else:
            files = [await self.get_still_snapshot(1)]
            if self.config.thermal_rtsp_uri:
                files.append(await self.get_still_snapshot(2))

        log.info(f"Sending {len(files)} snapshots...")
        return files

    async def ping(self, timeout: int):
        """Use the ISAPI status endpoint to check if the camera is reachable."""
        import asyncio
        from datetime import datetime, timedelta

        start = datetime.now()

        while datetime.now() - start < timedelta(seconds=timeout):
            try:
                status = await self.client.get_status()
            except OSError:
                pass
            else:
                if status is True:
                    log.info(f"Status call succeeded, result: {status}")
                    return True

            log.info("Failed to ping camera. Waiting 0.5sec...")
            await asyncio.sleep(0.5)

        log.info("Failed to ping camera in time, quitting...")
        return False
