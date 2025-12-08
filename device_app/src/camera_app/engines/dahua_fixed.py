import asyncio
import logging

from .dahua_base import DahuaCameraBase

log = logging.getLogger(__name__)

class DahuaFixedCamera(DahuaCameraBase):

    async def get_ui_payload(self, force_allow_absolute: bool = False):
        status = await self.client.get_focus_status()
        return {
            "cam_position": {"zoom": float(status["status.Zoom"])*100},
            "allow_absolute_position": force_allow_absolute or status["status.Status"] == "Normal"
        }

    async def check_for_zoom_complete(self):
        retries = 0
        status = None
        while status != "Normal" and retries < 30:
            data = await self.client.get_focus_status()
            status = data.get("status.Status")
            retries += 1
            await asyncio.sleep(0.1)

        await self.sync_ui()

    async def on_control_message(self, data):
        # check for power on message
        await super().on_control_message(data)

        if not self.check_control_message(data):
            return

        action = data.get("action")
        if action == "sync_ui":
            await self.sync_ui()
        elif action == "reset":
            await self.client.adjust_manual_zoom(zoom=-1, focus=-1)
            await self.check_for_zoom_complete()
            await self.sync_ui(force_allow_absolute=True)

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
