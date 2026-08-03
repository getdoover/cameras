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

# How often the app re-writes the flash/siren linkage when it owns the arming (i.e.
# whenever the camera is armed wider than the night window). Purely a correction for
# drift we didn't cause — the day/night transition itself is handled the moment the
# main loop notices it, not on this cadence.
DETERRENT_REASSERT_SECS = 10 * 60

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
        # When the linkage was last written, for the periodic re-assert.
        self._deterrent_asserted_at: datetime = None
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

        await self.setup_region_entrance(sensitivity)

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

        SD recording is preferred where a card is fitted: the camera records the event
        itself, so we still get footage of anything that happened while doover was
        offline or restarting, and we get the pre-roll leading up to the trigger,
        which recording the stream ourselves can't (we only start once we're told).

        Both modes need ffmpeg. It's obvious for the RTSP fallback; for the card it's
        because the camera stores IMKH/MPEG-PS rather than mp4, so the download has to
        be remuxed before anything will play it (see :meth:`remux_to_mp4`). Without
        ffmpeg neither mode can produce a usable video, and the caller falls back to
        single snapshots.
        """
        if not self.config.alarm.event_clips_enabled.value:
            # Worth saying out loud: the only symptom otherwise is intruder events
            # quietly producing a still instead of a video, with nothing in the log.
            log.info(
                "Event video is disabled in config; intruder events will upload a "
                "single snapshot."
            )
            return None

        if not shutil.which("ffmpeg"):
            log.warning(
                "Event video is enabled but ffmpeg is unavailable — it's needed to "
                "record the stream, and to remux the camera's own recordings, which "
                "aren't mp4. This needs the '-full' image (the deployment template "
                "selects it when Event Video is on). Falling back to single snapshots."
            )
            return None

        try:
            if await self.client.has_recording_storage():
                log.info("Event video: using the camera's on-card recording.")
                return "sd"
            log.info("Event video: camera reports no usable storage.")
        except Exception as e:
            log.warning(f"Failed to probe camera storage: {e}", exc_info=e)

        log.info("Event video: falling back to ffmpeg RTSP recording.")
        return "ffmpeg"

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

        night = (
            self.config.alarm.night_start_hour.value,
            self.config.alarm.night_end_hour.value,
        )
        # Night only, deliberately.
        #
        # This schedule used to be the union of the night window and the daytime
        # motion-snapshot window, because the schedule gates the *event* and a night-only
        # intrusion rule left daytime person/vehicle detection blind. Region entrance now
        # covers the day (see setup_region_entrance), so intrusion can go back to meaning
        # "night" — which is better in three ways:
        #
        #   * no duplicate events: intrusion re-alarms on static targets, entrance doesn't
        #   * the camera gates the deterrent itself again, so flash/siren still fire while
        #     doover is offline — the property the union sacrificed
        #   * the app no longer has to arm and disarm the linkage at dusk and dawn
        windows = [night]

        try:
            accepted = await self.client.set_event_arming_schedule(windows)
        except Exception as e:
            log.warning(f"Failed to write native arming schedule: {e}", exc_info=e)
            accepted = False

        self.native_schedule_active = accepted

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
                "app-driven arming, which re-asserts periodically and does NOT survive "
                "doover being offline across dusk."
            )
            await self.arm_night_deterrent(self.config.is_night())

    async def setup_region_entrance(self, sensitivity: int):
        """Arm region-entrance detection for the daytime snapshot window.

        Why a second rule rather than just using intrusion all day: intrusion has
        ``contAlarmForStaticTargetEnabled`` on (and ``targetAlarmInterval`` already at
        its maximum of 5), so it **re-alarms while a target stays in the region**. A car
        that drives in and parks therefore keeps producing events, each one costing a
        snapshot, an upload and an inference run. Region entrance fires once, when
        something crosses in.

        So the two rules split by job: entrance drives daytime snapshots, intrusion
        stays for the night alarm where re-alarming on a loiterer is a feature. Both
        classify targets, so person-vs-vehicle survives either way, and both are already
        in the engine's ``SMART_EVENT_TYPES``.
        """
        window = self.config.motion_snapshot_window
        if window is None:
            log.info("No motion-snapshot window; leaving region entrance disabled.")
            return

        try:
            await self.client.set_region_entrance(True, RULE_TARGETS, sensitivity)
        except Exception as e:
            log.warning(f"Failed to configure region entrance: {e}", exc_info=e)
            return

        # `center` is what puts the event on the alertStream. Without it the camera
        # handles the event silently and the app never hears about it -- the same trap
        # the intrusion trigger ships with.
        try:
            await self.client.set_smart_alarm_linkage(False, event="regionEntrance")
        except Exception as e:
            log.warning(f"Failed to link regionEntrance to center: {e}", exc_info=e)
            return

        # Its own schedule, covering only the window snapshots are wanted in. The night
        # deterrent's schedule is separate and belongs to fielddetection.
        try:
            accepted = await self.client.set_event_arming_schedule(
                [window], event="regionEntrance"
            )
        except Exception as e:
            log.warning(f"Failed to schedule region entrance: {e}", exc_info=e)
            return

        log.info(
            f"Region entrance armed for {window} "
            f"({'native schedule' if accepted else 'schedule rejected — always armed'})."
        )

    async def arm_night_deterrent(self, armed: bool):
        """Arm/disarm the built-in flash + siren active response.

        A no-op when the camera took a night-only arming schedule, which gates the
        linkage on-camera instead. Otherwise this is the only thing standing between a
        siren and a delivery driver at midday, so as well as acting on every change it
        **re-asserts periodically**: the linkage lives on the camera, where a web-UI
        visit, a firmware quirk or a factory-reset can silently change it, and drift in
        this direction is a siren going off in daylight rather than a missed log line.
        """
        if self.native_schedule_active:
            return

        now = datetime.now(tz=timezone.utc)
        changed = armed != self._deterrent_armed
        stale = (
            self._deterrent_asserted_at is None
            or (now - self._deterrent_asserted_at).total_seconds()
            >= DETERRENT_REASSERT_SECS
        )
        if not (changed or stale):
            return

        methods = self._deterrent_methods()
        if not methods:
            return

        self._deterrent_armed = armed
        self._deterrent_asserted_at = now
        await self._set_linkage(armed)
        if changed:
            log.info(
                f"Night deterrent {'armed' if armed else 'disarmed'} "
                f"({'+'.join(methods)})."
            )
        else:
            log.debug(
                f"Re-asserted night deterrent {'armed' if armed else 'disarmed'} "
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

        ``start``/``end`` only select which segment to fetch — they don't trim it. The
        camera hands back the whole segment (see
        :meth:`HikvisionClient.download_recording`), which for an event-triggered
        recording is roughly the event plus the camera's pre/post roll. That pre-roll
        is the reason to prefer this over recording the stream ourselves: it covers
        the moments *before* the trigger, which we can't, because we only start once
        the camera tells us.

        What comes back is *not* an mp4 despite the name — it's Hikvision's IMKH
        container around an MPEG program stream — so it gets remuxed before upload
        (see :meth:`remux_to_mp4`).
        """
        matches = await self.client.search_recordings(start, end)
        for match in matches:
            uri = match.get("mediaSegmentDescriptor.playbackURI")
            if not uri:
                continue

            data = await self.client.download_recording(uri)
            if not data:
                continue

            log.info(f"Fetched {len(data)} bytes of on-card recording; remuxing.")
            return await self.remux_to_mp4(data, "event")

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
