"""Hikvision DeepinView ANPR engine.

Targets the Hikvision iDS-2CD7A46G2/P-IZHSY (DeepinView ANPR bullet) and similar
"/P" road-traffic models. These cameras dedicate their deep-learning engine to
ANPR (license plate + vehicle) and expose *no* on-camera perimeter/person
classification — so the night-intruder trigger falls back to basic motion
detection (VMD), which coexists with ANPR on a separate subsystem.

Events arrive over the ISAPI alertStream (see ``HikvisionClient.stream_events``):
  * ``eventType == ANPR``  -> plate/vehicle read  -> ``anpr_callback``
  * ``eventType == VMD``   -> unclassified motion -> ``motion_detect_callback``

Night alarm: this firmware's VMD trigger cannot link to the alarm-output relay
via the API, so the app pulses the relay itself (``pulse_io_output``). The
camera's own buzzer (``beep``) *can* be linked natively and is armed/disarmed on
schedule as a doover-independent local alarm.
"""

import asyncio
import logging
from datetime import datetime, timedelta

import aiohttp
from pydoover.models import File

from .base import CameraBase, THUMBNAIL_FILENAME
from ..clients import HikvisionClient
from ..events import ANPREvent, MotionDetectEvent, MotionDetectEventType


log = logging.getLogger(__name__)


class HikvisionANPRCamera(CameraBase):
    def __init__(
        self,
        config,
        motion_detect_callback,
        anpr_callback,
        sync_presets_func,
        clear_active_preset_func,
    ):
        super().__init__(config)

        self.client: HikvisionClient = None
        self._session: aiohttp.ClientSession = None
        self.stream_events_task = None

        self.on_motion_event_callback = motion_detect_callback
        self.on_anpr_event_callback = anpr_callback
        self.sync_presets_func = sync_presets_func
        self.clear_active_preset_func = clear_active_preset_func

        # Tracks the last armed state so scheduler transitions are idempotent.
        self._night_armed: bool = None

    async def setup(self):
        self._session = aiohttp.ClientSession()
        self.client = HikvisionClient(
            self.config.connection.username.value,
            self.config.connection.password.value,
            self.config.connection.address.value,
            self.config.connection.control_port.value,
            self.config.connection.rtsp_port.value,
            self._session,
        )

        try:
            status = await self.client.get_status()
        except TimeoutError:
            log.exception("Failed to get camera status")
            return False

        if not status:
            log.info("Camera is offline, failed to get status.")
            return False

        if self.config.anpr.enabled.value:
            log.info("Enabling on-camera ANPR (vehicle + license plate).")
            try:
                await self.client.enable_vehicle_detection(True)
            except Exception as e:
                log.warning(f"Failed to enable ANPR: {e}", exc_info=e)

        if self.config.alarm.intruder_alarm_enabled.value:
            # Basic motion detection feeds the night-intruder trigger; make sure
            # it's on so the alertStream reports VMD events.
            try:
                await self.client.set_motion_detection(enabled=True)
            except Exception as e:
                log.warning(f"Failed to enable motion detection: {e}", exc_info=e)
            # Apply the correct arm state for the current time immediately.
            await self.arm_night_alarm(self.config.is_night())

        self.stream_events_task = asyncio.create_task(
            self.client.stream_events(self.on_cam_event)
        )
        return True

    async def close(self):
        if self.stream_events_task:
            self.stream_events_task.cancel()
        if self._session and not self._session.closed:
            await self._session.close()

    async def on_cam_event(self, event: dict):
        event_type = event.get("eventType", "")
        event_state = event.get("eventState", "")

        if event_type == "ANPR":
            anpr = ANPREvent.from_alert(event)
            min_conf = self.config.anpr.min_confidence.value
            if anpr.confidence is not None and min_conf and anpr.confidence < min_conf:
                log.info(
                    f"Ignoring low-confidence plate {anpr.plate} ({anpr.confidence} < {min_conf})"
                )
                return
            log.info(f"ANPR read: plate={anpr.plate} vehicle={anpr.vehicle_type}")
            await self._invoke(self.on_anpr_event_callback, anpr)
            return

        # Basic motion detection (no on-camera human/vehicle classification).
        # Only the leading edge ("active") matters; "inactive" is the clear.
        if event_type == "VMD" and event_state == "active":
            log.info("Motion (VMD) detected.")
            await self._invoke(
                self.on_motion_event_callback,
                MotionDetectEvent(MotionDetectEventType.motion, event),
            )

    async def fire_alarm(self):
        """Pulse the external siren/strobe relay (called by the app at night)."""
        port = self.config.alarm.output_port.value
        duration = self.config.alarm.pulse_secs.value
        log.info(f"Firing alarm: pulsing IO output {port} for {duration}s.")
        try:
            await self.client.pulse_io_output(port, duration)
        except Exception as e:
            log.warning(f"Failed to pulse alarm output: {e}", exc_info=e)

    async def arm_night_alarm(self, armed: bool):
        """Arm/disarm the camera's native buzzer linkage on schedule (idempotent)."""
        if armed == self._night_armed:
            return
        self._night_armed = armed

        if not self.config.alarm.beep_enabled.value:
            return
        try:
            await self.client.set_motion_beep_linkage(armed)
            log.info(f"Night buzzer linkage {'armed' if armed else 'disarmed'}.")
        except Exception as e:
            log.warning(f"Failed to set buzzer linkage: {e}", exc_info=e)

    async def get_still_snapshot(self, rtsp_uri: str) -> File:
        """Use the ISAPI snapshot endpoint instead of ffmpeg."""
        snap = await self.client.get_snapshot(channel=1)
        return File(
            filename="snapshot.jpg",
            data=snap,
            size=len(snap),
            content_type="image/jpeg",
        )

    async def get_thumbnail(self) -> File:
        """The camera's sub-stream picture is already thumbnail-sized, so take that
        rather than spending an ffmpeg pass scaling the main stream down."""
        try:
            snap = await self.client.get_snapshot(channel=1, subtype=1)
        except Exception as e:
            log.info(f"Couldn't fetch sub-stream thumbnail: {e}")
            return None
        return File(
            filename=THUMBNAIL_FILENAME,
            data=snap,
            size=len(snap),
            content_type="image/jpeg",
        )

    async def detect_night(self) -> bool:
        """Ask the camera whether its IR-cut filter is engaged; None if it won't say
        (``auto``/``schedule`` describe how it decides, not what it decided)."""
        try:
            cfg = await self.client.get_ir_cut_filter()
        except Exception as e:
            log.info(f"Couldn't read the camera's day/night state: {e}")
            return None

        mode = (cfg.get("IrcutFilterType") or "").strip().lower()
        if mode in ("day", "night"):
            return mode == "night"
        return None

    async def ping(self, timeout: int):
        start = datetime.now()

        while datetime.now() - start < timedelta(seconds=timeout):
            try:
                status = await self.client.get_status()
            except OSError:
                pass
            else:
                if status is True:
                    log.info(f"Status call succeeded, result: {status}")
                    return True

            log.info("Failed to ping camera. Waiting 0.5sec...")
            await asyncio.sleep(0.5)

        log.info("Failed to ping camera in time, quitting...")
        return False

    @staticmethod
    async def _invoke(callback, *args):
        if asyncio.iscoroutinefunction(callback):
            await callback(*args)
        else:
            callback(*args)
