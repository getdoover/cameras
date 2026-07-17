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

from .base import CameraBase, THUMBNAIL_FILENAME
from ..clients import HikvisionClient
from ..clients.hikvision import NORMALIZED_SCREEN
from ..events import (
    DetectionTarget,
    DetectionZone,
    MotionDetectEvent,
    MotionDetectEventType,
)


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

# App target vocabulary <-> Hikvision's tokens (from the camera's advertised
# detectionTarget opt="all,human,vehicle,animal,others").
TARGET_TO_HIK = {
    DetectionTarget.person: "human",
    DetectionTarget.vehicle: "vehicle",
    DetectionTarget.animal: "animal",
    DetectionTarget.other: "others",
}
HIK_TO_TARGET = {v: k for k, v in TARGET_TO_HIK.items()}


class HikvisionAcuSenseCamera(CameraBase):
    # Read off this camera's own /ISAPI/Smart/FieldDetection/1/capabilities:
    # 4 region slots, 3-10 points each, sensitivity 1-100.
    ZONE_CAPABILITIES = {
        "supported": True,
        "max_zones": 4,
        "min_points": 3,
        "max_points": 10,
        "targets": [t.value for t in TARGET_TO_HIK],
        "supports_sensitivity": True,
        "supports_per_zone_targets": True,
        # The camera answers OK to a region <enabled> change and then ignores it, so
        # a zone can't be switched off - it has to be removed. The frontend should
        # offer delete rather than a toggle.
        "supports_disable": False,
    }

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
        else:
            # Still has to run: it links `center`, without which the camera never
            # puts detections on the alertStream and we see nothing. Passing False
            # leaves the deterrent off, which is what's wanted here.
            await self.client.set_smart_alarm_linkage(False)

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

    async def record_event_video(
        self, since: datetime, stop: asyncio.Event, max_secs: int
    ) -> File:
        """
        Capture one video covering an intruder event, as a single file.

        ``stop`` is set by the caller once the event has gone quiet, so the video
        lasts as long as the intruder kept triggering detections (bounded by
        ``max_secs``).

        In ``ffmpeg`` mode we record the RTSP stream live for that whole span. In
        ``sd`` mode the camera has been recording it all along (the ``record``
        linkage), so we just wait for the event to finish and then pull the span back
        in one download.
        """
        if self.event_clip_mode == "ffmpeg":
            return await self.record_video_until(
                self.config.rtsp_uri, stop, max_secs
            )
        if self.event_clip_mode == "sd":
            try:
                await asyncio.wait_for(stop.wait(), timeout=max_secs)
            except asyncio.TimeoutError:
                pass
            return await self._fetch_sd_video(since, datetime.now(tz=timezone.utc))
        return None

    async def _fetch_sd_video(self, start: datetime, end: datetime) -> File:
        """
        Download the camera's own recording of ``start`` -> ``end`` as one file.

        The camera stores recordings in fixed-length segments, so a search can return
        several, and any one of them can be far longer than the event. We therefore
        take the playbackURI it gives us and re-point its time range at the event,
        which makes the camera mux exactly that span for us.

        NOTE: unverified — the camera this was built against has no card fitted, so
        this path has never run against real recordings.
        """
        matches = await self.client.search_recordings(start, end)
        for match in matches:
            uri = match.get("mediaSegmentDescriptor.playbackURI")
            if not uri:
                continue

            data = await self.client.download_recording(
                self.client.bound_playback_uri(uri, start, end)
            )
            if not data:
                continue

            return File(
                filename="event.mp4",
                data=data,
                size=len(data),
                content_type="video/mp4",
            )

        log.info("Camera reported no recording for the event window.")
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

    # -- Detection zones --

    def _to_native(self, x: float, y: float) -> tuple:
        """Normalised (0..1, top-left origin) -> Hikvision's 0..1000 screen."""
        return (
            round(x * NORMALIZED_SCREEN),
            round(self._flip_y(y) * NORMALIZED_SCREEN),
        )

    def _from_native(self, x: int, y: int) -> tuple:
        """Hikvision's 0..1000 screen -> normalised (0..1, top-left origin)."""
        return (
            x / NORMALIZED_SCREEN,
            self._flip_y(y / NORMALIZED_SCREEN),
        )

    @staticmethod
    def _flip_y(y: float) -> float:
        """Convert between top-left and Hikvision's y axis.

        Kept as one function, called from both directions, so if the axis turns out
        to be the other way up it's a single change rather than a hunt. Involutive:
        flip(flip(y)) == y.
        """
        return 1.0 - y

    async def get_detection_zones(self) -> list:
        cfg = await self.client.get_field_detection_regions()
        zones = []
        for region in cfg:
            points = [self._from_native(x, y) for x, y in region["points"]]
            if not points:
                continue  # an unconfigured slot - the camera keeps 4 of them
            zones.append(
                DetectionZone(
                    id=region["id"],
                    points=points,
                    enabled=region["enabled"],
                    targets=[
                        HIK_TO_TARGET[t]
                        for t in region["targets"]
                        if t in HIK_TO_TARGET
                    ],
                    sensitivity=region["sensitivity"],
                )
            )
        return zones

    async def set_detection_zones(self, zones: list) -> None:
        max_zones = self.ZONE_CAPABILITIES["max_zones"]
        if len(zones) > max_zones:
            raise ValueError(f"This camera supports at most {max_zones} zones")

        regions = []
        for index, zone in enumerate(zones, start=1):
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

            targets = [TARGET_TO_HIK[t] for t in zone.targets if t in TARGET_TO_HIK]
            regions.append(
                {
                    # The camera addresses regions by slot, so renumber rather than
                    # trusting whatever ids the frontend sent.
                    "id": index,
                    "points": [self._to_native(x, y) for x, y in zone.points],
                    "targets": targets or list(RULE_TARGETS),
                    "sensitivity": (
                        zone.sensitivity
                        if zone.sensitivity is not None
                        else self.config.sensitivity.value
                    ),
                }
            )

        # The camera keeps every region slot it has, so one we simply leave out of
        # the body holds on to its old polygon — zones could be edited but never
        # deleted. Blank the slots we aren't using so dropping a zone removes it.
        for index in range(len(regions) + 1, max_zones + 1):
            regions.append(
                {
                    "id": index,
                    "points": [],
                    "targets": list(RULE_TARGETS),
                    "sensitivity": self.config.sensitivity.value,
                }
            )

        log.info(f"Writing {len(zones)} detection zone(s) to the camera.")
        await self.client.set_field_detection_regions(regions)

    async def get_thumbnail(self) -> File:
        """Grab the camera's sub-stream picture rather than scaling one ourselves.

        It's already thumbnail-sized (640x360, ~18KB vs 1920x1080/~117KB on the main
        stream), so this is a single HTTP GET with no ffmpeg — meaning thumbnails
        work on the slim image.
        """
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
        """Ask the camera whether its IR-cut filter is engaged.

        This beats inspecting the image: a grey/foggy daylight scene is also washed
        out, but the filter's position is ground truth. Only ``day``/``night`` are an
        answer — ``auto``/``schedule`` describe how the camera decides, not what it
        decided, so those return None and the consumer works it out from the
        thumbnail instead.
        """
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
