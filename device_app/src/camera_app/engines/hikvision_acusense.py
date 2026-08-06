"""Hikvision AcuSense ColorVu engine.

Targets Hikvision AcuSense cameras (e.g. DS-2CD2387G3-LIS2UY/SRB) that classify
targets on-camera as human / vehicle / animal via line-crossing and intrusion
(field) detection. Unlike the ANPR "/P" models, these expose real perimeter
analytics, so this is the driver for person/intruder detection.

**Intrusion (``fielddetection``) is the general-purpose rule, day and night.** Presence in
the region is the trigger, so it catches a target that is already in frame, one that
appears inside the region, and one that crosses only the outer margin -- none of which
region entrance can see, because entrance needs the target tracked *outside* the region
first. The reason entrance used to own the day was intrusion's re-alarm on a static
target, which turned one parked car into a stream of snapshots. That re-alarm is kept — it
is the only signal that an intruder is *still there*, which the night alarm is built on —
and the duplicate cost is handled in the app instead, where the picture can be throttled
without throttling the alarm (see ``CameraApplication.claim_motion_snapshot``).

**Region entrance is back, for excluded areas only.** It was previously disabled
outright, on the reasoning that a second rule means a second event for the same person
walking through. That reasoning still holds and is still the default: entrance is written
*only* for zones the user explicitly marked ``excluded_area``, never as a duplicate of an
intrusion zone. What changed is that "somebody entered the area they're not allowed in" is
a different question from "somebody is in frame", and entrance answers it better — it
fires once on crossing in rather than re-alarming while they stand there.

The duplicate the old comment warned about is therefore now *possible* but deliberate:
an excluded area overlapping an intrusion zone genuinely reports twice for one person,
once per question. The app collapses that (see ``CameraApplication.zones_for_event``),
preferring the excluded-area zone, because it is the more specific statement.

Zones carry which rule they belong to (``ZoneKind``), so the two rules can no longer
drift the way they did when the same polygons were fanned out to both with separate
hardcoded defaults and nothing reconciling them.

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
from ..clients.hikvision import (
    ENTRANCE_EDGE_WARN,
    INTRUSION_DWELL_MAX_SECS,
    INTRUSION_DWELL_SECS,
    NORMALIZED_SCREEN,
)
from ..events import (
    DETECTOR_REQUIRES_TARGET,
    DetectionTarget,
    DetectionZone,
    MotionDetectEvent,
    MotionDetectEventType,
    RULE_FIELD_DETECTION,
    RULE_REGION_ENTRANCE,
    ZONE_RULE_EVENT_TYPES,
    ZoneDetector,
    ZoneKind,
    rule_for_event_type,
)


log = logging.getLogger(__name__)

# The smart events this engine acts on: intrusion, plus region entrance for excluded
# areas. Both are rules it configures itself.
#
# `regionExiting` and `linedetection` stay out on purpose rather than by oversight. The
# camera can run them alongside these, and each one that fires is another snapshot, upload
# and inference run for the same person walking through. Accepting an event type we never
# enable is a latent duplicate waiting for someone to tick a box in the camera's web UI —
# so the engine disables them and ignores them.
#
# Keys are lowercased `eventType` values; see `events.ZONE_RULE_EVENT_TYPES` for why the
# casing can't just be compared directly to the ISAPI path names.
SMART_EVENT_TYPES = set(ZONE_RULE_EVENT_TYPES)

# How the camera says "that target is still there".
#
# It does NOT repeat the smart event. Verified on an iDS-2CD5T87G2/V-XHSY (V5.9.20) with
# somebody standing in the zone for two minutes: `fielddetection` fired `active` exactly
# once, and from then on the camera sent `duration` alerts naming it —
#   <eventType>duration</eventType><eventState>active</eventState>
#   <DurationList><Duration><relationEvent>fielddetection</relationEvent></Duration>
# — every `targetAlarmInterval` seconds (5), interleaved with `fielddetection`/`inactive`.
#
# This is the only evidence an intruder hasn't left; there is nothing to poll. Ignoring it
# capped every night alarm at one cooldown after the first detection, however long the
# intruder stayed. `contAlarmForStaticTargetEnabled` must be on for these to arrive.
DURATION_EVENT_TYPE = "duration"

# Smart rules this engine turns off, because intrusion covers what they'd report and
# every one left on is a duplicate event per target. Names are ISAPI path segments and
# the casing is not uniform -- see `disable_smart_rule`.
#
# `regionEntrance` is NOT in here any more: it is now the excluded-area rule, enabled or
# disabled according to whether any excluded-area zones exist (see
# `_write_excluded_zones`). Putting it back here would fight that write every main loop.
UNUSED_SMART_RULES = ("regionExiting", "LineDetection")

# The on-camera intrusion rule always classifies both, regardless of the app's
# Object Detection setting — that setting shapes which events raise notifications
# downstream, not what the camera looks for.
RULE_TARGETS = ["human", "vehicle"]

# How many regionEntrance slots to assume when the camera won't tell us. The real number
# is read from its own capabilities at setup (`_read_entrance_max_zones`); this is only the
# floor to fall back on, chosen to match FieldDetection's four rather than guessed high —
# writing more regions than the rule has slots gets the whole PUT rejected.
DEFAULT_ENTRANCE_MAX_ZONES = 4

# How far the camera's clock may be out before it is rewritten.
#
# Seconds, not minutes, and that is the whole point: the arming schedule only cares about
# the hour, but the event-clip search asks the camera for a ~25s window around an event and
# the camera answers on its own clock. Measured on a DS-2CD2387G3 sitting 44s behind the
# doovit: every single search returned "no recording" while the footage was on the card the
# whole time, 44s from where we looked. The clock is manual (no reachable NTP), so it will
# keep drifting and this will keep correcting it.
MAX_CLOCK_DRIFT_SECS = 5

# Extra slack each side of the event window when searching the card, on top of the clock
# offset. Covers the residual drift under MAX_CLOCK_DRIFT_SECS, plus the camera opening its
# segment a moment after the trigger.
EVENT_CLIP_SEARCH_MARGIN = timedelta(seconds=30)

# How often the app re-writes the camera's arming schedule and the flash/siren linkage.
# Purely a correction for drift we didn't cause — a web-UI visit, a firmware quirk, a
# factory reset — since both live on the camera and nothing tells us when they change.
# Neither the day/night transition nor a config edit waits for this cadence: both are
# acted on the moment the main loop notices them.
REASSERT_SECS = 10 * 60

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
        # Per-zone dwell time: how long a target must be in the zone before it counts.
        # The range is the camera's own advertised one (timeThreshold min=0 max=60), and
        # the frontend needs it — unlike sensitivity, 0..60 isn't guessable.
        "supports_threshold": True,
        "threshold_min": INTRUSION_DWELL_SECS,
        "threshold_max": INTRUSION_DWELL_MAX_SECS,
        "supports_per_zone_targets": True,
        # The camera answers OK to a region <enabled> change and then ignores it, so
        # a zone can't be switched off - it has to be removed. The frontend should
        # offer delete rather than a toggle.
        "supports_disable": False,
        # Every zone carries a notify flag, so the frontend should offer the control.
        "supports_notify": True,
        # What each kind of zone can do here, because they are genuinely not
        # interchangeable and a frontend offering the wrong control gets silently
        # ignored by the camera.
        # What a zone can additionally be asked to look for, beyond what the camera
        # classifies. Not zone kinds: a single zone can want any combination of these
        # alongside its person/vehicle targets. Each needs a camera target to be watching
        # for, or no snapshot is ever produced for the model to run over.
        "detectors": [
            {
                "value": ZoneDetector.ppe.value,
                "display_name": "PPE",
                "requires_target": DETECTOR_REQUIRES_TARGET[ZoneDetector.ppe],
                "detected_by": "object_detection",
            },
            {
                "value": ZoneDetector.anpr.value,
                "display_name": "Number Plates",
                "requires_target": DETECTOR_REQUIRES_TARGET[ZoneDetector.anpr],
                "detected_by": "object_detection",
            },
        ],
        "kinds": {
            ZoneKind.intrusion.value: {
                # "Regular", not "Intrusion": it's the ordinary zone almost everyone
                # wants, and the camera's own jargon only makes that harder to pick.
                "display_name": "Regular",
                "max_zones": 4,
                "supports_threshold": True,
                "default_notify": DetectionZone.default_notify(ZoneKind.intrusion),
            },
            ZoneKind.excluded_area.value: {
                "display_name": "Excluded Area",
                # Filled in from the camera's own regionEntrance capabilities during
                # setup; this is only the fallback if that read fails. It is a separate
                # budget from intrusion's - a different rule with its own slots.
                "max_zones": DEFAULT_ENTRANCE_MAX_ZONES,
                # regionEntrance has no <timeThreshold> at all, so a dwell control here
                # would be a slider the camera throws away.
                "supports_threshold": False,
                "default_notify": DetectionZone.default_notify(ZoneKind.excluded_area),
                # Entrance needs the target tracked OUTSIDE the region before it crosses
                # in, so a zone hugging the frame edge never fires. Measured, not
                # theorised: 10..990 produced zero events where 250..750 fired reliably
                # (see HikvisionClient._default_region_entrance_body). The frontend should
                # warn when any point is within this many normalized units of the edge.
                "edge_warn": ENTRANCE_EDGE_WARN,
                "edge_warn_normalized": round(ENTRANCE_EDGE_WARN / NORMALIZED_SCREEN, 4),
            },
        },
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

        # What the last real detection was classified as, so a `duration` continuation —
        # which carries no target of its own — can be reported as the same thing.
        self._last_target: MotionDetectEventType = MotionDetectEventType.motion
        self._deterrent_armed: bool = None
        # When the linkage was last written, for the periodic re-assert.
        self._deterrent_asserted_at: datetime = None
        # True once the camera has accepted a native arming schedule *and* that schedule
        # means night and nothing else, in which case the app must stop toggling the
        # linkage itself (the schedule owns it). See assert_arming_schedule.
        self.native_schedule_active: bool = False
        # The windows last written to the camera, and when — so a config edit is noticed
        # without waiting for the periodic re-assert, and the re-assert is cheap.
        self._schedule_windows: list = None
        self._schedule_asserted_at: datetime = None
        # How far the camera's clock is ahead of ours, measured by sync_camera_clock.
        # Applied to the recording search, which runs on the camera's clock.
        self.clock_offset: timedelta = timedelta(0)
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
            f"sensitivity={sensitivity} (per-zone dwell left as configured)"
        )
        try:
            await self.client.set_field_detection(True, RULE_TARGETS, sensitivity)
        except Exception as e:
            log.warning(f"Failed to configure intrusion detection: {e}", exc_info=e)

        # Keep re-alarming while a target stays in the region, and assert it rather than
        # trusting the camera's default. It is the only way the app can tell an intruder is
        # still present, so the night alarm holding the strobe/horn/recording for the length
        # of an event depends on it -- as does the camera's own light/buzzer, which follows
        # the event. Duplicate daytime snapshots are the app's problem, not the camera's.
        interval = await self.client.set_static_target_alarm(True)
        self._warn_if_realarm_outlasts_cooldown(interval)

        await self.disable_unused_rules()

        # How many excluded areas this model can hold. Read once, here, because it's a
        # fixed property of the camera and the zone editor needs it before anyone draws.
        await self._read_entrance_max_zones()

        # Resolve this before arming: the deterrent only adds the `record` linkage
        # when we're actually going to read recordings back off the camera.
        self.event_clip_mode = await self._resolve_event_clip_mode()

        # Before the deterrent: it branches on whether the camera can gate the linkage
        # itself, which is what this decides. Written even with the alarm disabled — the
        # schedule controls when the camera detects at all, not just when it flashes.
        await self.assert_arming_schedule()

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

    @staticmethod
    def _duration_relation(event: dict) -> str:
        """Which event a ``duration`` alert is reporting the continuation of."""
        for key, value in event.items():
            if key.lower().endswith("relationevent") and value:
                return value.strip()
        return ""

    async def on_cam_event(self, event: dict):
        event_type = event.get("eventType", "")
        event_state = event.get("eventState", "")

        # "Still there." See DURATION_EVENT_TYPE: this, not a repeated smart event, is how
        # the camera reports a target that hasn't left, so it's what keeps the alarm, the
        # strobe and the recording running. Forwarded as a continuation, which the app must
        # not treat as a new detection — one arrives every few seconds.
        if event_type == DURATION_EVENT_TYPE and event_state == "active":
            related = self._duration_relation(event)
            if related in SMART_EVENT_TYPES:
                log.debug(f"{related} target still present.")
                await self._invoke(
                    self.on_motion_event_callback,
                    MotionDetectEvent(self._last_target, event, continuation=True),
                )
            return

        if event_type.lower() not in SMART_EVENT_TYPES or event_state != "active":
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

        # Which rule fired, so the app can find the zone the user drew. Paired with the
        # region ids the alert carries, this is what makes a detection attributable to one
        # zone rather than to the camera as a whole.
        rule = rule_for_event_type(event_type)
        motion = MotionDetectEvent(event_type_enum, event, rule=rule)

        log.info(
            f"AcuSense {event_type} target={target or 'unknown'} "
            f"region(s)={motion.region_ids or 'unreported'}"
        )
        self._last_target = event_type_enum
        await self._invoke(self.on_motion_event_callback, motion)

    async def fire_alarm(self):
        """Pulse the external siren/strobe relay (called by the app at night)."""
        port = self.config.alarm.output_port.value
        duration = self.config.alarm.pulse_secs.value
        log.info(f"Firing alarm: pulsing IO output {port} for {duration}s.")
        try:
            await self.client.pulse_io_output(port, duration)
        except Exception as e:
            log.warning(f"Failed to pulse alarm output: {e}", exc_info=e)

    async def sync_camera_clock(self, max_drift_secs: int = MAX_CLOCK_DRIFT_SECS) -> bool:
        """Keep the camera's clock in step with ours, correcting it when it drifts.

        Ours comes from the doovit, which is NTP-synced; the camera's is manual and resets
        to 2019 on a power cut, so this is re-checked from the main loop (every second)
        rather than only at setup. The read is cheap and the write only happens when the
        camera is more than ``max_drift_secs`` out, so the steady state is one GET per loop
        and no writes. Returns whether the clock was (re)set.

        Checking this often also keeps :attr:`clock_offset` fresh, which is what the
        event-clip search is shifted by.

        Also records how far out the camera is (:attr:`clock_offset`), because a few
        seconds of drift is enough to break the event-clip search even though it is
        nowhere near enough to matter to an arming schedule -- see
        :data:`MAX_CLOCK_DRIFT_SECS` and :meth:`_fetch_sd_video`.
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
            offset = (camera_time - now).total_seconds()
            drift = abs(offset)
            if drift <= max_drift_secs:
                # Not worth a write, but the residual still shifts the search window.
                self.clock_offset = timedelta(seconds=offset)
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

        self.clock_offset = timedelta(0)
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

    def _arming_windows(self) -> list:
        """The hour windows the camera must be armed for, as (start, end) pairs.

        The schedule gates the **event**, not just its linkages: an hour outside it
        produces no ``fielddetection`` at all, so the camera classifies nothing and the app
        hears nothing (measured — walking in front of the camera at 12:47 with a night-only
        schedule produced only an unclassified ``VMD`` event). Intrusion is the daytime rule
        as well as the night one, so this is the night window *plus* the motion-snapshot
        window; ``set_event_arming_schedule`` takes their union.
        """
        night = (
            self.config.alarm.night_start_hour.value,
            self.config.alarm.night_end_hour.value,
        )
        day = self.config.motion_snapshot_window
        return [night] + ([day] if day else [])

    async def assert_arming_schedule(self, force: bool = False) -> None:
        """Write the camera's arming schedule, and keep it written.

        Called at setup and then from every main loop. It rewrites when the windows have
        changed — so editing Night Start Hour takes effect without a restart — and
        otherwise every :data:`REASSERT_SECS`, because the schedule lives on the camera
        where a web-UI visit or a factory reset can silently change it and nothing tells us.
        Drift here is invisible in the worst direction: the camera stops reporting events
        for part of the day and the app simply never hears anything.

        This runs whether or not the intruder alarm is enabled. The schedule is not a
        deterrent setting — it decides when the camera detects at all, so a site that only
        wants daytime snapshots needs it just as much as one that wants a siren.
        """
        windows = self._arming_windows()
        now = datetime.now(tz=timezone.utc)
        stale = (
            self._schedule_asserted_at is None
            or (now - self._schedule_asserted_at).total_seconds() >= REASSERT_SECS
        )
        changed = windows != self._schedule_windows
        if not (force or changed or stale):
            return

        try:
            accepted = await self.client.set_event_arming_schedule(windows)
        except Exception as e:
            log.warning(f"Failed to write native arming schedule: {e}", exc_info=e)
            accepted = False

        # Remembered whether or not the camera took it. Keeping this None on rejection
        # made `changed` true on every pass, so a camera that refuses a schedule got a PUT
        # every second, forever — a retry storm that looks like the app pegging a CPU.
        # Rejection now retries on the same REASSERT_SECS cadence as everything else.
        self._schedule_asserted_at = now
        self._schedule_windows = windows

        # Whether the *camera* can gate the deterrent for us, which is a stronger claim
        # than "the camera took a schedule". It can only do that when the schedule means
        # night and nothing else; once it also covers the day, a permanently-armed linkage
        # would sound the siren at a delivery driver at midday. So when the schedule is
        # wider than the night, the app owns arming — the cost of intrusion covering the
        # day, and the reason arm_night_deterrent runs every main loop.
        day_covered = len(windows) > 1
        was_native = self.native_schedule_active
        self.native_schedule_active = accepted and not day_covered

        if changed and not stale:
            log.info(f"Arming windows changed; rewrote the camera's schedule as {windows}.")
        if accepted and day_covered and not was_native:
            log.info(
                "Arming schedule covers the daytime motion window as well as the night, so "
                "the app arms the deterrent at the night boundary rather than the camera. "
                "NOTE: that does not survive doover being offline across dusk."
            )
        elif not accepted:
            log.info(
                "Camera did not accept a native arming schedule; it stays armed whenever "
                "it was, and the app owns the deterrent."
            )

    async def setup_night_deterrent(self):
        """Set up the flash / siren active response on the intrusion event.

        Where the camera's schedule means night and nothing else, the linkage is left
        permanently armed and the camera gates it — so the deterrent fires even while doover
        is offline. Otherwise the app toggles the linkage at the night boundary
        (:meth:`arm_night_deterrent`, called each main loop, re-asserting every
        :data:`REASSERT_SECS`). :meth:`assert_arming_schedule` decides which, so it must
        have run first.
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

        if self.native_schedule_active:
            # The schedule gates the linkage, so leave it permanently armed.
            await self._set_linkage(True)
            log.info(
                f"Night deterrent ({'+'.join(methods)}) armed via the camera's "
                f"native arming schedule."
            )
        else:
            await self.arm_night_deterrent(self.config.is_night())

    @classmethod
    def _clamp_threshold(cls, secs: int) -> int:
        """A zone's dwell time, defaulted and held inside what the camera accepts.

        Clamped rather than rejected, like zone coordinates: a frontend sending 90 on a
        camera that stops at 60 shouldn't lose the whole write. ``None`` means the zone
        didn't specify one, which is the default rather than "leave it alone" — there is no
        per-slot value to preserve once the region body is rebuilt.
        """
        if secs is None:
            return INTRUSION_DWELL_SECS
        return max(
            cls.ZONE_CAPABILITIES["threshold_min"],
            min(cls.ZONE_CAPABILITIES["threshold_max"], int(secs)),
        )

    def _warn_if_realarm_outlasts_cooldown(self, interval: int) -> None:
        """Warn when the camera re-alarms slower than the app gives up waiting.

        The night alarm treats "no detection for ``event_clip_cooldown`` seconds" as the
        intruder having left. If the camera only re-reports a stationary target every
        ``targetAlarmInterval`` seconds and that is the longer of the two, then someone
        standing still ends the alarm on a timer while they're still standing there — which
        looks like the alarm cutting out for no reason.
        """
        if interval is None:
            return
        cooldown = self.config.alarm.event_clip_cooldown.value
        if interval >= cooldown:
            log.warning(
                f"The camera re-reports a stationary target every {interval}s but the app "
                f"treats {cooldown}s of quiet as the intruder having left, so a motionless "
                f"intruder will end the alarm early. Raise Event Video Cooldown above "
                f"{interval}s."
            )

    async def disable_unused_rules(self):
        """Switch off the smart rules intrusion makes redundant.

        Each rule left on is a second event for the same person walking through, and so a
        second snapshot, upload and inference run. Their polygons are preserved (see
        ``disable_smart_rule``), so nothing a user drew is lost by turning one off, and
        their events are ignored regardless (see :data:`SMART_EVENT_TYPES`) -- this is
        about not paying for them, not about correctness.
        """
        for rule in UNUSED_SMART_RULES:
            if not await self.client.disable_smart_rule(rule):
                log.info(
                    f"Couldn't confirm {rule} is disabled. Its events are ignored either "
                    f"way, but if it is on the camera is doing work for nothing."
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
            >= REASSERT_SECS
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

        The window is asked for in **the camera's** clock, not ours. It stamps segments and
        answers searches on its own manual clock, so a few seconds of drift against a ~25s
        window means every search misses — the footage is on the card, just not where we
        looked (measured: 44s behind, and not one clip was ever found). So the offset
        measured by :meth:`sync_camera_clock` is applied, plus a margin either side.
        """
        offset = self.clock_offset
        matches = await self.client.search_recordings(
            start + offset - EVENT_CLIP_SEARCH_MARGIN,
            end + offset + EVENT_CLIP_SEARCH_MARGIN,
        )
        if abs(offset.total_seconds()) >= 1:
            log.info(
                f"Searched the card shifted by {offset.total_seconds():.0f}s for the "
                f"camera's clock."
            )

        # Widening the window can pull in a neighbouring event's segment, so prefer the one
        # that overlaps the event itself most rather than whichever came back first.
        for match in sorted(
            matches,
            key=lambda m: self._overlap_secs(m, start + offset, end + offset),
            reverse=True,
        ):
            uri = match.get("mediaSegmentDescriptor.playbackURI")
            if not uri:
                continue

            data = await self.client.download_recording(uri)
            if not data:
                continue

            log.info(f"Fetched {len(data)} bytes of on-card recording; remuxing.")
            return await self.remux_to_mp4(data, "event")

        log.info(
            f"Camera reported no recording between {start.isoformat()} and "
            f"{end.isoformat()} (camera clock offset {offset.total_seconds():.0f}s)."
        )
        return None

    @staticmethod
    def _overlap_secs(match: dict, start: datetime, end: datetime) -> float:
        """How many seconds of ``match`` fall inside the event window. 0 if unknown."""
        try:
            seg_start = datetime.fromisoformat(
                match["timeSpan.startTime"].replace("Z", "+00:00")
            )
            seg_end = datetime.fromisoformat(
                match["timeSpan.endTime"].replace("Z", "+00:00")
            )
        except (KeyError, ValueError, AttributeError):
            return 0.0
        latest_start = max(seg_start, start)
        earliest_end = min(seg_end, end)
        return max(0.0, (earliest_end - latest_start).total_seconds())

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

    def max_zones_for(self, kind: ZoneKind) -> int | None:
        """How many zones of ``kind`` this camera can hold.

        None means "no camera limit" — the app-only kinds, which no rule has to store.
        """
        return self.ZONE_CAPABILITIES["kinds"][kind.value]["max_zones"]

    async def _read_entrance_max_zones(self) -> None:
        """Ask the camera how many entrance slots it really has.

        Only worth doing once, at setup: it's a fixed property of the model. Left at
        :data:`DEFAULT_ENTRANCE_MAX_ZONES` if the camera won't say, which is a guess but a
        conservative one — guessing high would get every zone write rejected outright,
        guessing low only limits how many excluded areas can be drawn.
        """
        found = await self.client.get_rule_max_regions(RULE_REGION_ENTRANCE)
        if not found:
            return
        current = self.max_zones_for(ZoneKind.excluded_area)
        if found != current:
            log.info(
                f"Camera advertises {found} region-entrance slot(s) (assumed {current})."
            )
        # Per-instance, so one model's answer can't leak onto another camera's class.
        self.ZONE_CAPABILITIES = {
            **self.ZONE_CAPABILITIES,
            "kinds": {
                **self.ZONE_CAPABILITIES["kinds"],
                ZoneKind.excluded_area.value: {
                    **self.ZONE_CAPABILITIES["kinds"][ZoneKind.excluded_area.value],
                    "max_zones": found,
                },
            },
        }

    async def get_detection_zones(self) -> list:
        """Every zone this camera holds, read back from the rules that store them.

        Only the camera-backed kinds appear here. The ``ppe``/``anpr`` zones live nowhere
        on the camera, so they can't be recovered from it — the app layer merges those in
        from its own record (see ``CameraApplication.merge_zone_records``).
        """
        zones = []
        for kind, rule, reader in (
            (
                ZoneKind.intrusion,
                RULE_FIELD_DETECTION,
                self.client.get_field_detection_regions,
            ),
            (
                ZoneKind.excluded_area,
                RULE_REGION_ENTRANCE,
                self.client.get_region_entrance_regions,
            ),
        ):
            # A disabled rule's polygons are not zones. They are leftovers — the camera
            # keeps a region's points when the rule is switched off, so an entrance rule
            # that was written and later disabled still reports its old polygon.
            #
            # Reporting one would be worse than cosmetic: the editor would show an
            # excluded area that detects nothing, and saving from that state would see one
            # excluded zone and re-enable the rule, arming a zone nobody drew.
            #
            # Only skips on a definite False. Unknown (None) reports the regions, because a
            # rule we couldn't read is not a rule we know to be off, and hiding a real
            # zone is the worse mistake.
            try:
                if await self.client.get_smart_rule_enabled(rule) is False:
                    log.debug(f"{rule} is disabled; its regions are not live zones.")
                    continue
            except Exception as e:
                log.info(f"Couldn't check whether {rule} is enabled: {e}")

            try:
                cfg = await reader()
            except Exception as e:
                log.warning(f"Couldn't read {kind.value} zones: {e}", exc_info=e)
                continue

            for region in cfg:
                points = [self._from_native(x, y) for x, y in region["points"]]
                if not points:
                    continue  # an unconfigured slot - the camera keeps all of them
                zones.append(
                    DetectionZone(
                        id=region["id"],
                        points=points,
                        # Entrance reports no per-region enabled; a region the camera
                        # holds a polygon for is live either way.
                        enabled=region.get("enabled", True),
                        targets=[
                            HIK_TO_TARGET[t]
                            for t in region["targets"]
                            if t in HIK_TO_TARGET
                        ],
                        sensitivity=region["sensitivity"],
                        threshold_secs=region.get("time_threshold"),
                        kind=kind,
                        # Notify isn't a camera concept, so the read-back can't know it.
                        # The app layer overlays the real value from its own record; this
                        # is what a zone the app has never seen written would default to.
                        notify=None,
                    )
                )
        return zones

    def _validate_points(self, zone) -> None:
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

    def _to_region(self, index: int, zone) -> dict:
        """One zone as a native region body, addressed by slot.

        ``time_threshold`` is always included even for entrance, which has no such field:
        the entrance body builder simply doesn't emit it (see
        ``HikvisionClient._region_entrance_region``), so one region shape serves both
        rules and the caller doesn't have to branch.
        """
        targets = [TARGET_TO_HIK[t] for t in zone.targets if t in TARGET_TO_HIK]
        return {
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
            "time_threshold": self._clamp_threshold(zone.threshold_secs),
        }

    def _blank_region(self, index: int) -> dict:
        return {
            "id": index,
            "points": [],
            "targets": list(RULE_TARGETS),
            "sensitivity": self.config.sensitivity.value,
            "time_threshold": INTRUSION_DWELL_SECS,
        }

    def _regions_for(self, zones: list, max_zones: int) -> list:
        """Zone list -> the full set of region slots to write, blanks included.

        The camera keeps every region slot it has, so one simply left out of the body
        holds on to its old polygon — which meant zones could be edited but never
        deleted. Every unused slot is explicitly blanked so dropping a zone removes it.
        """
        regions = [self._to_region(i, z) for i, z in enumerate(zones, start=1)]
        regions.extend(
            self._blank_region(i) for i in range(len(regions) + 1, max_zones + 1)
        )
        return regions

    def _warn_on_edge_zones(self, zones: list) -> None:
        """Warn about an excluded area drawn too close to the frame edge.

        Entrance only fires for a target it tracked *outside* the region first, so a zone
        hugging the edge leaves nowhere to be outside and never fires at all. Measured on
        a real camera: a region at 10..990 produced zero events where 250..750 fired
        reliably.

        A warning, not a rejection — the user may have drawn it deliberately against a
        wall or a gate where nothing can approach from that side, and silently moving
        somebody's polygon would be worse than a log line.
        """
        limit = ENTRANCE_EDGE_WARN / NORMALIZED_SCREEN
        for zone in zones:
            if any(
                min(x, y) <= limit or max(x, y) >= 1.0 - limit for x, y in zone.points
            ):
                log.warning(
                    f"Excluded area '{zone.name or zone.id}' comes within "
                    f"{limit:.0%} of the frame edge. Region entrance needs to see a "
                    f"target outside the zone before it crosses in, so a zone this close "
                    f"to the edge may never fire. Pull it in from the edge if it doesn't "
                    f"trigger."
                )

    async def set_detection_zones(self, zones: list) -> None:
        """Write each zone to whichever rule owns its kind.

        Both camera rules are always written, including when a kind has no zones — that
        write is what blanks the slots of zones that were deleted. The app-only kinds
        (``ppe``/``anpr``) are validated and returned untouched for the app layer to store;
        nothing is sent to the camera for them.
        """
        by_kind = {}
        for zone in zones:
            by_kind.setdefault(zone.kind, []).append(zone)

        for kind, kind_zones in by_kind.items():
            limit = self.max_zones_for(kind)
            if limit is not None and len(kind_zones) > limit:
                raise ValueError(
                    f"This camera supports at most {limit} "
                    f"{kind.value.replace('_', ' ')} zone(s), got {len(kind_zones)}"
                )
            for zone in kind_zones:
                self._validate_points(zone)

        intrusion = by_kind.get(ZoneKind.intrusion, [])
        excluded = by_kind.get(ZoneKind.excluded_area, [])
        self._warn_on_edge_zones(excluded)

        log.info(
            f"Writing zones to the camera: {len(intrusion)} intrusion, "
            f"{len(excluded)} excluded area. "
            f"({len(zones) - len(intrusion) - len(excluded)} app-only zone(s) are not "
            f"written to the camera.)"
        )

        await self.client.set_field_detection_regions(
            self._regions_for(intrusion, self.max_zones_for(ZoneKind.intrusion))
        )
        await self._write_excluded_zones(excluded)

        # The region write rebuilds the rule body, which drops any rule-level field it
        # doesn't know about — including the static-target re-alarm switch. Re-assert it, or
        # editing a zone quietly costs the night alarm its "intruder still present" signal.
        await self.client.set_static_target_alarm(True)

    async def _write_excluded_zones(self, excluded: list) -> None:
        """Write the excluded-area zones, and turn the entrance rule on or off to match.

        The rule is enabled only while there is at least one excluded area, because that
        is the whole justification for having a second rule running: with none, entrance
        would fire alongside intrusion for the same person and cost a duplicate event,
        snapshot and inference run — which is exactly why it used to be disabled outright.

        Slots are blanked before the rule is switched off rather than instead of it. Just
        disabling would leave the polygons on the camera, so re-adding any excluded area
        later would silently bring the old ones back with it.
        """
        max_zones = self.max_zones_for(ZoneKind.excluded_area)
        await self.client.set_region_entrance_regions(
            self._regions_for(excluded, max_zones)
        )

        if excluded:
            try:
                await self.client.set_region_entrance_enabled(True)
            except Exception as e:
                log.warning(
                    f"Wrote {len(excluded)} excluded area(s) but couldn't enable the "
                    f"region-entrance rule: {e}. They will not fire until it is on.",
                    exc_info=e,
                )
            return

        if not await self.client.disable_smart_rule(RULE_REGION_ENTRANCE):
            log.info(
                "No excluded areas left, but couldn't confirm region entrance is "
                "disabled. Its slots are blank so it has nothing to fire on."
            )

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
