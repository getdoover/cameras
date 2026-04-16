import asyncio
import logging

from datetime import datetime, timedelta

from pydoover.docker.device_agent.models import File

from .base import CameraBase
from ..clients import BoschClient
from ..events import MotionDetectEvent, MotionDetectEventType


log = logging.getLogger(__name__)


class BoschCameraBase(CameraBase):
    def __init__(self, config, motion_detect_callback, sync_presets_func, clear_active_preset_func):
        super().__init__(config)

        self.event_subscription_task = None
        self.client: BoschClient = None
        self.on_motion_event_callback = motion_detect_callback

        self.sync_presets_func = sync_presets_func
        self.clear_active_preset_func = clear_active_preset_func

    async def setup(self):
        self.client = BoschClient(
            self.config.connection.username.value,
            self.config.connection.password.value,
            self.config.connection.address.value,
            self.config.connection.control_port.value,
        )
        try:
            await self.client.connect()
        except Exception:
            log.exception("Failed to connect to Bosch camera via ONVIF")
            return False

        try:
            status = await self.client.get_status()
        except Exception:
            log.exception("Failed to get camera status")
            return False
        else:
            if not status:
                log.info("Camera is offline, failed to get status.")
                return False

        if self.config.human_detect_enabled or self.config.vehicle_detect_enabled:
            log.info(f"Starting ONVIF event subscription for motion detection: {self.config.object_detection.elements}")
            self.event_subscription_task = asyncio.create_task(
                self.client.subscribe_events(self.on_cam_event)
            )

        return True

    def close(self):
        if self.event_subscription_task:
            self.event_subscription_task.cancel()

    async def get_still_snapshot(self, rtsp_uri: str) -> File:
        snap = await self.client.get_snapshot()
        return File(
            filename="snapshot.jpg",
            data=snap,
            size=len(snap),
            content_type="image/jpeg",
        )

    async def on_cam_event(self, event_type_str: str, topic: str):
        match event_type_str:
            case "person":
                event_type = MotionDetectEventType.person
            case "vehicle":
                event_type = MotionDetectEventType.vehicle
            case _:
                event_type = MotionDetectEventType.unknown

        log.info(f"Detected ONVIF motion event: {event_type} (topic: {topic})")
        await self.on_motion_event_callback(MotionDetectEvent(event_type, {"topic": topic}))

    async def ping(self, timeout: int):
        start = datetime.now()

        while datetime.now() - start < timedelta(seconds=timeout):
            try:
                status = await self.client.get_status()
            except OSError:
                pass
            else:
                if status is True:
                    log.info(f"status call succeeded, result: {status}")
                    return True

            log.info("Failed to ping camera. Waiting 0.5sec...")
            await asyncio.sleep(0.5)

        log.info("Failed to ping camera in time, quitting...")
        return False
