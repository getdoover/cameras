import asyncio
import logging

from .dahua_base import DahuaCameraBase

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

    async def on_control_message(self, message_id, data):
        # check for power on message
        await super().on_control_message(message_id, data)

        if not self.check_control_message(message_id, data):
            return

        action = data.get("action")
        if action == "reset":
            await self.client.adjust_manual_zoom(zoom=-1, focus=-1)
            await self.check_for_zoom_complete()

        if data.get("action") != "zoom":
            return

        log.info(f"Executing control command for camera {self.name}: {data}")
        zoom = data.get("value", 0)
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
