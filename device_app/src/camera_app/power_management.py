import asyncio
import logging

from datetime import datetime, timedelta

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .application import CameraApplication

log = logging.getLogger(__name__)


class CameraPowerManagement:
    def __init__(self, app: "CameraApplication"):
        self.app = app
        self.config = app.config.power
        self.tasks = []

        self.check_release_task = asyncio.create_task(self.check_release())

    async def acquire_for(self, dt: timedelta):
        if not self.config.enabled.value:
            log.info("Power management is disabled. Skipping...")
            return

        acquired_until_ts = self.app.get_global_tag(
            f"camera_power_{self.config.pin.value}", 0
        )
        acquired_until = datetime.fromtimestamp(acquired_until_ts)
        acquire_until = datetime.now() + dt

        # we can set this regardless in case the app got into some sort of funny state
        await self.app.platform_iface.set_do_async(self.config.pin.value, True)

        if acquired_until > acquire_until:
            log.info(
                f"Already acquired until {acquired_until} which exceeds our timeout of {self.config.timeout.value}. "
                f"Setting platform iface on to be sure and skipping..."
            )
            await self.app.platform_iface.set_do_async(self.config.pin.value, True)
            return

        log.info(f"Acquiring power for camera until {acquire_until}")
        await self.app.platform_iface.set_do_async(self.config.pin.value, True)
        await self.app.set_global_tag(f"camera_power_{self.config.pin.value}", acquire_until.timestamp())

    async def acquire(self):
        return await self.acquire_for(timedelta(seconds=self.config.timeout.value))

        # self.tasks.append(asyncio.create_task(self.release_at(acquire_until)))

    async def check_release(self):
        while True:
            try:
                await self.app.wait_until_ready()

                if not self.config.enabled.value:
                    log.info("Power management is disabled. Skipping...")
                    await asyncio.sleep(60)
                    continue

                # a few prerequisites that make this possible, and desirable.
                # for simplicity, this is never restarted, nor cancelled when a camera is started. this is because:
                # 1. Time to shut off can never go backwards (ie. if you sleep until the next one and it's cancelled it will
                #    just check again and wait for the next one)
                # 2. If there's no releases scheduled it will check every 30 seconds (less than the minimum timeout)

                acquired_until_ts = self.app.get_global_tag(f"camera_power_{self.config.pin.value}", 0)
                acquired_until = datetime.fromtimestamp(acquired_until_ts)
                if acquired_until_ts and acquired_until < datetime.now():
                    log.info(f"Power acquired until {acquired_until} has expired. Releasing power...")
                    await self.release()
                    await self.app.set_global_tag(f"camera_power_{self.config.pin.value}", 0)
                else:
                    if acquired_until_ts == 0:
                        log.info("No acquired timestamp found. Sleeping for 60sec.")
                        await asyncio.sleep(60)
                    else:
                        log.info(f"Sleeping until acquired time: {acquired_until}")
                        await asyncio.sleep((acquired_until - datetime.now()).total_seconds())

            except asyncio.CancelledError:
                log.info("Power release check cancelled")
                break
            except Exception as e:
                log.error(f"Failed to check power release: {e}")
                await asyncio.sleep(10)

    async def release(self):
        log.info("Releasing power...")
        await self.app.platform_iface.set_do(self.config.pin.value, False)
