import asyncio
import logging
from datetime import datetime, timezone

from .dahua_base import DahuaCameraBase

log = logging.getLogger(__name__)

class DahuaPTZCamera(DahuaCameraBase):
    HORIZONTAL_RANGE = (0, 360)
    VERTICAL_RANGE = (-15, 90)
    ZOOM_RANGE = (100, 2500)

    def __init__(self, *args, **kwargs):
        self.last_position = None
        self.last_absolute_control = True
        super().__init__(*args, **kwargs)

    @staticmethod
    def normalise(value, actual_range, desired_range):
        prop = desired_range[0] + \
                (value - actual_range[0]) * (desired_range[1] - desired_range[0]) / (actual_range[1] - actual_range[0])
        return max(min(prop, desired_range[1]), desired_range[0])

    def validate_value(self, value, min_val, max_val, new_min, new_max):
        value = max(min(value, max_val), min_val)
        if new_min <= value <= new_max:
            return value

        return self.normalise(value, (min_val, max_val), (new_min, new_max))

    def normalise_position(self, x, y, zoom):
        if 0 <= x < 180:
            x = x / 180
        elif 180 < x <= 360:
            x = (x - 360) / 180

        if -180 <= y <= 180:
            y = y / -180

        return x, y, self.normalise(zoom, self.ZOOM_RANGE, (0, 1))
        # return self.normalise(x, self.HORIZONTAL_RANGE, (0, 1)), \
        #        self.normalise(y, self.VERTICAL_RANGE, (0, 1)), \
        #        self.normalise(zoom, self.ZOOM_RANGE, (-1, 1))

    async def fetch_presets(self) -> list[str]:
        presets = await self.client.get_presets(fetch=True)
        return list(presets.keys())

    async def set_absolute_control_disabled(self):
        if self.last_absolute_control is False:
            return

        self.last_absolute_control = False

    async def get_position(self, fetch: bool = False):
        if fetch is False and self.last_position is not None:
            return self.last_position

        pos = await self.client.get_ptz_position()
        normalised = self.normalise_position(*pos)
        self.last_position = normalised
        return normalised

    async def check_for_move_complete(self):
        retries = 0
        status = None
        while status != "Idle" and retries < 30:
            data = await self.client.get_ptz_status()
            status = data.get("status.MoveStatus")
            retries += 1
            await asyncio.sleep(0.1)

    @staticmethod
    def snowflake_to_datetime(snowflake_id):
        timestamp = ((int(snowflake_id) >> 22) + 1735689600000) / 1000.0
        dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        now = datetime.now(tz=timezone.utc)
        log.info(f"DT: {(now - dt).total_seconds()}sec")

    async def on_control_message(self, message_id, data):
        # check for power on message
        await super().on_control_message(message_id, data)

        if not self.check_control_message(message_id, data):
            return

        log.info(f"Executing control command for camera: {data}")

        action = data.get("action", "")
        amount = data.get("value")
        if amount is None and action != "stop":
            return

        self.snowflake_to_datetime(message_id)

        if action == "stop":
            await self.client.stop_ptz()
            await self.check_for_move_complete()
        elif action == "zoom":
            x, y, z = await self.get_position(fetch=True)
            z = self.normalise(amount, (0, 100), (0, 1))
            await self.client.absolute_ptz(x, y, z)
            await self.check_for_move_complete()
            await self.clear_active_preset_func()
        elif action == "pantilt_continuous":
            pan, tilt = amount.get("pan"), amount.get("tilt")
            pan = self.normalise(pan, (-1, 1), (-10, 10))
            tilt = self.normalise(tilt, (-1, 1), (-10, 10))
            # pan = self.validate_value(pan, -100, 100, -10, 10)
            # tilt = self.validate_value(tilt, -100, 100, -10, 10)
            log.info(f"pan-tilting: {pan}, {tilt}")
            await self.client.continuous_ptz(pan, tilt, 0, timeout=0.5)
            log.info(f"done pan-tilt-movement")
            await self.clear_active_preset_func()
            # await self.set_absolute_control_disabled()
        elif action == "zoom_continuous":
            amount = self.validate_value(amount, -100, 100, -1, 1)
            # zoom amounts don't matter... it's just the + or - that matters (in vs out)
            await self.client.continuous_zoom(amount)
            await self.clear_active_preset_func()

        elif action == "pantilt_absolute":
            pan, tilt = amount.get("pan"), amount.get("tilt")
            log.info(f"pan-tilting absolute: {pan}, {tilt}")
            curr_pos = await self.get_position()
            await self.client.absolute_ptz(pan, tilt, curr_pos[2])
            await self.check_for_move_complete()
            await self.clear_active_preset_func()
        elif "incremental" in action:
            amount = self.validate_value(amount, -100, 100, -1, 1)
            log.info(f"incremental moving: {action}, {amount}")

            if action == "incremental_pan":
                await self.client.relative_ptz(amount, 0, 0)
            elif action == "incremental_tilt":
                await self.client.relative_ptz(0, amount, 0)
            elif action == "incremental_zoom":
                await self.client.relative_ptz(0, 0, amount)

            await self.clear_active_preset_func()
        elif action == "goto_preset":
            log.info(f"moving to preset {amount}")
            await self.client.goto_preset(amount)
            # await self.set_absolute_control_disabled()
            await self.sync_presets_func(amount)
            await self.check_for_move_complete()
        elif action == "create_preset":
            log.info(f"creating preset {amount}")
            await self.client.create_preset(amount)
            await self.sync_presets_func(amount)
        elif action == "delete_preset":
            log.info(f"deleting preset {amount}")
            await self.client.delete_preset(amount)
            await self.sync_presets_func()
