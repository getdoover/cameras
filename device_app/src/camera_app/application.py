import asyncio
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import aiohttp
from pydoover import rpc
from pydoover.docker import Application
from pydoover.models import EventSubscription, AggregateUpdateEvent

from .app_config import CameraConfig, CameraType
from .app_tags import CameraTags
from .app_ui import CameraUI
from .engines import DahuaPTZCamera
from .engines.dahua_base import DahuaCameraBase
from .engines.dahua_fixed import DahuaFixedCamera
from .engines.generic import GenericRTSPCamera
from .engines.hikvision_thermal import HikVisionThermal
from .events import (
    MotionDetectEvent,
    MotionDetectEventType,
    SDPOfferPayload,
    CAMERA_CONTROL_CHANNEL,
)
from .power_management import CameraPowerManagement

log = logging.getLogger()

GET_NOW_CMD_NAME = "camera_snapshots"
LAST_SNAPSHOT_CMD_NAME = "last_cam_snapshot"
UI_CONNECT_POWERON_TIMEOUT_SEC = 60 * 15  # 15min


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
            case CameraType.hikvision_thermal:
                self.engine = HikVisionThermal(self.config)
            case _:
                raise ValueError(f"Unknown camera type: {self.config.type.value}")

        self.rpc.register_handlers(self.engine)

        await self.engine.setup()
        await self.setup_rtsp_server()
        await self.sync_presets()

    async def close(self):
        if self.engine:
            await self.engine.close()

    async def on_user_connection(self, event: AggregateUpdateEvent):
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
        if self.check_snapshot_can_run():
            log.info("Running snapshot from main loop.")
            await self.lock_snapshot_and_run()

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

    async def lock_snapshot_and_run(self):
        self.snapshot_running = True
        try:
            await self.run_snapshot()
        except Exception as e:
            log.error(f"Error getting snapshot: {str(e)}", exc_info=e)
        self.snapshot_running = False

        now = datetime.now()
        await self.tags.last_cam_snapshot.set(now.timestamp())

        # might as well update presets when we're fetching snapshots...
        await self.sync_presets()

    async def run_snapshot(self, retries: int = 3, ping_timeout: int = 20):
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
            await self.device_agent.create_message(self.app_key, {}, files)
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
        except KeyError:
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

    async def on_motion_event_callback(self, event: MotionDetectEvent):
        log.info(f"Motion event detected, type: {event.type}.")
        await self.lock_snapshot_and_run()

        match event.type:
            case MotionDetectEventType.person:
                if self.ui_manager.get_value("alert_me_on_human_motion") is True:
                    payload = {
                        "message": f"{self.app_display_name} has detected a person.",
                        "topic": "motion_event_person",
                        "severity": "Info",
                    }
                    await self.create_message("notifications", payload)

            case MotionDetectEventType.vehicle:
                if self.ui_manager.get_value("alert_me_on_vehicle_motion") is True:
                    payload = {
                        "message": f"{self.app_display_name} has detected a vehicle.",
                        "topic": "motion_event_vehicle",
                        "severity": "Info",
                    }
                    await self.create_message("notifications", payload)

            case MotionDetectEventType.unknown:
                log.warning("Unknown event detected.")

    @rpc.handler("camera_snapshots", channel=CAMERA_CONTROL_CHANNEL)
    async def on_snapshot_command(self, ctx, payload):
        if self.snapshot_running:
            log.info("Skipping trigger snapshot request, snapshot task already running")
            return {self.app_key: "Snapshot already in progress"}

        log.info("Snapshot command received")
        await self.lock_snapshot_and_run()
        return {self.app_key: "success"}

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
