import asyncio
import base64
import io
import json
import logging
import re

from datetime import datetime, timedelta

import aiohttp
from PIL import Image

from .base import CameraBase, MAX_MESSAGE_SIZE
from ..clients import DahuaClient
from ..events import MotionDetectEvent, MotionDetectEventType


EVENT_MATCH = re.compile(
    r"(?P<boundary>.*)\r\n"
    r"Content-Type: (?P<content>.*)\r\n"
    r"Content-Length: (?P<content_length>\d*)\r\n\r\n"
    r"Code=(?P<code>.*);action=(?P<action>.*);index=(?P<index>.*);data=(?P<data>.*)",
    re.DOTALL,
)


log = logging.getLogger(__name__)


class DahuaCameraBase(CameraBase):
    def __init__(self, config, motion_detect_callback, sync_presets_func, clear_active_preset_func):
        super().__init__(config)

        self.last_processed_id = None

        self.stream_events_task = None
        self.client: DahuaClient = None
        self.on_motion_event_callback = motion_detect_callback

        self.sync_presets_func = sync_presets_func
        self.clear_active_preset_func = clear_active_preset_func

    async def setup(self):
        self.client = DahuaClient(
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

        if self.config.human_detect_enabled or self.config.vehicle_detect_enabled:
            log.info(f"Starting motion detection: {self.config.object_detection.elements}")
            await self.client.enable_smart_motion_detection(
                human=self.config.human_detect_enabled,
                vehicle=self.config.vehicle_detect_enabled,
            )
            events = ["SmartMotionHuman", "SmartMotionVehicle"]
            self.stream_events_task = asyncio.create_task(
                self.client.stream_events(self.on_cam_event, events)
            )

        return True

    def close(self):
        if self.stream_events_task:
            self.stream_events_task.cancel()

    async def get_still_snapshot(self) -> bytes:
        # we don't need to use ffmpeg on this, just use the camera's built-in stuff

        snap = await self.client.get_snapshot()
        # we need to do a bit of compression because normal images are ~255kB,
        # we have a 128kB max limit on the websocket. by reducing the quality to 10% we can get them down to ~50kB.
        proj = base64.b64encode(snap)
        log.info(f"Original resolution image is {len(proj) / 1000}kB.")
        if len(proj) > MAX_MESSAGE_SIZE:
            log.info("Downscaling original image to 10% quality.")
            im = Image.open(io.BytesIO(snap))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=10)
            proj = base64.b64encode(buf.getbuffer())

        return proj

    async def on_cam_event(self, data: bytes, _):
        match = EVENT_MATCH.search(data.decode())
        if not (match and match.group("action") == "Start"):
            return  # this will also ignore heartbeat events

        data = json.loads(match.group("data"))

        match match.group("code"):
            case "SmartMotionHuman":
                event_type = MotionDetectEventType.person
            case "SmartMotionVehicle":
                event_type = MotionDetectEventType.vehicle
            case _:
                event_type = MotionDetectEventType.unknown

        log.info(f"Detected motion detection event: {event_type}")
        await self.on_motion_event_callback(MotionDetectEvent(event_type, data))

    async def check_control_message(self, message_id, data):
        if self.config.control_enabled.value is False:
            log.info("Control not enabled, ignoring message.")
            return False

        if self.last_processed_id and message_id < self.last_processed_id:
            log.info("Task stale, skipping...")
            return False

        self.last_processed_id = message_id
        return True

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

            log.info(f"Failed to ping camera. Waiting 0.5sec...")
            await asyncio.sleep(0.5)


        log.info("Failed to ping camera in time, quitting...")
        return False
