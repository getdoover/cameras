import asyncio
import logging
import re

from pydoover import rpc
from pydoover.models import File

from .bosch_base import BoschCameraBase
from ..app_config import Mode
from ..events import PTZControlEvent, CAMERA_CONTROL_CHANNEL

log = logging.getLogger(__name__)


class BoschPTZCamera(BoschCameraBase):
    # ONVIF normalized ranges (native to the protocol)
    PAN_RANGE = (-1.0, 1.0)
    TILT_RANGE = (-1.0, 1.0)
    ZOOM_RANGE = (0.0, 1.0)

    def __init__(self, *args, **kwargs):
        self.last_position = None
        super().__init__(*args, **kwargs)

    @staticmethod
    def normalise(value, actual_range, desired_range):
        prop = desired_range[0] + (value - actual_range[0]) * (
            desired_range[1] - desired_range[0]
        ) / (actual_range[1] - actual_range[0])
        return max(min(prop, desired_range[1]), desired_range[0])

    def validate_value(self, value, min_val, max_val, new_min, new_max):
        value = max(min(value, max_val), min_val)
        if new_min <= value <= new_max:
            return value
        return self.normalise(value, (min_val, max_val), (new_min, new_max))

    async def fetch_presets(self) -> list[str]:
        presets = await self.client.get_presets(fetch=True)
        return list(presets.keys())

    async def get_position(self, fetch: bool = False):
        if fetch is False and self.last_position is not None:
            return self.last_position

        pos = await self.client.get_ptz_position()
        self.last_position = pos
        return pos

    async def check_for_move_complete(self):
        retries = 0
        move_status = None
        while move_status != "IDLE" and retries < 30:
            status = await self.client.get_ptz_status()
            if hasattr(status, "MoveStatus"):
                ms = status.MoveStatus
                if hasattr(ms, "PanTilt"):
                    move_status = str(ms.PanTilt).upper()
                elif hasattr(ms, "Zoom"):
                    move_status = str(ms.Zoom).upper()
                else:
                    move_status = str(ms).upper()
            retries += 1
            await asyncio.sleep(0.1)

    @rpc.handler("stop", channel=CAMERA_CONTROL_CHANNEL)
    async def on_stop(self, ctx, payload):
        await self.client.stop()
        await self.check_for_move_complete()

    @rpc.handler("zoom", parser=float, channel=CAMERA_CONTROL_CHANNEL)
    async def on_zoom(self, ctx, payload: float):
        pan, tilt, _ = await self.get_position(fetch=True)
        zoom = self.normalise(payload, (0, 100), self.ZOOM_RANGE)
        await self.client.absolute_move(pan, tilt, zoom)
        await self.check_for_move_complete()
        await self.clear_active_preset_func()

    @rpc.handler("pantilt_continuous", parser=PTZControlEvent.from_dict, channel=CAMERA_CONTROL_CHANNEL)
    async def on_pantilt_continuous(self, ctx, payload: PTZControlEvent):
        pan = self.normalise(payload.pan, (-1, 1), self.PAN_RANGE)
        tilt = self.normalise(payload.tilt, (-1, 1), self.TILT_RANGE)
        log.info(f"pan-tilting continuous: {pan}, {tilt}")
        await self.client.continuous_move(pan, tilt, 0)
        await self.clear_active_preset_func()

    @rpc.handler("pantilt_absolute", parser=PTZControlEvent.from_dict, channel=CAMERA_CONTROL_CHANNEL)
    async def on_pantilt_absolute(self, ctx, payload: PTZControlEvent):
        log.info(f"pan-tilting absolute: {payload.pan}, {payload.tilt}")
        curr_pos = await self.get_position()
        await self.client.absolute_move(payload.pan, payload.tilt, curr_pos[2])
        await self.check_for_move_complete()
        await self.clear_active_preset_func()

    @rpc.handler("zoom_continuous", parser=float, channel=CAMERA_CONTROL_CHANNEL)
    async def on_zoom_continuous(self, ctx, payload: float):
        zoom_speed = self.validate_value(payload, -100, 100, -1, 1)
        await self.client.continuous_move(0, 0, zoom_speed)
        await self.clear_active_preset_func()

    @rpc.handler("goto_preset", parser=str, channel=CAMERA_CONTROL_CHANNEL)
    async def on_goto_preset(self, ctx, payload: str):
        log.info(f"moving to preset {payload}")
        await self.client.goto_preset(payload)
        await self.sync_presets_func(payload)
        await self.check_for_move_complete()

    @rpc.handler(re.compile(r"incremental_.*"), channel=CAMERA_CONTROL_CHANNEL, parser=float)
    async def on_incremental(self, ctx, payload: float):
        amount = self.validate_value(payload, -100, 100, -1, 1)
        log.info(f"incremental moving: {ctx.method}, {amount}")

        if ctx.method == "incremental_pan":
            await self.client.relative_move(amount, 0, 0)
        elif ctx.method == "incremental_tilt":
            await self.client.relative_move(0, amount, 0)
        elif ctx.method == "incremental_zoom":
            await self.client.relative_move(0, 0, amount)

        await self.clear_active_preset_func()

    @rpc.handler("create_preset", parser=str, channel=CAMERA_CONTROL_CHANNEL)
    async def on_create_preset(self, ctx, payload: str):
        log.info(f"creating preset {payload}")
        await self.client.create_preset(payload)
        await self.sync_presets_func(payload)

    @rpc.handler("delete_preset", parser=str, channel=CAMERA_CONTROL_CHANNEL)
    async def on_delete_preset(self, ctx, payload: str):
        log.info(f"deleting preset {payload}")
        await self.client.delete_preset(payload)
        await self.sync_presets_func()

    async def get_snapshot(self) -> list[File]:
        if Mode(self.config.snapshot.mode.value) is Mode.video:
            func = self.get_video_snapshot
        else:
            func = self.get_still_snapshot

        files = []

        presets = await self.fetch_presets()
        if presets:
            for preset in presets:
                log.info(f"Taking snapshot at {preset}...")
                try:
                    await self.client.goto_preset(preset)
                    await self.check_for_move_complete()
                    file = await func(self.config.rtsp_uri)
                except Exception as e:
                    log.info(f"Failed to take snapshot: {e}")
                else:
                    files.append(file)
        else:
            try:
                file = await func(self.config.rtsp_uri)
            except Exception as e:
                log.info(f"Failed to take snapshot: {e}")
            else:
                files.append(file)

        log.info(f"Sending {len(files)} snapshots...")
        return files
