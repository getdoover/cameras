import asyncio
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import aiohttp
from pydoover import rpc, ui
from pydoover.docker import Application
from pydoover.models import (
    AggregateUpdateEvent,
    EventSubscription,
    File,
    NotificationSeverity,
)

from .app_config import CameraConfig, CameraType
from .app_tags import CameraTags
from .app_ui import CameraUI
from .engines import DahuaPTZCamera
from .engines.base import Capture, THUMBNAIL_SUFFIX
from .engines.dahua_base import DahuaCameraBase
from .engines.dahua_fixed import DahuaFixedCamera
from .engines.generic import GenericRTSPCamera
from .engines.bosch_ptz import BoschPTZCamera
from .engines.hikvision_thermal import HikVisionThermal
from .engines.hikvision_anpr import HikvisionANPRCamera
from .engines.hikvision_acusense import HikvisionAcuSenseCamera
from .engines.hikvision_deepinview import HikvisionDeepinViewCamera
from .events import (
    ANPREvent,
    DETECTOR_REQUIRES_TARGET,
    DetectionZone,
    DetectionZonesPayload,
    MotionDetectEvent,
    MotionDetectEventType,
    PPEEvent,
    SDPOfferPayload,
    TargetBox,
    ZoneKind,
    event_image,
    CAMERA_CONTROL_CHANNEL,
    SET_ZONES_CMD,
)
from .power_management import CameraPowerManagement

log = logging.getLogger()

GET_NOW_CMD_NAME = "camera_snapshots"
LAST_SNAPSHOT_CMD_NAME = "last_cam_snapshot"
UI_CONNECT_POWERON_TIMEOUT_SEC = 60 * 15  # 15min
# How far before an intruder event to search the camera's SD card, to cover the
# camera's pre-record buffer.
EVENT_CLIP_LOOKBACK_SEC = 10

# External horn burst pattern while an intruder is present: sound it for HORN_ON_SEC
# out of every HORN_PERIOD_SEC. Deliberately not configurable — it's a fixed cadence.
# (The strobe light, by contrast, is held on continuously for the whole event.)
HORN_ON_SEC = 3
HORN_PERIOD_SEC = 10
# How often the external-alarm loop re-publishes its hold and reconciles the outputs.
ALARM_TICK_SEC = 1
# Prefix for the cross-app "hold until" tag that coordinates a shared strobe/horn
# output between camera apps. Keyed by pin (like camera power's camera_power_<pin>),
# read/written with app_key=None so every camera app on the Doovit sees the same value.
ALARM_HOLD_TAG_PREFIX = "camera_alarm_output_"

# Why a snapshot/video was captured, attached to the message payload so a gallery can
# label or filter it. The detection reasons mirror the `camera_event` channel's
# `kind`, so the media and the automation event agree on what happened.
REASON_SCHEDULE = "schedule"  # the periodic snapshot timer
REASON_MANUAL = "manual"  # somebody asked for one
REASON_INTRUDER = "intruder"  # a detection inside the night alarm window
SNAPSHOT_REASONS = (
    REASON_SCHEDULE,
    REASON_MANUAL,
    REASON_INTRUDER,
    "person",
    "vehicle",
    "anpr",
    "ppe",
)

# The camera's own frame of an event, uploaded beside the snapshot we fetch. Named so it
# reads as a distinct view in `media` rather than looking like the main capture.
EVENT_FRAME_NAME = "event-frame"

# The reasons that count as a "motion snapshot" — a picture taken because the camera
# classified something, as opposed to the schedule, a manual request, or the night
# intruder alarm. These are what the motion-snapshot window and its object-detection
# flag apply to.
MOTION_SNAPSHOT_REASONS = ("person", "vehicle")


class CameraApplication(Application):
    config: CameraConfig
    tags: CameraTags
    ui: CameraUI

    config_cls = CameraConfig
    tags_cls = CameraTags
    ui_cls = CameraUI

    async def setup(self):
        self.engine = None

        self.power_management = CameraPowerManagement(self)

        self.app_display_name = self.app_display_name or "Camera"

        # self.ui = CameraUI(self.config, self.app_key, self.app_display_name)
        # self.ui_manager.add_children(*self.ui.fetch())
        # self.ui_manager._add_interaction(self.ui.human_detection)
        # self.ui_manager._add_interaction(self.ui.vehicle_detection)

        # we don't want a submodule view for cameras since the UI
        # renders it as a submodule anyway (and we'd end up with double submodules).
        # self.ui_manager.set_variant(ui.ApplicationVariant.stacked)
        # self.ui_manager.set_display_name(self.app_display_name)

        self.snapshot_running = None
        self._shutdown_at = None

        # When a motion snapshot was last taken, per zone, for the capture cooldown.
        # Keyed by (rule, slot id); `_UNZONED_COOLDOWN_KEY` holds the shared timer for
        # detections that can't be attributed to a zone. See claim_motion_snapshot.
        self._last_motion_snapshot_at = {}

        # Event-video state: recording runs for as long as the intruder keeps
        # re-triggering, and stops once the cooldown lapses with no new detection.
        self._intruder_clip_task = None
        self._last_intruder_event_at = None

        # Background task driving the external strobe/horn on the Doovit outputs for as
        # long as an intruder is present. Re-triggers extend it rather than restart it.
        self._external_alarm_task = None

        # In-flight pulse of the camera's own alarm relay (see start_alarm_pulse).
        self._alarm_pulse_task = None

        # the below is probably a "fix in doover 2.0" problem to have some better / more native
        # camera feels
        # self.ui_manager._transform_interaction_name = self._transform_interaction_name
        # self.ui_manager._add_interaction(ui.SlimCommand(GET_NOW_CMD_NAME))
        # self.ui_manager._add_interaction(ui.SlimCommand(LAST_SNAPSHOT_CMD_NAME))

        await self.subscribe("doover_ui_fastmode", EventSubscription.aggregate_update)

        # self.control_task = asyncio.create_task(self.handle_control_messages())
        # self.device_agent.subscribe_to_channel_messages("camera_control", self.on_control_message)

        match CameraType(self.config.type.value):
            case CameraType.dahua_ptz:
                self.engine = DahuaPTZCamera(
                    self.config,
                    self.on_motion_event_callback,
                    self.sync_presets,
                    self.clear_preset,
                )
            case CameraType.dahua_fixed:
                self.engine = DahuaFixedCamera(
                    self.config,
                    self.on_motion_event_callback,
                    self.sync_presets,
                    self.clear_preset,
                )
            case CameraType.dahua_generic:
                self.engine = DahuaCameraBase(
                    self.config,
                    self.on_motion_event_callback,
                    self.sync_presets,
                    self.clear_preset,
                )
            case CameraType.unifi_generic:
                self.engine = GenericRTSPCamera(self.config)
            case CameraType.generic_ip:
                self.engine = GenericRTSPCamera(self.config)
            case CameraType.bosch_ptz:
                self.engine = BoschPTZCamera(
                    self.config,
                    self.on_motion_event_callback,
                    self.sync_presets,
                    self.clear_preset,
                )
            case CameraType.hikvision_thermal:
                self.engine = HikVisionThermal(self.config)
            case CameraType.hikvision_anpr:
                self.engine = HikvisionANPRCamera(
                    self.config,
                    self.on_motion_event_callback,
                    self.on_anpr_event_callback,
                    self.sync_presets,
                    self.clear_preset,
                )
            case CameraType.hikvision_acusense:
                self.engine = HikvisionAcuSenseCamera(
                    self.config,
                    self.on_motion_event_callback,
                    self.sync_presets,
                    self.clear_preset,
                )
            case CameraType.hikvision_deepinview:
                self.engine = HikvisionDeepinViewCamera(
                    self.config,
                    self.on_motion_event_callback,
                    self.on_anpr_event_callback,
                    self.on_ppe_event_callback,
                    self.sync_presets,
                    self.clear_preset,
                )
            case _:
                raise ValueError(f"Unknown camera type: {self.config.type.value}")

        self.rpc.register_handlers(self.engine)

        await self.engine.setup()
        await self.setup_rtsp_server()
        await self.sync_presets()
        # log_update=False: starting up isn't somebody changing the zones, so it
        # shouldn't land in the audit log.
        await self.publish_detection_zones(log_update=False)

    async def close(self):
        if self._intruder_clip_task:
            self._intruder_clip_task.cancel()
        if self._external_alarm_task:
            self._external_alarm_task.cancel()
        if self._alarm_pulse_task:
            self._alarm_pulse_task.cancel()
        if self.engine:
            await self.engine.close()

    async def on_aggregate_update(self, event: AggregateUpdateEvent):
        if event.channel.name != "doover_ui_fastmode":
            # we only care about fastmode updates
            return

        await asyncio.sleep(0.1)
        if self.tag_manager.is_being_observed:
            log.info("Enabling power for user observation.")
            await self.power_management.acquire_for(
                timedelta(seconds=UI_CONNECT_POWERON_TIMEOUT_SEC)
            )

    async def on_shutdown_at(self, shutdown_at: datetime):
        self._shutdown_at = shutdown_at
        await self.power_management.release()

    @rpc.handler("power_on", parser=bool, channel=CAMERA_CONTROL_CHANNEL)
    async def power_on(self, ctx, payload):
        # just do this here again, no harm in duplicating this...
        await self.setup_rtsp_server()
        await self.power_management.acquire()

    @rpc.handler(
        "accept_sdp", parser=SDPOfferPayload.from_dict, channel=CAMERA_CONTROL_CHANNEL
    )
    async def accept_sdp(self, ctx, payload: SDPOfferPayload):
        await self.setup_rtsp_server()
        await self.power_management.acquire()
        await self.accept_sdp_offer(self.app_key, payload.stream_name, payload.value)
        log.info("Finished accepting SDP offer and published.")

    async def main_loop(self):
        # The camera's clock is manual and resets to 2019 on a power cut, taking its
        # arming schedule and recording search down with it — so re-check it here
        # rather than only at setup. No-ops unless it has actually drifted.
        if isinstance(self.engine, HikvisionAcuSenseCamera):
            await self.engine.sync_camera_clock()

        await self.update_alarm_schedule()

        if self.check_snapshot_can_run():
            log.info("Running snapshot from main loop.")
            await self.lock_snapshot_and_run()

    async def update_alarm_schedule(self):
        """Keep the camera's arming schedule and night alarm in step with the config.

        Two separate things, on the same 10-minute correction cadence, both idempotent so
        running every loop is cheap:

        * **The arming schedule** — which hours the camera detects *at all* on this
          firmware, so it is re-asserted whether or not there's an alarm, and rewritten
          immediately if the night or motion-snapshot window was edited. This is also what
          fixes itself when the device comes back from being offline: setup writes it, and
          the loop keeps it written.
        * **The deterrent linkage** — armed at dusk and disarmed at dawn, but only when the
          app owns it; when the camera's schedule means night and nothing else it gates the
          linkage itself and ``arm_night_deterrent`` no-ops. The relay pulse is per-event
          (see on_motion_event_callback).
        """
        if isinstance(self.engine, HikvisionAcuSenseCamera):
            await self.engine.assert_arming_schedule()

        if not self.config.alarm.intruder_alarm_enabled.value:
            return
        night = self.config.is_night()
        if isinstance(self.engine, HikvisionANPRCamera):
            await self.engine.arm_night_alarm(night)
        elif isinstance(self.engine, HikvisionAcuSenseCamera):
            await self.engine.arm_night_deterrent(night)

    def check_snapshot_can_run(self):
        if self.config.snapshot.enabled.value is False:
            return False

        if self.snapshot_running:
            return False

        if (
            datetime.now(tz=timezone.utc)
            - datetime.fromtimestamp(self.tags.last_cam_snapshot.value, tz=timezone.utc)
        ) > timedelta(seconds=self.config.snapshot.period.value):
            return True

        return False

    async def lock_snapshot_and_run(
        self,
        reason: str = REASON_SCHEDULE,
        boxes: list = None,
        event_frame: bytes = None,
    ):
        self.snapshot_running = True
        try:
            await self.run_snapshot(reason, boxes=boxes, event_frame=event_frame)
        except Exception as e:
            log.error(f"Error getting snapshot: {str(e)}", exc_info=e)
        self.snapshot_running = False

        now = datetime.now()
        await self.tags.last_cam_snapshot.set(now.timestamp())

        # might as well update presets when we're fetching snapshots...
        await self.sync_presets()

    async def run_snapshot(
        self,
        reason: str = REASON_SCHEDULE,
        retries: int = 3,
        ping_timeout: int = 20,
        boxes: list = None,
        event_frame: bytes = None,
    ):
        await self.power_management.acquire()

        # await a successful ping to the camera
        # generic ip cameras will use an icmp ping, dahua cameras can use an http server ping
        if self.config.power.enabled.value:
            wake_delay = self.config.power.wake_delay.value
        else:
            wake_delay = 0

        if not await self.engine.ping(ping_timeout + wake_delay):
            log.info("Failed to ping camera, skipping snapshot.")
            # ...but if the camera handed us a frame with the event, that frame is still
            # worth having — arguably more so, since the camera has stopped answering.
            if event_frame:
                log.info("Publishing the camera's own event frame anyway.")
                await self.upload_media(
                    [self.build_event_frame(event_frame)], reason, boxes
                )
            return None

        # at this point, dahua cameras will be ready to take a snapshot, but unifi / generic ones
        # will potentially need to wait a bit longer - they may be 'pingable' but may not be 'ready'.
        log.info("Ping succeeded, getting snapshot.")

        ## attempt to take a snapshot
        error_count = 0
        files = None
        while error_count < retries:
            try:
                files = await self.engine.get_snapshot()
            except Exception as e:
                log.info(f"Failed to get snapshot: {e}, retrying...")
                await asyncio.sleep(1)
                error_count += 1
                continue

            if files:
                # we've got the camera, keep moving...
                break
            else:
                log.info("Failed to get snapshot, retrying...")
                await asyncio.sleep(1)
                error_count += 1

        if files is None:
            log.info("Failed to get snapshot after retries")
            if event_frame:
                log.info("Publishing the camera's own event frame anyway.")
                await self.upload_media(
                    [self.build_event_frame(event_frame)], reason, boxes
                )
            return False

        # The camera's frame goes up beside ours rather than instead of it: ours is clean
        # and full resolution, theirs is the moment the target was actually in the zone.
        if event_frame:
            files = [*files, self.build_event_frame(event_frame)]

        try:
            # Every capture goes up, not just the first — a PTZ camera returns one
            # per preset, and a thermal camera a visible and a thermal view.
            await self.upload_media(files, reason, boxes)
        except Exception as e:
            log.warning(f"Failed to publish snapshot: {e}", exc_info=e)
        else:
            await asyncio.sleep(2)

    @staticmethod
    def build_event_frame(image: bytes) -> Capture:
        """Wrap the camera's own event JPEG as a capture, so it uploads like any other view.

        No thumbnail: it goes up beside a snapshot that has one, and these frames are
        already small enough (200-420KB) that a preview would cost more than it saves.
        """
        return Capture(
            EVENT_FRAME_NAME,
            File(
                filename=f"{EVENT_FRAME_NAME}.jpg",
                data=image,
                size=len(image),
                content_type="image/jpeg",
            ),
        )

    async def setup_rtsp_server(self):
        if not self.config.rtsp_server.enabled.value:
            log.info("RTSP server disabled in config. Ignoring...")
            return

        base = self.config.rtsp_server.address.value
        auth = aiohttp.BasicAuth("demo", "demo")
        async with aiohttp.request("GET", f"{base}/streams", auth=auth) as resp:
            data = await resp.json()

        await self.setup_rtsp_stream(self.app_key, self.config.rtsp_uri, data)
        if self.config.thermal_rtsp_uri:
            await self.setup_rtsp_stream(
                f"{self.app_key}_thermal", self.config.thermal_rtsp_uri, data
            )

    async def setup_rtsp_stream(self, stream_name, rtsp_uri, streams_data):
        base = self.config.rtsp_server.address.value
        auth = aiohttp.BasicAuth("demo", "demo")

        try:
            configured_url = streams_data["payload"][stream_name]["channels"]["0"][
                "url"
            ]
        except (KeyError, TypeError):
            method = "add"  # doesn't exist
        else:
            if configured_url == rtsp_uri:
                log.info("RTSP server stream already exists. Skipping...")
                return  # already exists

            method = "edit"

        body = {
            "name": stream_name,
            "channels": {
                "0": {
                    "name": stream_name,
                    "url": rtsp_uri,
                    "on_demand": True,
                    "audio": True,
                    "debug": False,
                }
            },
        }
        log.info("Creating rtsp server stream...")
        async with aiohttp.request(
            "POST",
            f"{base}/stream/{quote(stream_name)}/{method}",
            json=body,
            auth=auth,
        ) as resp:
            assert resp.status == 200

    async def accept_sdp_offer(self, camera_name, stream_name, offer: str):
        base = self.config.rtsp_server.address.value
        auth = aiohttp.BasicAuth("demo", "demo")

        credentials = await self.device_agent.fetch_turn_token()
        body = {
            "ice_servers": credentials.uris,
            "ice_username": credentials.username,
            "ice_credential": credentials.credential,
        }
        if offer:
            body["data"] = offer

        # get SDP and update camera channel with data
        async with aiohttp.request(
            "POST",
            f"{base}/stream/{quote(stream_name)}/channel/0/webrtc?uuid={quote(stream_name)}&channel=0",
            json=body,
            auth=auth,
            # headers={"Content-Type": "application/json"}
        ) as resp:
            if resp.status != 200:
                data = await resp.json()
                log.info(f"SDP Failed: {data['payload']}")
            else:
                answer = await resp.text()
                # answer is the base64-encoded SDP answer
                await self.device_agent.update_channel_aggregate(
                    camera_name, {"sdp": answer}, max_age_secs=-1
                )

    async def upload_media(self, captures: list, reason: str, boxes: list = None):
        """Publish captures, each with its thumbnail, plus a payload describing them.

        A single message carries every view captured in one go — a PTZ camera
        contributes one per preset, a thermal camera one visible and one thermal — so
        ``media`` is always a list, even for the one-view case::

            {"reason": "schedule", "night": true, "media": [
                {"name": "Preset1", "file": "Preset1.jpg",
                 "thumbnail": "Preset1-thumbnail.jpg"},
                {"name": "Preset2", "file": "Preset2.jpg",
                 "thumbnail": "Preset2-thumbnail.jpg"}]}

        The payload names which attachment is which so a gallery doesn't have to
        infer it from filenames, and says why the capture happened. ``reason`` is one
        of :data:`SNAPSHOT_REASONS` and matches the ``kind`` of the matching
        ``camera_event`` message.

        ``night`` is only present when the camera states it outright; when it's
        absent the image itself can be inspected (an IR frame is monochrome), which
        is left to the consumer rather than paying for it on the device.

        ``boxes`` are where the camera says it saw the targets that triggered the
        capture (:class:`TargetBox`), published as ``detections``. Absent — not empty —
        when the event carried none, so a consumer can tell "the camera reported no
        boxes" from "this camera doesn't report boxes at all".
        """
        files, media = [], []
        for capture in captures:
            files.extend(capture.files())
            entry = {"name": capture.name, "file": capture.media.filename}
            if capture.thumbnail:
                entry["thumbnail"] = capture.thumbnail.filename
            media.append(entry)

        payload = {"reason": reason, "media": media}

        # Marks the frame as wanted by the Object Detection app. Set explicitly rather
        # than left to that app to infer from `reason`, so which cameras get analysed
        # is decided per-camera here — the detection app can watch a camera for plates
        # without every one of its motion snapshots being run through the models.
        if reason in MOTION_SNAPSHOT_REASONS:
            payload["object_detection"] = self.config.motion_snapshot_object_detection

        # What the camera itself localised, for whoever analyses the frame: a region to
        # crop a plate read to, and something concrete to deduplicate consecutive events
        # on. Advisory — a consumer is free to ignore it and run over the whole frame.
        if boxes:
            payload["detections"] = [b.to_dict() for b in boxes]

        # The zones the object detection app acts on, carried with the frame they apply to
        # rather than fetched separately — they can't drift out of step that way, and that
        # app needs no extra subscription to get them. Same reasoning as
        # `object_detection` above: this app is the authority on what its own frames are
        # for.
        #
        # Only sent when there are some. An absent key means "this camera has no opinion",
        # which that app must treat as "analyse the whole frame" — not as "no zones, so
        # find nothing". See common.zones.zones_for_detector.
        #
        # Never allowed to fail the upload: these zones are advisory metadata about a frame,
        # and losing the picture over them would be a straight downgrade. Worst case the
        # frame is analysed unfiltered, which is what happens on every camera with no zones
        # anyway.
        try:
            zones = self.detector_zones()
        except Exception as e:
            log.warning(f"Couldn't attach detection zones to the snapshot: {e}")
            zones = []
        if zones:
            payload["detection_zones"] = zones

        try:
            night = await self.engine.detect_night()
        except Exception as e:
            log.warning(f"Failed to read day/night state: {e}", exc_info=e)
            night = None
        if night is not None:
            payload["night"] = night

        log.info(f"Publishing {len(media)} capture(s) as {len(files)} file(s).")
        await self.device_agent.create_message(self.app_key, payload, files)

    async def publish_camera_event(self, kind: str, **extra):
        """Publish a structured event to the ``camera_event`` channel.

        This is the hook doover automations subscribe to (publish onward, etc.).
        ``kind`` is the event class — "intruder", "person", "vehicle", "anpr" — and
        callers add whatever detail is relevant to that kind.
        """
        payload = {
            "kind": kind,
            "app_key": self.app_key,
            "display_name": self.app_display_name,
            **extra,
        }
        try:
            await self.create_message("camera_event", payload)
        except Exception as e:
            log.warning(f"Failed to publish camera_event: {e}", exc_info=e)

    async def watch_for_event_end(self, stop: asyncio.Event):
        """Set ``stop`` once the intruder event has gone quiet.

        Each detection pushes ``_last_intruder_event_at`` forward, so an intruder who
        keeps setting the camera off keeps the recording running.
        """
        cooldown = self.config.alarm.event_clip_cooldown.value
        while True:
            last = self._last_intruder_event_at
            if (
                last is None
                or (datetime.now(tz=timezone.utc) - last).total_seconds() > cooldown
            ):
                stop.set()
                return
            await asyncio.sleep(1)

    async def run_event_video(self):
        """Capture the whole intruder event as one video and upload it.

        Recording runs until the event goes quiet (or hits the max-length cap), then
        uploads a single file — rather than chopping the event into fixed-length
        clips. How it's captured is the engine's business (SD card vs ffmpeg).
        """
        # The camera pre-records a few seconds before the trigger, so its recording of
        # the event starts before we do — look back far enough to catch that.
        started_at = datetime.now(tz=timezone.utc) - timedelta(
            seconds=EVENT_CLIP_LOOKBACK_SEC
        )
        stop = asyncio.Event()
        watcher = asyncio.create_task(self.watch_for_event_end(stop))
        thumbnail = None

        try:
            await self.power_management.acquire()
            recorder = asyncio.create_task(
                self.engine.record_event_video(
                    started_at, stop, self.config.alarm.event_clip_max_secs.value
                )
            )
            # Grab the preview while the recording runs, so it catches the intruder
            # at the trigger rather than an empty scene once they've left.
            try:
                thumbnail = await self.engine.get_thumbnail()
            except Exception as e:
                log.warning(f"Failed to get event thumbnail: {e}", exc_info=e)
            video = await recorder
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(f"Failed to capture event video: {e}", exc_info=e)
            video = None
        finally:
            watcher.cancel()
            self._intruder_clip_task = None

        if not video:
            return

        log.info(f"Uploading event video ({video.size} bytes).")
        video.filename = "event.mp4"
        if thumbnail is not None:
            thumbnail.filename = f"event{THUMBNAIL_SUFFIX}.jpg"
        try:
            await self.upload_media(
                [Capture("event", video, thumbnail)], REASON_INTRUDER
            )
        except Exception as e:
            log.warning(f"Failed to publish event video: {e}", exc_info=e)

    def start_event_video(self):
        """Start recording this event, unless it's already being recorded."""
        if self._intruder_clip_task and not self._intruder_clip_task.done():
            return  # already recording — the extended timestamp keeps it going
        self._intruder_clip_task = asyncio.create_task(self.run_event_video())

    async def on_anpr_event_callback(self, event: ANPREvent):
        log.info(f"ANPR event: plate={event.plate}, vehicle={event.vehicle_type}.")
        if event.plate:
            await self.tags.last_plate.set(event.plate)
        # The plate rect isn't classified by the camera - it's a plate because of the
        # event it arrived on - so it gets labelled here.
        await self.lock_snapshot_and_run(
            "anpr",
            TargetBox.list_from_alert(event.data, default_target="plate"),
            event_frame=event_image(event.data),
        )

        await self.publish_camera_event(
            "anpr",
            plate=event.plate,
            vehicle_type=event.vehicle_type,
            confidence=event.confidence,
        )

        plate = event.plate or "unknown"
        await self.send_notification(
            f"{self.app_display_name} detected vehicle plate {plate}.",
            severity=NotificationSeverity.Info,
            topic="anpr_event",
        )

    def start_alarm_pulse(self):
        """Pulse the camera's alarm relay, without going deaf while it holds.

        ``fire_alarm`` holds the relay high for ``pulse_secs`` (10s by default) and this
        callback is being awaited by the engine's alertStream reader — so awaiting it inline
        stops the app hearing anything for those 10 seconds, including the re-alarms that are
        its only evidence the intruder is still there. Long enough, and the event looks over
        while it's still happening.

        A pulse already in flight is left to finish rather than restarted: the relay is one
        piece of hardware, and a second pulse's release would cut the first one short.
        """
        if not hasattr(self.engine, "fire_alarm"):
            return
        if self._alarm_pulse_task and not self._alarm_pulse_task.done():
            return
        self._alarm_pulse_task = asyncio.create_task(self.engine.fire_alarm())

    def start_external_alarm(self):
        """Start the external strobe/horn for this event, unless already running.

        A re-fired detection just pushes ``_last_intruder_event_at`` forward (done by
        the caller), which keeps the existing task going — like ``start_event_video``.
        """
        if self._external_alarm_task and not self._external_alarm_task.done():
            return
        self._external_alarm_task = asyncio.create_task(self.run_external_alarm())

    async def run_external_alarm(self):
        """Drive the Doovit strobe + horn for as long as the intruder is present.

        The strobe light is held on continuously for the whole event; the horn sounds
        in short bursts (``HORN_ON_SEC`` on out of every ``HORN_PERIOD_SEC``). Both run
        until the intruder goes quiet — tracked by the same ``_last_intruder_event_at``
        + cooldown watcher the event video uses (``watch_for_event_end``) — then both
        outputs are dropped. Each output's pin doubles as its enable: an unset pin is
        skipped, so this no-ops when neither is wired.

        These outputs may be **shared between camera apps** (two cameras wired to the
        same site strobe/horn), and each camera is its own app process. They coordinate
        through a per-pin cross-app tag (``camera_alarm_output_<pin>``, app_key=None,
        like camera power): while active, each app publishes its "hold until" deadline
        there, so an output is only dropped once *no* camera still holds it — one camera
        clearing can't cut the alarm while another still sees the intruder. The horn's
        burst on/off is derived from the shared wall clock rather than a private timer,
        so apps sharing the pin command the same state instead of fighting over it.
        """
        cfg = self.config.alarm
        strobe = cfg.doovit_strobe_pin.value
        horn = cfg.doovit_horn_pin.value
        pins = [p for p in (strobe, horn) if p is not None]
        if not pins:
            return

        stop = asyncio.Event()
        watcher = asyncio.create_task(self.watch_for_event_end(stop))
        cooldown_ms = cfg.event_clip_cooldown.value * 1000
        log.info(f"External alarm engaged (strobe pin={strobe}, horn pin={horn}).")
        try:
            while not stop.is_set():
                now_ms = self._now_ms()
                # Publish our hold so any camera app sharing these pins keeps them up
                # while we still see the intruder. Basing the deadline on the last
                # detection (not "now") keeps it consistent with the local watcher, so
                # our own stale hold expires exactly when we stop.
                basis = self._last_intruder_event_at or datetime.now(tz=timezone.utc)
                until_ms = int(basis.timestamp() * 1000) + cooldown_ms
                for pin in pins:
                    await self._extend_alarm_hold(pin, until_ms)

                # Strobe: solid on for the whole event. Horn: short repeated bursts,
                # phased off the shared wall clock so apps sharing the pin agree on the
                # on/off state rather than clobbering each other.
                if strobe is not None:
                    await self.platform_iface.set_do(strobe, True)
                if horn is not None:
                    burst_on = now_ms % (HORN_PERIOD_SEC * 1000) < HORN_ON_SEC * 1000
                    await self.platform_iface.set_do(horn, burst_on)

                await self._sleep_unless_stopped(stop, ALARM_TICK_SEC)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(f"External alarm failed: {e}", exc_info=e)
        finally:
            watcher.cancel()
            # Our intruder has cleared. Drop each output only if no other camera app
            # still holds it — otherwise leave it on for that app to manage.
            now_ms = self._now_ms()
            for pin in pins:
                try:
                    if not self._alarm_output_held(pin, now_ms):
                        await self.platform_iface.set_do(pin, False)
                except Exception as e:
                    log.warning(f"Failed to clear external output {pin}: {e}")
            self._external_alarm_task = None
            log.info("External alarm released.")

    @staticmethod
    def _now_ms() -> int:
        return int(datetime.now(tz=timezone.utc).timestamp() * 1000)

    @staticmethod
    def _alarm_hold_tag(pin: int) -> str:
        return f"{ALARM_HOLD_TAG_PREFIX}{pin}"

    async def _extend_alarm_hold(self, pin: int, until_ms: int) -> None:
        """Publish our "hold until" deadline for a (possibly shared) output pin.

        Cross-app coordination via a global tag (app_key=None). Uses max semantics so a
        peer camera with a later deadline is never lowered — the pin's effective hold is
        the latest deadline any camera has published for it.
        """
        tag = self._alarm_hold_tag(pin)
        current = self.tag_manager.get_tag(tag, default=0, app_key=None) or 0
        if until_ms > current:
            await self.tag_manager.set_tag(tag, until_ms, app_key=None)

    def _alarm_output_held(self, pin: int, now_ms: int) -> bool:
        """Whether any camera app still holds this output (deadline in the future)."""
        until = self.tag_manager.get_tag(self._alarm_hold_tag(pin), default=0, app_key=None)
        return bool(until) and until > now_ms

    @staticmethod
    async def _sleep_unless_stopped(stop: asyncio.Event, secs: float) -> bool:
        """Wait up to ``secs``, returning early True if ``stop`` is set meanwhile."""
        try:
            await asyncio.wait_for(stop.wait(), timeout=secs)
            return True
        except asyncio.TimeoutError:
            return False

    async def on_ppe_event_callback(self, event: PPEEvent):
        """A DeepinView PPE (hard-hat) violation: someone without a hard hat.

        Publishes a structured ``camera_event`` for automations, grabs a snapshot so
        there's an image of the violation, and (when configured) sends a notification.
        """
        count = event.no_hardhat
        log.info(f"PPE violation detected (missing hard hats: {count}).")

        await self.tags.last_ppe_violation.set(int(datetime.now().timestamp() * 1000))

        # Automations hook off this; publish before the slow snapshot work.
        await self.publish_camera_event("ppe", violation="no_hardhat", count=count)

        await self.lock_snapshot_and_run(
            "ppe",
            TargetBox.list_from_alert(event.data),
            event_frame=event_image(event.data),
        )

        if self.config.ppe.notify.value:
            who = f"{count} people" if count and count > 1 else "someone"
            await self.send_notification(
                f"{self.app_display_name} detected {who} without a hard hat.",
                severity=NotificationSeverity.Warn,
                topic="ppe_event",
            )

    # Cooldown key used when a detection can't be attributed to a zone — every camera that
    # doesn't report regions, which is most of them. Keeps those on a single shared
    # cooldown, exactly as before zones had identities.
    _UNZONED_COOLDOWN_KEY = None

    def claim_motion_snapshot(self, zones: list = None) -> bool:
        """Whether a motion snapshot may be taken now, claiming the slot if so.

        The camera keeps re-reporting a target while it stays in the zone, because the night
        alarm needs to know the intruder is still there (see ``set_static_target_alarm``).
        This is what stops that costing a snapshot, an upload and a cloud inference run
        every few seconds for the same parked vehicle.

        **The cooldown is per zone.** It used to be one global timer, which was right when
        one rule meant one set of zones, and wrong the moment a second rule could report the
        same person: walking into an excluded area 4s after tripping an ordinary zone got a
        notification and no picture, because the ordinary zone had already claimed the slot.
        One zone throttling *itself* is the point; one zone throttling *another* is not,
        least of all an ordinary zone silencing an excluded area's only image.

        Only the *picture* is throttled. The alarm, the notification and the
        ``camera_event`` are not: a siren that skips the second detection because the first
        was 10 seconds ago would be a real bug, whereas a near-identical frame is waste.
        """
        interval = self.config.motion_snapshot_min_interval
        if not interval:
            return True

        # An excluded area leads the list (zones_for_event sorts it there), so an event
        # matching both rules is throttled against the excluded area's own history rather
        # than the ordinary zone's.
        key = (
            (zones[0].rule, zones[0].id) if zones else self._UNZONED_COOLDOWN_KEY
        )

        now = datetime.now(tz=timezone.utc)
        last = self._last_motion_snapshot_at.get(key)
        if last is not None and (now - last).total_seconds() < interval:
            where = f" for {self._describe_zones(zones)}" if zones else ""
            log.info(
                f"Skipping snapshot{where} — last one was under {interval}s ago (the "
                f"camera is still reporting the same target)."
            )
            return False

        self._last_motion_snapshot_at[key] = now
        return True

    async def on_detection_continues(self):
        """The target that set off a live intruder event still hasn't left.

        This is what keeps the strobe, the horn and the recording going for as long as
        somebody is actually there. It only ever *extends* an event already in progress —
        ``watch_for_event_end`` reads the same timestamp — and deliberately does nothing
        else: no snapshot, no notification, no ``camera_event``, because the camera sends
        one of these every few seconds and each is the same intruder, not a new one.

        An event whose cooldown has already lapsed is left alone rather than resurrected:
        by then the recording has been fetched and uploaded, and reviving it would start a
        second clip of an intruder we've already reported.
        """
        last = self._last_intruder_event_at
        if last is None:
            return

        cooldown = self.config.alarm.event_clip_cooldown.value
        if (datetime.now(tz=timezone.utc) - last).total_seconds() > cooldown:
            return

        self._last_intruder_event_at = datetime.now(tz=timezone.utc)

        # Keep the camera's relay energised for as long as they're there. This is the one
        # output that has to be re-driven rather than held: it's pulsed for pulse_secs, so
        # chaining a fresh pulse each time the pulse ends keeps the siren going while
        # preserving the property that matters -- if this app dies, the relay drops on its
        # own within one pulse instead of sticking on. On an install with no Doovit
        # strobe/horn pins wired this is the *only* alarm output, so leaving it out of the
        # continuation path meant the siren ran for one pulse and stopped, however long the
        # intruder stayed.
        self.start_alarm_pulse()

        # Both no-ops while already running; they matter if one lost its race with the
        # cooldown while the intruder was, in fact, still standing there.
        self.start_external_alarm()
        if getattr(self.engine, "event_clip_mode", None):
            self.start_event_video()

    async def on_motion_event_callback(self, event: MotionDetectEvent):
        # Not a new detection — the camera saying the last one is still going on.
        if event.continuation:
            await self.on_detection_continues()
            return

        # Which zone the user drew this happened in, if we can tell. Empty means "no zone
        # opinion" rather than "no match" — see zones_for_event.
        zones = self.zones_for_event(event)
        excluded = next(
            (z for z in zones if z.kind is ZoneKind.excluded_area),
            None,
        )
        # Attached to the camera_event payloads so an automation can act on the zone
        # rather than re-deriving it. Omitted entirely when unknown, so a consumer can
        # tell "not in a zone" from "this camera doesn't report zones".
        zone_fields = (
            {
                "zone": zones[0].name or zones[0].id,
                "zone_kind": zones[0].kind.value,
            }
            if zones
            else {}
        )

        log.info(
            f"Motion event detected, type: {event.type}"
            f"{f' in {self._describe_zones(zones)}' if zones else ''}."
        )

        # Night intruder handling — applies to the Hikvision event cameras when
        # their intruder alarm is armed and we're in the night window. Covers
        # unclassified motion (ANPR VMD) and classified person/vehicle (AcuSense).
        intruder_engine = isinstance(
            self.engine, (HikvisionANPRCamera, HikvisionAcuSenseCamera)
        )
        if (
            intruder_engine
            and self.config.alarm.intruder_alarm_enabled.value
            and self.config.is_night()
        ):
            label = {
                MotionDetectEventType.person: "a person",
                MotionDetectEventType.vehicle: "a vehicle",
            }.get(event.type, "motion")

            # Automations hook off this; publish before the slow media work so a
            # downstream automation isn't waiting on a snapshot/clip upload.
            await self.publish_camera_event(
                "intruder", target=event.type.value, label=label, **zone_fields
            )

            # Whether this is a fresh intruder, or the camera re-reporting one it already
            # told us about. Worked out *before* the timestamp moves, and used only to keep
            # the notification to one per event: the camera re-alarms every few seconds
            # while a target stays in the zone, so notifying per event would be a message
            # every few seconds for one intruder. The alarm itself is never throttled.
            new_intruder = self._last_intruder_event_at is None or (
                datetime.now(tz=timezone.utc) - self._last_intruder_event_at
            ).total_seconds() > self.config.alarm.event_clip_cooldown.value

            # Mark the intruder as present now — both the event-video recorder and the
            # external strobe/horn track this timestamp (+ cooldown, via
            # watch_for_event_end) to tell when the intruder has gone. A re-fire just
            # pushes it forward, extending both rather than restarting them.
            self._last_intruder_event_at = datetime.now(tz=timezone.utc)

            # External strobe/horn wired to the Doovit outputs, for the whole event.
            self.start_external_alarm()

            # Event video is captured by a background task that outlives this
            # callback; a re-fire just extends the recording. When the engine couldn't
            # resolve a capture mode we keep the single-snapshot behaviour.
            if getattr(self.engine, "event_clip_mode", None):
                self.start_event_video()
            elif self.claim_motion_snapshot(zones):
                await self.lock_snapshot_and_run(
                    REASON_INTRUDER, event.boxes, event_frame=event.image
                )

            # The camera's own deterrent (flash / siren) is already armed natively.
            self.start_alarm_pulse()

            if new_intruder:
                # Deliberately NOT gated on the zone's notify flag, unlike the daytime
                # notifications below. This path only runs when the intruder alarm is
                # explicitly enabled *and* it's inside the night window, and it has just
                # sounded a siren and started recording. An alarm that does all that
                # without telling anyone is worse than useless, and a zone's notify flag is
                # about routine detections — it is not a request to be kept in the dark
                # about a night-time intruder. Turn the alarm itself off if that's wanted.
                await self.send_notification(
                    f"{self.app_display_name} detected {label} (possible intruder)"
                    f"{self._zone_suffix(zones)}.",
                    severity=NotificationSeverity.Warn,
                    topic="motion_event_intruder",
                )
            return

        # Outside the night window (or alarm disabled), raw unclassified motion
        # is not actionable — only classified person/vehicle events continue below.
        if event.type is MotionDetectEventType.motion:
            return

        # The picture is gated by the motion-snapshot window, but the event is not:
        # an automation still wants to know a person was seen at 3am even when the
        # site only keeps daytime images.
        if not self.config.motion_snapshot_allowed():
            log.info(
                f"Skipping {event.type.value} snapshot — outside the motion snapshot "
                f"window."
            )
        elif self.claim_motion_snapshot(zones):
            await self.lock_snapshot_and_run(
                event.type.value, event.boxes, event_frame=event.image
            )

        if event.type in (MotionDetectEventType.person, MotionDetectEventType.vehicle):
            await self.publish_camera_event(
                event.type.value, target=event.type.value, **zone_fields
            )

        # Somebody entering a zone marked "excluded area" is a different claim from
        # somebody being in frame, so it gets its own event kind for automations to hook
        # rather than being flattened into the person/vehicle one.
        if excluded is not None:
            await self.publish_camera_event(
                "excluded_area",
                target=event.type.value,
                zone=excluded.name or excluded.id,
            )

        # Whether a classified detection notifies is the zone's decision.
        #
        # Read from the stored zone record, never from a bare UI element. The gate this
        # replaces was a pair of UI switches created without values, so reading one raised
        # `KeyError: alert_me_on_human_motion` from the middle of this handler and killed
        # everything after it — which is far worse than an unwanted notification, and is
        # why it was made unconditional in the first place. A zone record always has a
        # `notify` value (DetectionZone fills the default in), and an install with no zones
        # falls back to notifying exactly as it did before, so neither the old crash nor a
        # silent camera is reachable from here.
        if not self.should_notify(zones, fallback=True):
            log.info(
                f"Not notifying for {event.type.value} — "
                f"{self._describe_zones(zones)} has notifications off."
            )
            return

        if excluded is not None:
            # Warn, not Info: this is somewhere nobody is supposed to be, which is a
            # different thing from a person walking through a monitored area.
            await self.send_notification(
                f"{self.app_display_name} detected {event.type.value} entering "
                f"{self._zone_label(excluded)}.",
                severity=NotificationSeverity.Warn,
                topic="excluded_area_event",
            )
            return

        match event.type:
            case MotionDetectEventType.person:
                await self.send_notification(
                    f"{self.app_display_name} has detected a person"
                    f"{self._zone_suffix(zones)}.",
                    severity=NotificationSeverity.Info,
                    topic="motion_event_person",
                )

            case MotionDetectEventType.vehicle:
                await self.send_notification(
                    f"{self.app_display_name} has detected a vehicle"
                    f"{self._zone_suffix(zones)}.",
                    severity=NotificationSeverity.Info,
                    topic="motion_event_vehicle",
                )

            case MotionDetectEventType.unknown:
                log.warning("Unknown event detected.")

    @staticmethod
    def _zone_label(zone) -> str:
        """A zone's name for humans, falling back to something unambiguous.

        The kind is part of the fallback because slot ids restart per kind — an intrusion
        zone and an excluded area are both "zone 1", and a log line saying so is no use
        when the whole question is which of them fired.
        """
        if zone.name:
            return f"'{zone.name}'"
        return f"{zone.kind.value} zone {zone.id}"

    @classmethod
    def _zone_suffix(cls, zones: list) -> str:
        """" in <zone>" for a notification, or nothing when no zone was identified."""
        if not zones:
            return ""
        return f" in {cls._zone_label(zones[0])}"

    @classmethod
    def _describe_zones(cls, zones: list) -> str:
        return ", ".join(cls._zone_label(z) for z in zones) or "no zone"

    @rpc.handler("get_immediate_snapshot", channel=CAMERA_CONTROL_CHANNEL)
    async def on_snapshot_command(self, ctx, payload):
        if self.snapshot_running:
            log.info("Skipping trigger snapshot request, snapshot task already running")
            return {self.app_key: "Snapshot already in progress"}

        log.info("Snapshot command received")
        await self.lock_snapshot_and_run(REASON_MANUAL)
        return {self.app_key: "success"}

    def zones_for_event(self, event: MotionDetectEvent) -> list:
        """The zone(s) a detection happened in, most specific first.

        Matched on ``(rule, slot id)`` — the camera reports which of a rule's regions
        fired, and the stored record says what that region is for.

        Returns ``[]`` when the zone can't be identified, which is the common case and not
        an error: cameras other than the Hikvision perimeter models report no region at
        all, a ``duration`` continuation carries none, and a camera whose zones were never
        written through this app has no records to match. **Callers must treat an empty
        result as "no zone opinion" and fall back to the camera-wide behaviour** — never as
        "no zone matched, so stay quiet". That distinction is the compatibility guarantee:
        an install that has never opened the zone editor must keep behaving exactly as it
        did before this feature existed.

        Excluded areas sort first so an overlapping pair resolves to the more specific
        statement. The camera genuinely reports both — one rule answering "is somebody in
        frame", the other "did somebody enter the place they're banned from" — and the
        second is the one worth acting on.
        """
        if not event.rule or not event.region_ids:
            return []

        stored = {}
        for raw in self.tags.detection_zones.value or []:
            try:
                zone = DetectionZone.from_dict(raw)
            except Exception:
                continue
            if zone.rule:
                stored[(zone.rule, zone.id)] = zone

        matched = [
            zone
            for region_id in event.region_ids
            if (zone := stored.get((event.rule, region_id))) is not None
        ]
        matched.sort(key=lambda z: z.kind is not ZoneKind.excluded_area)
        return matched

    def should_notify(self, zones: list, fallback: bool) -> bool:
        """Whether a detection in ``zones`` should raise a notification.

        ``fallback`` is used when no zone could be identified — see
        :meth:`zones_for_event` for why that must not mean "stay quiet".

        Any one matching zone asking for a notification is enough. A person standing in
        the overlap of a quiet zone and a loud one is still in the loud one, and the
        failure of missing a real alert is much worse than one extra message.
        """
        if not zones:
            return fallback
        return any(z.notify for z in zones)

    def merge_zone_records(self, from_camera: list) -> list:
        """Combine what the camera holds with what only the app knows.

        The camera is authoritative for the geometry of the rules it runs — a web-UI visit
        or a factory reset can change it behind our back, which is the whole reason zones
        are read back rather than echoed. But it has no concept of ``notify``, and no place
        at all for the ``ppe``/``anpr`` zones, so those come from
        ``tags.detection_zones``.

        Matching is by ``(kind, slot id)``, because that pair is what the camera actually
        addresses a region by; the frontend's ids are renumbered on write, so they are not
        a stable key. A camera-held zone with no stored record keeps its kind's default
        notify — that's a zone somebody drew in the camera's own web UI, and defaulting it
        quiet is the safer of the two.
        """
        stored = {}
        for raw in self.tags.detection_zones.value or []:
            try:
                zone = DetectionZone.from_dict(raw)
            except Exception as e:
                log.info(f"Ignoring an unreadable stored zone record: {e}")
                continue
            stored[(zone.kind, zone.id)] = zone

        merged = []
        for zone in from_camera:
            record = stored.pop((zone.kind, zone.id), None)
            if record is not None:
                zone.notify = record.notify
                # The camera doesn't store a zone's name either.
                zone.name = zone.name or record.name
            else:
                # A zone on the camera that this app has no record of: drawn before this
                # feature existed, or in the camera's own web UI.
                #
                # It reports as notifying, NOT as the kind's default, because notifying is
                # what actually happens to it — with no record, zones_for_event can't
                # identify it and should_notify falls back to the camera-wide behaviour
                # (see the no-zones compatibility guarantee). Showing the kind default
                # here would be a lie in the editor, and worse, saving that lie back would
                # silently switch off notifications for every zone somebody already had.
                zone.notify = True
            merged.append(zone)

        # Whatever's left is a zone the camera never sees. The app-only kinds belong here
        # and are the point of the tag; a leftover camera-backed kind means the camera has
        # lost a region we wrote (a factory reset, someone deleting it in the web UI), so
        # it's dropped rather than reported as live — the read-back must reflect what the
        # camera will actually act on.
        for (kind, _slot), zone in stored.items():
            # Every kind is a camera rule now, so anything left over means the camera has
            # lost a region we wrote (a factory reset, someone deleting it in the web UI).
            # Dropped rather than reported as live: the read-back must reflect what the
            # camera will actually act on.
            log.info(
                f"Stored {kind.value} zone {zone.id} is no longer on the camera; "
                f"dropping it from the zone list."
            )

        return merged

    async def store_zone_records(self, zones: list) -> None:
        """Keep the app's copy of the zones, for what the camera can't hold."""
        await self.tags.detection_zones.set([z.to_dict() for z in zones])

    def detector_zones(self, zones: list = None) -> list:
        """The zones asking for something the object detection app finds, as plain dicts.

        A zone qualifies by carrying at least one :class:`ZoneDetector`, not by being a
        kind of its own — one ordinary zone can want a person, their hard hat and any
        plate in the same polygon.

        These ride along on each snapshot rather than being fetched: they arrive with the
        frame they apply to, and the camera app is already the authority on what its own
        frames are for (the ``object_detection`` key does the same thing).
        """
        if zones is None:
            zones = [
                DetectionZone.from_dict(z)
                for z in (self.tags.detection_zones.value or [])
            ]
        return [z.to_dict() for z in zones if z.detectors]

    @staticmethod
    def warn_on_unreachable_detectors(zones: list) -> None:
        """Warn about a detector that can never run, because nothing will trigger a frame.

        The object detection app only sees snapshots the camera published, and the camera
        only publishes one when it classified a target. A zone asking for PPE while not
        watching for people produces no frames, so the model never runs and the zone
        silently does nothing — the exact failure that is hardest to notice.

        A warning rather than a correction: the UI selects the target for you, so a zone
        arriving without it came from something deliberate, and quietly rewriting what
        somebody wrote is worse than telling them.
        """
        for zone in zones:
            for detector in zone.detectors:
                needed = DETECTOR_REQUIRES_TARGET.get(detector)
                if needed and needed not in [t.value for t in zone.targets]:
                    log.warning(
                        f"Zone '{zone.name or zone.id}' asks for {detector.value} but "
                        f"does not detect '{needed}'. The camera only publishes a "
                        f"snapshot when it classifies a target, so nothing will ever be "
                        f"analysed for {detector.value} here. Add '{needed}' to the "
                        f"zone's targets."
                    )

    async def build_zone_state(self, error: str = None) -> dict:
        """Read the camera's current zones, plus what it can do with them.

        Always reads back off the camera rather than echoing what was asked for:
        these cameras will answer OK and then quietly ignore a field (Hikvision drops
        a region's `enabled`, for one), so an echo would show the frontend a state
        that doesn't exist. What the camera has no notion of is merged back in from the
        app's own record — see :meth:`merge_zone_records`.
        """
        state = {"capabilities": self.engine.ZONE_CAPABILITIES, "zones": []}
        if error:
            state["error"] = error

        if self.engine.ZONE_CAPABILITIES["supported"]:
            try:
                zones = await self.engine.get_detection_zones()
            except Exception as e:
                log.warning(f"Failed to read detection zones: {e}", exc_info=e)
                state.setdefault("error", str(e))
            else:
                state["zones"] = [z.to_dict() for z in self.merge_zone_records(zones)]

        return state

    async def publish_detection_zones(self, error: str = None, log_update: bool = True):
        """Read the camera's zones and publish them as the command's own value.

        The zones live in the `ui_cmds` aggregate as the command's value — same as any
        other interaction, where the value is the current state — so this is both how
        the editor gets its initial state and how it learns what a write actually did.
        """
        state = await self.build_zone_state(error)
        await self.ui.set_detection_zones.set(state, log_update=log_update)
        return state

    @ui.handler(SET_ZONES_CMD, parser=DetectionZonesPayload.from_dict, auto_update=False)
    async def on_set_detection_zones(self, ctx, payload: DetectionZonesPayload):
        """Write detection zones to the camera and publish what actually stuck.

        Runs over `ui_cmds` so the commands system records who changed the zones and
        when.

        auto_update is off deliberately. It would write the *request* back as the new
        value (it's handed the parsed payload, not what we return) — which is both the
        echo we're trying to avoid, and unserialisable here since our parser hands
        back a DetectionZonesPayload. So we publish the read-back ourselves.
        """
        if not self.engine.ZONE_CAPABILITIES["supported"]:
            error = f"{self.config.type.value} does not support detection zones"
            log.info(f"Rejecting zone write: {error}")
            return await self.publish_detection_zones(error=error)

        error = None
        try:
            await self.engine.set_detection_zones(payload.zones)
        except Exception as e:
            log.warning(f"Failed to set detection zones: {e}", exc_info=e)
            error = str(e)
        else:
            # Only on success, and only after the camera has taken them: the record is
            # meant to describe zones that exist. Storing a rejected write would leave the
            # app notifying for an excluded area the camera isn't watching.
            #
            # Slot ids are renumbered per kind to match what the engine wrote, so the
            # record keys line up with what the camera reports back (see
            # merge_zone_records).
            self.warn_on_unreachable_detectors(payload.zones)
            await self.store_zone_records(self._with_slot_ids(payload.zones))

        return await self.publish_detection_zones(error=error)

    @staticmethod
    def _with_slot_ids(zones: list) -> list:
        """Renumber zones per kind, mirroring how the engine assigns camera slots.

        The engine addresses regions by position within their kind, discarding whatever
        ids the frontend sent (see ``HikvisionAcuSenseCamera._to_region``). The stored
        record has to agree with that or the merge can't pair them up.
        """
        counters = {}
        for zone in zones:
            counters[zone.kind] = counters.get(zone.kind, 0) + 1
            zone.id = counters[zone.kind]
        return zones

    async def sync_presets(self, active_preset: str = None):
        if self.config.control_enabled.value:
            try:
                presets = await self.engine.fetch_presets()
            except Exception as e:
                log.info(f"Failed to get presets: {e}. Falling back to tag values...")
            else:
                await self.tags.presets.set(presets)

            if active_preset:
                await self.tags.active_preset.set(active_preset)

    async def clear_preset(self):
        await self.tags.active_preset.set(None)
