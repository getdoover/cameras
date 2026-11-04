import asyncio
import base64
import io
import json
import logging
import re

from datetime import datetime, timedelta

import aiohttp
from pydoover.models import File

from .base import CameraBase, MAX_MESSAGE_SIZE
from ..clients import DahuaClient
from ..events import (
    DetectionTarget,
    DetectionZone,
    MotionDetectEvent,
    MotionDetectEventType,
)

# Dahua expresses IVS coordinates against a virtual screen of this size, rather
# than the real resolution.
DAHUA_NORMALIZED_SCREEN = 8192

# App target vocabulary <-> Dahua's IVS object types.
TARGET_TO_DAHUA = {
    DetectionTarget.person: "Human",
    DetectionTarget.vehicle: "Vehicle",
}
DAHUA_TO_TARGET = {v: k for k, v in TARGET_TO_DAHUA.items()}


EVENT_MATCH = re.compile(
    r"(?P<boundary>.*)\r\n"
    r"Content-Type: (?P<content>.*)\r\n"
    r"Content-Length: (?P<content_length>\d*)\r\n\r\n"
    r"Code=(?P<code>.*);action=(?P<action>.*);index=(?P<index>.*);data=(?P<data>.*)",
    re.DOTALL,
)


log = logging.getLogger(__name__)


class DahuaCameraBase(CameraBase):
    # Dahua exposes IVS rules rather than a fixed set of region slots, and this maps
    # zones onto the rules already configured on the camera — so `max_zones` is a
    # sane cap rather than something the camera advertises.
    #
    # NOTE: unverified — the zone read/write below is written against Dahua's
    # documented config layout but has never run against a real Dahua camera.
    ZONE_CAPABILITIES = {
        "supported": True,
        "max_zones": 8,
        "min_points": 3,
        "max_points": 8,
        "targets": [t.value for t in TARGET_TO_DAHUA],
        # Sensitivity on Dahua lives in the rule's size filter, not the region.
        "supports_sensitivity": False,
        "supports_per_zone_targets": True,
        "supports_disable": True,
    }

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

    # -- Detection zones --

    @staticmethod
    def _to_native(x: float, y: float) -> tuple:
        """Normalised (0..1, top-left origin) -> Dahua's 0..8191 screen."""
        top = DAHUA_NORMALIZED_SCREEN - 1
        return (round(x * top), round(y * top))

    @staticmethod
    def _from_native(x: int, y: int) -> tuple:
        """Dahua's 0..8191 screen -> normalised (0..1, top-left origin)."""
        top = DAHUA_NORMALIZED_SCREEN - 1
        return (x / top, y / top)

    async def get_detection_zones(self) -> list:
        zones = []
        for rule in await self.client.get_ivs_regions():
            if not rule["points"]:
                continue  # a rule with no region drawn
            zones.append(
                DetectionZone(
                    id=rule["index"],
                    name=rule["name"],
                    enabled=rule["enabled"],
                    points=[self._from_native(x, y) for x, y in rule["points"]],
                    targets=[
                        DAHUA_TO_TARGET[t]
                        for t in rule["targets"]
                        if t in DAHUA_TO_TARGET
                    ],
                )
            )
        return zones

    async def set_detection_zones(self, zones: list) -> None:
        """Write each zone onto the IVS rule with the matching id.

        Zones map onto rules that already exist on the camera — this doesn't create
        IVS rules, since their type (CrossRegionDetection etc.) and size filters are
        set up on the camera itself. A zone whose id has no rule is rejected rather
        than silently dropped.
        """
        existing = {r["index"]: r for r in await self.client.get_ivs_regions()}

        for zone in zones:
            if zone.id not in existing:
                raise ValueError(
                    f"No IVS rule {zone.id} on this camera "
                    f"(configured rules: {sorted(existing)})"
                )
            if not (
                self.ZONE_CAPABILITIES["min_points"]
                <= len(zone.points)
                <= self.ZONE_CAPABILITIES["max_points"]
            ):
                raise ValueError(
                    f"Zone {zone.id} needs between "
                    f"{self.ZONE_CAPABILITIES['min_points']} and "
                    f"{self.ZONE_CAPABILITIES['max_points']} points, got "
                    f"{len(zone.points)}"
                )

        for zone in zones:
            targets = [TARGET_TO_DAHUA[t] for t in zone.targets if t in TARGET_TO_DAHUA]
            await self.client.set_ivs_region(
                zone.id,
                [self._to_native(x, y) for x, y in zone.points],
                targets=targets or None,
            )
            await self.client.set_ivs_rule(0, zone.id, zone.enabled)

        log.info(f"Wrote {len(zones)} detection zone(s) to the camera.")

    async def get_still_snapshot(self, rtsp_uri: str) -> File:
        # we don't need to use ffmpeg on this, just use the camera's built-in stuff

        snap = await self.client.get_snapshot()
        return File(
            filename="snapshot.jpg",
            data=snap,
            size=len(snap),
            content_type="image/jpeg",
        )
        # we need to do a bit of compression because normal images are ~255kB,
        # we have a 128kB max limit on the websocket. by reducing the quality to 10% we can get them down to ~50kB.
        # proj = base64.b64encode(snap)
        # log.info(f"Original resolution image is {len(proj) / 1000}kB.")
        # if len(proj) > MAX_MESSAGE_SIZE:
        #     log.info("Downscaling original image to 10% quality.")
        #     im = Image.open(io.BytesIO(snap))
        #     buf = io.BytesIO()
        #     im.save(buf, "JPEG", quality=10)
        #     proj = base64.b64encode(buf.getbuffer())
        # return proj

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
