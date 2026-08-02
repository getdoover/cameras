import asyncio
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import aiohttp
from pydoover import rpc, ui
from pydoover.docker import Application
from pydoover.models import EventSubscription, AggregateUpdateEvent, NotificationSeverity

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
    DetectionZonesPayload,
    MotionDetectEvent,
    MotionDetectEventType,
    PPEEvent,
    SDPOfferPayload,
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

        # Event-video state: recording runs for as long as the intruder keeps
        # re-triggering, and stops once the cooldown lapses with no new detection.
        self._intruder_clip_task = None
        self._last_intruder_event_at = None

        # Background task driving the external strobe/horn on the Doovit outputs for as
        # long as an intruder is present. Re-triggers extend it rather than restart it.
        self._external_alarm_task = None

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
        """Arm/disarm the camera's native night alarm based on the time window.

        Only a fallback for AcuSense: when the camera accepted a native arming
        schedule at setup, it gates the linkage itself and arm_night_deterrent
        no-ops. The relay pulse happens per-event (see on_motion_event_callback).
        Both arm calls are idempotent, so running every loop is cheap.
        """
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

    async def lock_snapshot_and_run(self, reason: str = REASON_SCHEDULE):
        self.snapshot_running = True
        try:
            await self.run_snapshot(reason)
        except Exception as e:
            log.error(f"Error getting snapshot: {str(e)}", exc_info=e)
        self.snapshot_running = False

        now = datetime.now()
        await self.tags.last_cam_snapshot.set(now.timestamp())

        # might as well update presets when we're fetching snapshots...
        await self.sync_presets()

    async def run_snapshot(
        self, reason: str = REASON_SCHEDULE, retries: int = 3, ping_timeout: int = 20
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
            # maybe this should put an error banner up on the UI? log the error somehow?
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
            return False

        try:
            # Every capture goes up, not just the first — a PTZ camera returns one
            # per preset, and a thermal camera a visible and a thermal view.
            await self.upload_media(files, reason)
        except Exception as e:
            log.warning(f"Failed to publish snapshot: {e}", exc_info=e)
        else:
            await asyncio.sleep(2)

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

    async def upload_media(self, captures: list, reason: str):
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
        await self.lock_snapshot_and_run("anpr")

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

        await self.lock_snapshot_and_run("ppe")

        if self.config.ppe.notify.value:
            who = f"{count} people" if count and count > 1 else "someone"
            await self.send_notification(
                f"{self.app_display_name} detected {who} without a hard hat.",
                severity=NotificationSeverity.Warn,
                topic="ppe_event",
            )

    async def on_motion_event_callback(self, event: MotionDetectEvent):
        log.info(f"Motion event detected, type: {event.type}.")

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
                "intruder", target=event.type.value, label=label
            )

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
            else:
                await self.lock_snapshot_and_run(REASON_INTRUDER)

            # The camera's own deterrent (flash / siren) is already armed natively.
            if hasattr(self.engine, "fire_alarm"):
                await self.engine.fire_alarm()

            await self.send_notification(
                f"{self.app_display_name} detected {label} (possible intruder).",
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
        if self.config.motion_snapshot_allowed():
            await self.lock_snapshot_and_run(event.type.value)
        else:
            log.info(
                f"Skipping {event.type.value} snapshot — outside the motion snapshot "
                f"window."
            )

        if event.type in (MotionDetectEventType.person, MotionDetectEventType.vehicle):
            await self.publish_camera_event(event.type.value, target=event.type.value)

        match event.type:
            case MotionDetectEventType.person:
                if self.ui_manager.get_value("alert_me_on_human_motion") is True:
                    await self.send_notification(
                        f"{self.app_display_name} has detected a person.",
                        severity=NotificationSeverity.Info,
                        topic="motion_event_person",
                    )

            case MotionDetectEventType.vehicle:
                if self.ui_manager.get_value("alert_me_on_vehicle_motion") is True:
                    await self.send_notification(
                        f"{self.app_display_name} has detected a vehicle.",
                        severity=NotificationSeverity.Info,
                        topic="motion_event_vehicle",
                    )

            case MotionDetectEventType.unknown:
                log.warning("Unknown event detected.")

    @rpc.handler("get_immediate_snapshot", channel=CAMERA_CONTROL_CHANNEL)
    async def on_snapshot_command(self, ctx, payload):
        if self.snapshot_running:
            log.info("Skipping trigger snapshot request, snapshot task already running")
            return {self.app_key: "Snapshot already in progress"}

        log.info("Snapshot command received")
        await self.lock_snapshot_and_run(REASON_MANUAL)
        return {self.app_key: "success"}

    async def build_zone_state(self, error: str = None) -> dict:
        """Read the camera's current zones, plus what it can do with them.

        Always reads back off the camera rather than echoing what was asked for:
        these cameras will answer OK and then quietly ignore a field (Hikvision drops
        a region's `enabled`, for one), so an echo would show the frontend a state
        that doesn't exist.
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
                state["zones"] = [z.to_dict() for z in zones]

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

        return await self.publish_detection_zones(error=error)

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
