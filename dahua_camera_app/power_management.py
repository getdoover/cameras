import asyncio
import logging
import time

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from camera_iface import CameraConfig
    from pydoover.docker import PlatformInterface

log = logging.getLogger(__name__)

class SnapshotInProgress(Exception):
    def __init__(self):
        super().__init__("There is already a snapshot in progress for this camera.")


class PowerContext:
    def __init__(self, camera_rtsp: str, manager: "CameraPowerManagement"):
        self.camera = camera_rtsp
        self.manager = manager

    async def __aenter__(self):
        if not self.manager.plt_iface:
            # early exit if we don't define the platform interface (testing, etc.)
            return

        try:
            await self.manager.power_on()
        except Exception as e:
            log.error(f"Failed to power on camera {e}")

        if not self.manager.is_powered:
            return  # something went wrong with trying to turn the power on...

        wake_delay = self.manager.config.wake_delay.value
        to_sleep = wake_delay - (time.time() - self.manager.start_powered)
        if to_sleep > 0:
            await asyncio.sleep(to_sleep)

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.manager.handle_cam_done(self.camera)

    def __await__(self):
        return self.__aenter__().__await__()


class CameraPowerManagement:
    def __init__(self, plt_iface: "PlatformInterface", config: "CameraConfig"):
        self.plt_iface = plt_iface
        self.config = config

        self.is_powered: bool = False
        self.start_powered = None

        self.active_cameras = set()
        self.delayed_poweroff_tasks: dict[str, asyncio.Task] = dict()


    async def schedule_power_off(self):
        # this is a worst-case scenario something breaks and the camera won't get turned off.
        pin = self.config.power_pin.value
        if pin is None:
            log.debug("No power pin found, cannot schedule power off")
            return

        log.debug(f"Scheduling power off in {self.config.power_timeout.value} seconds")
        await self.plt_iface.schedule_do_async(pin, False, self.config.power_timeout.value)

    async def power_on(self):
        log.debug("Powering on cameras")
        # any calls to this should reset a timer to turn off the cameras
        await self.schedule_power_off()

        if self.is_powered:
            log.debug("Cameras are already powered on")
            return

        pin = self.config.power_pin.value
        if pin is None:
            log.debug("No power pin found, cannot power on")
            return

        logging.info("Powering cameras on")
        await self.plt_iface.set_do_async(pin, True)
        self.is_powered = True
        self.start_powered = time.time()

    async def power_off(self):
        log.debug("Powering off cameras")
        if not self.is_powered:
            log.debug("Cameras are already powered off")
            return

        pin = self.config.power_pin.value
        if pin is None:
            log.debug("No power pin found, cannot power off")
            return

        log.info("Powering cameras off")
        await self.plt_iface.set_do_async(pin, False)
        self.is_powered = False
        self.start_powered = None

    async def handle_cam_done(self, camera_to_handle: str):
        log.debug(f"Handling camera done for {camera_to_handle}")
        self.active_cameras.remove(camera_to_handle)

        if len(self.active_cameras) == 0:
            await self.power_off()

    def acquire(self, camera_to_manage: str):
        log.debug(f"Acquiring camera {camera_to_manage}")
        if camera_to_manage in self.active_cameras:
            log.debug(f"Snapshot already in progress for {camera_to_manage}")
            raise SnapshotInProgress()

        log.debug(f"Adding {camera_to_manage} to active cameras")
        self.active_cameras.add(camera_to_manage)
        return PowerContext(camera_to_manage, self)

    async def _handle_done_in(self, cam: str, delay: int):
        log.debug(f"Sleeping for {delay} seconds before handling camera done for {cam}")
        await asyncio.sleep(delay)
        log.debug(f"Handling delayed camera done task for {cam}")
        await self.handle_cam_done(cam)

    async def acquire_for(self, camera_to_manage: str, delay: int):
        await PowerContext(camera_to_manage, self)
        self.active_cameras.add(camera_to_manage)

        # just to make sure if this is called multiple times it won't turn camera off early
        try:
            self.delayed_poweroff_tasks[camera_to_manage].cancel()
        except KeyError:
            pass

        task = asyncio.create_task(self._handle_done_in(camera_to_manage, delay))
        task.add_done_callback(lambda _: self.delayed_poweroff_tasks.pop(camera_to_manage, None))
        self.delayed_poweroff_tasks[camera_to_manage] = task
        log.debug(f"Done acquiring power for camera, due to expire in {delay} seconds")