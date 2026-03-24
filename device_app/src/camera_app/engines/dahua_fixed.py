import asyncio
import logging

from pydoover import rpc

from .dahua_base import DahuaCameraBase
from ..events import GenericCameraControlEvent, CAMERA_CONTROL_CHANNEL

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

    @rpc.handler("reset", parser=GenericCameraControlEvent.from_dict, channel=CAMERA_CONTROL_CHANNEL)
    async def reset(self, ctx, payload: GenericCameraControlEvent):
        await self.client.adjust_manual_zoom(zoom=-1, focus=-1)
        await self.check_for_zoom_complete()

    @rpc.handler("zoom", parser=int, channel=CAMERA_CONTROL_CHANNEL)
    async def zoom(self, ctx, payload: int):
        zoom = payload
        log.info(f"Executing control command for camera: {payload}")
        if 1 < zoom < 100:
            zoom = zoom / 100
        else:
            zoom = max(min(zoom, 1), 0)
        zoom = round(zoom, 1)
        log.info(f"adjusting zoom: {zoom}")

        await self.client.adjust_manual_zoom(zoom)
        await asyncio.sleep(1)

        await self.check_for_zoom_complete()
        # await self.client.auto_focus()

