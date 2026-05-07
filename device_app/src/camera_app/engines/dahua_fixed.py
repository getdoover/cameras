import asyncio
import logging

from pydoover import rpc

from .dahua_base import DahuaCameraBase
from ..events import CAMERA_CONTROL_CHANNEL

log = logging.getLogger(__name__)

class DahuaFixedCamera(DahuaCameraBase):
    async def check_for_zoom_complete(self):
        retries = 0
        status = None
        while status != "Normal" and retries < 30:
            data = await self.client.get_focus_status()
            status = data.get("status.Status")
            retries += 1
            await asyncio.sleep(0.1)

    @rpc.handler("reset", channel=CAMERA_CONTROL_CHANNEL)
    async def reset(self, ctx, payload):
        await self.client.adjust_manual_zoom(zoom=-1, focus=-1)
        await self.check_for_zoom_complete()

    # @rpc.handler("zoom", parser=float, channel=CAMERA_CONTROL_CHANNEL)
    # async def zoom(self, ctx, payload: float):
    #     zoom = payload
    #     log.info(f"Executing control command for camera: {payload}")
    #     if 1 < zoom < 100:
    #         zoom = zoom / 100
    #     else:
    #         zoom = max(min(zoom, 1), 0)
    #     zoom = round(zoom, 1)
    #     log.info(f"adjusting zoom: {zoom}")
    #
    #     await self.client.adjust_manual_zoom(zoom)
    #     await asyncio.sleep(1)
    #
    #     await self.check_for_zoom_complete()
    #     # await self.client.auto_focus()

    @rpc.handler("zoom_continuous", parser=float, channel=CAMERA_CONTROL_CHANNEL)
    async def zoom_continuous(self, ctx, payload: float):
        log.info(f"Executing continuous zoom command: {payload}")
        if payload == 0:
            return

        status = await self.client.get_focus_status()
        try:
            current = float(status["status.Zoom"])
        except (KeyError, ValueError, TypeError):
            log.warning(f"Could not read current zoom from {status}, defaulting to 0.5")
            current = 0.5

        step = 0.1 if payload > 0 else -0.1
        new_zoom = round(max(0.0, min(1.0, current + step)), 1)
        log.info(f"continuous zoom: {current} -> {new_zoom}")

        await self.client.adjust_manual_zoom(new_zoom)
        await self.check_for_zoom_complete()

