"""Hikvision AcuSense ColorVu engine.

Targets Hikvision AcuSense cameras (e.g. DS-2CD2387G3-LIS2UY/SRB) that classify
targets on-camera as human / vehicle / animal via line-crossing and intrusion
(field) detection. Unlike the ANPR "/P" models, these expose real perimeter
analytics, so this is the driver for person/intruder detection.

Events arrive over the ISAPI alertStream. AcuSense smart events carry the
classification per-event inside ``<DetectionRegionList><DetectionRegionEntry>``:

  <eventType>fielddetection</eventType>
  <eventState>active</eventState>
  <DetectionRegionList><DetectionRegionEntry>
    <detectionTarget>human</detectionTarget>       <- person / vehicle / animal
    <TargetRect>...</TargetRect>
  </DetectionRegionEntry></DetectionRegionList>

Night deterrent: the ColorVu white light can flash on a smart event natively
(``eventIntelligence`` supplement-light mode) — doover-independent — and the app
additionally pulses the alarm-output relay + sends a notification.
"""

import asyncio
import logging
import shutil
from datetime import datetime, timedelta, timezone

import aiohttp
from pydoover.models import File

from .base import CameraBase
from ..clients import HikvisionClient
from ..events import MotionDetectEvent, MotionDetectEventType


log = logging.getLogger(__name__)

# AcuSense smart-event types that carry a classified target.
SMART_EVENT_TYPES = {
    "fielddetection",
    "linedetection",
    "regionEntrance",
    "regionExiting",
}

# The on-camera intrusion rule always classifies both, regardless of the app's
# Object Detection setting — that setting shapes which events raise notifications
# downstream, not what the camera looks for.
RULE_TARGETS = ["human", "vehicle"]


class HikvisionAcuSenseCamera(CameraBase):
    def __init__(
        self,
        config,
        motion_detect_callback,
        sync_presets_func,
        clear_active_preset_func,
    ):
        super().__init__(config)

        self.client: HikvisionClient = None
        self._session: aiohttp.ClientSession = None
        self.stream_events_task = None

        self.on_motion_event_callback = motion_detect_callback
        self.sync_presets_func = sync_presets_func
        self.clear_active_preset_func = clear_active_preset_func

        self._deterrent_armed: bool = None
        # True once the camera has accepted a native arming schedule, in which case
        # the app must stop toggling the linkage itself (the schedule owns it).
        self.native_schedule_active: bool = False
        # How event clips get captured: "sd" (camera records to its microSD, we
        # fetch over ContentMgmt), "ffmpeg" (we record the RTSP stream ourselves),
        # or None (clips off / not possible). Resolved in setup().
        self.event_clip_mode: str = None
        # playbackURIs already uploaded, so a re-fetch during one event doesn't
        # upload the same SD segment twice.
        self._fetched_clip_uris: set = set()

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

        # Do this first: a camera that thinks it's 2019 breaks its own arming
        # schedule and makes recording searches return nothing.
        await self.sync_camera_clock()

        sensitivity = self.config.sensitivity.value
        log.info(
            f"Configuring intrusion detection: targets={RULE_TARGETS} "
            f"sensitivity={sensitivity}"
        )
        try:
            await self.client.set_field_detection(True, RULE_TARGETS, sensitivity)
        except Exception as e:
            log.warning(f"Failed to configure intrusion detection: {e}", exc_info=e)

        # Resolve this before arming: the deterrent only adds the `record` linkage
        # when we're actually going to read recordings back off the camera.
        self.event_clip_mode = await self._resolve_event_clip_mode()

        if self.config.alarm.intruder_alarm_enabled.value:
            await self.setup_night_deterrent()

        self.stream_events_task = asyncio.create_task(
            self.client.stream_events(self.on_cam_event)
        )
        return True

    async def close(self):
        if self.stream_events_task:
            self.stream_events_task.cancel()
        if self._session and not self._session.closed:
            await self._session.close()

    @staticmethod
    def _extract_target(event: dict) -> str:
        """Pull the classified target (human/vehicle/animal) out of a smart event."""
        for key, value in event.items():
            if key.lower().endswith("detectiontarget") and value:
                # Multiple entries could be comma-joined; take the first.
                return value.split(",")[0].strip().lower()
        return ""

    async def on_cam_event(self, event: dict):
        event_type = event.get("eventType", "")
        event_state = event.get("eventState", "")

        if event_type not in SMART_EVENT_TYPES or event_state != "active":
            return

        target = self._extract_target(event)
        match target:
            case "human":
                event_type_enum = MotionDetectEventType.person
            case "vehicle":
                event_type_enum = MotionDetectEventType.vehicle
            case _:
                # animal / others / unclassified — still real motion in the zone.
                event_type_enum = MotionDetectEventType.motion

        log.info(f"AcuSense {event_type} target={target or 'unknown'}")
        await self._invoke(
            self.on_motion_event_callback,
            MotionDetectEvent(event_type_enum, event),
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

    async def sync_camera_clock(self, max_drift_secs: int = 60) -> bool:
        """Keep the camera's clock in step with ours, correcting it when it drifts.

        Ours comes from the doovit, which is NTP-synced; the camera's is manual and
        resets to 2019 on a power cut, so this is checked periodically rather than
        only at setup. Returns whether the clock was (re)set.
        """
        now = datetime.now().astimezone()
        try:
            current = await self.client.get_time()
            camera_time = datetime.fromisoformat(current["localTime"])
        except (KeyError, ValueError, TypeError) as e:
            log.info(f"Couldn't read the camera clock ({e}); setting it anyway.")
        except Exception as e:
            log.warning(f"Failed to read camera clock: {e}", exc_info=e)
            return False
        else:
            drift = abs((camera_time - now).total_seconds())
            if drift <= max_drift_secs:
                return False
            log.info(
                f"Camera clock is {drift:.0f}s out (camera={camera_time.isoformat()}, "
                f"app={now.isoformat()}); correcting."
            )

        try:
            await self.client.set_time(now)
        except Exception as e:
            log.warning(f"Failed to set camera clock: {e}", exc_info=e)
            return False

        log.info(f"Camera clock set to {now.isoformat()}.")
        return True

    def _deterrent_methods(self) -> list:
        methods = []
        if self.config.alarm.white_light_deterrent.value:
            methods.append("whiteLight")
        if self.config.alarm.audio_alarm.value:
            methods.append("audio")
        if self.event_clip_mode == "sd":
            # Record the event to the camera's microSD so the app can fetch the
            # clip afterwards over ContentMgmt (no ffmpeg needed).
            methods.append("record")
        return methods

    async def _resolve_event_clip_mode(self) -> str:
        """Pick how event clips get captured, preferring the camera's own storage.

        SD recording is preferred: it needs no ffmpeg (so it works on the slim image)
        and the camera keeps recording even while doover is offline. Without usable
        storage we fall back to recording the RTSP stream ourselves, which needs the
        ffmpeg that only ships in the 'full' image — if that's missing too, clips
        aren't possible and the caller reverts to single snapshots.
        """
        if not self.config.alarm.event_clips_enabled.value:
            return None

        try:
            if await self.client.has_recording_storage():
                log.info("Event clips: using the camera's on-card recording.")
                return "sd"
            log.info("Event clips: camera reports no usable storage.")
        except Exception as e:
            log.warning(f"Failed to probe camera storage: {e}", exc_info=e)

        if shutil.which("ffmpeg"):
            log.info("Event clips: falling back to ffmpeg RTSP recording.")
            return "ffmpeg"

        log.warning(
            "Event clips enabled, but the camera has no usable storage and ffmpeg is "
            "unavailable (slim image) — falling back to single snapshots."
        )
        return None

    async def _set_linkage(self, armed: bool) -> None:
        try:
            await self.client.set_smart_alarm_linkage(armed, self._deterrent_methods())
        except Exception as e:
            log.warning(f"Failed to set alarm linkage: {e}", exc_info=e)

    async def setup_night_deterrent(self):
        """Arm the night deterrent, preferring the camera's native arming schedule.

        Preferred path: link flash / siren / record to the intrusion event
        permanently and let the camera's own arming schedule gate them to the night
        window, so the deterrent fires even while doover is offline. If the firmware
        won't accept a schedule we fall back to the app toggling the linkage at the
        night boundary (:meth:`arm_night_deterrent`, called each main loop).
        """
        methods = self._deterrent_methods()
        if not methods:
            return

        if self.config.alarm.white_light_deterrent.value:
            # ColorVu only *flashes* the light on a smart event in this mode; without
            # it the whiteLight linkage has nothing to drive.
            try:
                await self.client.set_supplement_light_mode("eventIntelligence")
            except Exception as e:
                log.warning(f"Failed to set supplement light mode: {e}", exc_info=e)

        try:
            self.native_schedule_active = await self.client.set_event_arming_schedule(
                self.config.alarm.night_start_hour.value,
                self.config.alarm.night_end_hour.value,
            )
        except Exception as e:
            log.warning(f"Failed to write native arming schedule: {e}", exc_info=e)
            self.native_schedule_active = False

        if self.native_schedule_active:
            # The schedule gates the linkage, so leave it permanently armed.
            await self._set_linkage(True)
            log.info(
                f"Night deterrent ({'+'.join(methods)}) armed via the camera's "
                f"native arming schedule."
            )
        else:
            log.info(
                "Camera did not accept a native arming schedule; falling back to "
                "app-driven arming."
            )
            await self.arm_night_deterrent(self.config.is_night())

    async def arm_night_deterrent(self, armed: bool):
        """Arm/disarm the built-in flash + siren active response (idempotent).

        Fallback path only — a no-op when the camera took a native arming schedule,
        which gates the linkage on-camera instead.
        """
        if self.native_schedule_active or armed == self._deterrent_armed:
            return
        self._deterrent_armed = armed

        methods = self._deterrent_methods()
        if not methods:
            return
        await self._set_linkage(armed)
        log.info(
            f"Night deterrent {'armed' if armed else 'disarmed'} "
            f"({'+'.join(methods)})."
        )

    # -- On-camera event clips (microSD -> ContentMgmt -> doover) --

    def reset_clip_tracking(self) -> None:
        """Forget uploaded segments, so the next event starts fetching afresh."""
        self._fetched_clip_uris.clear()

    async def get_event_clip(self, since: datetime) -> File:
        """
        Return the next event clip, or None if there isn't one to send yet.

        In ``sd`` mode this polls the camera's recordings and returns quickly (often
        with None while a segment is still being written). In ``ffmpeg`` mode it
        blocks for the clip length and returns that clip, so callers shouldn't add
        their own delay on top.
        """
        if self.event_clip_mode == "sd":
            return await self._fetch_sd_clip(since)
        if self.event_clip_mode == "ffmpeg":
            return await self.get_video_snapshot(
                self.config.rtsp_uri,
                secs=self.config.alarm.event_clip_interval.value,
            )
        return None

    async def _fetch_sd_clip(self, since: datetime) -> File:
        """
        Fetch the next not-yet-uploaded SD recording covering ``since`` -> now.

        Returns None when the camera has no new finalised segment yet — the camera
        only indexes a segment once it has finished writing it, so expect the first
        clip of an event to lag the detection.
        """
        matches = await self.client.search_recordings(
            since, datetime.now(tz=timezone.utc)
        )
        for match in matches:
            uri = match.get("mediaSegmentDescriptor.playbackURI")
            if not uri or uri in self._fetched_clip_uris:
                continue

            data = await self.client.download_recording(uri)
            if not data:
                continue

            self._fetched_clip_uris.add(uri)
            return File(
                filename="event.mp4",
                data=data,
                size=len(data),
                content_type="video/mp4",
            )
        return None

    async def get_still_snapshot(self, rtsp_uri: str) -> File:
        """Use the ISAPI snapshot endpoint instead of ffmpeg."""
        snap = await self.client.get_snapshot(channel=1)
        return File(
            filename="snapshot.jpg",
            data=snap,
            size=len(snap),
            content_type="image/jpeg",
        )

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
