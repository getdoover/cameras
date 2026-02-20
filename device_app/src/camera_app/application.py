import asyncio
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

import aiohttp
from pydoover.docker import Application
from pydoover import ui
from pydoover.docker.device_agent.models import MessageCreateEvent

from .app_config import CameraConfig, CameraType
from .app_ui import CameraUI
from .engines import DahuaPTZCamera
from .engines.dahua_base import DahuaCameraBase
from .engines.dahua_fixed import DahuaFixedCamera
from .engines.generic import GenericRTSPCamera
from .events import MotionDetectEvent, MotionDetectEventType
from .power_management import CameraPowerManagement

log = logging.getLogger()

GET_NOW_CMD_NAME = "camera_snapshots"
LAST_SNAPSHOT_CMD_NAME = "last_cam_snapshot"
UI_CONNECT_POWERON_TIMEOUT_SEC = 60 * 15  # 15min


class CameraApplication(Application):
    config: CameraConfig

    async def setup(self):
        self.power_management = CameraPowerManagement(self)

        self.ui = CameraUI(self.config, self.app_key, self.app_display_name)
        self.ui_manager.add_children(*self.ui.fetch())

        # we don't want a submodule view for cameras since the UI
        # renders it as a submodule anyway (and we'd end up with double submodules).
        self.ui_manager.set_variant(ui.ApplicationVariant.stacked)

        self.snapshot_running = None
        self._shutdown_at = None

        # the below is probably a "fix in doover 2.0" problem to have some better / more native
        # camera feels
        self.ui_manager._transform_interaction_name = self._transform_interaction_name
        self.ui_manager._add_interaction(ui.SlimCommand(GET_NOW_CMD_NAME))
        self.ui_manager._add_interaction(ui.SlimCommand(LAST_SNAPSHOT_CMD_NAME))

        self.subscribe_to_channel("ui_state@wss_connections", self.on_user_connection)

        self.control_task = asyncio.create_task(self.handle_control_messages())
        # self.device_agent.subscribe_to_channel_messages("camera_control", self.on_control_message)

        ui_cmds = await self.get_channel_aggregate("ui_cmds")

        to_send = {}
        to_send.update(
            self.configure_detect_alerts(
                ui_cmds, "human", self.config.human_detect_enabled
            )
        )
        to_send.update(
            self.configure_detect_alerts(
                ui_cmds, "vehicle", self.config.vehicle_detect_enabled
            )
        )
        if to_send:
            await self.publish_to_channel("ui_cmds", {"cmds": to_send})

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
            case _:
                raise ValueError(f"Unknown camera type: {self.config.type.value}")

        await self.engine.setup()
        await self.setup_rtsp_server()
        await self.sync_presets()

    async def close(self):
        await self.engine.close()

    async def on_user_connection(self, _, payload: dict[str, Any]):
        # we get the full payload here, but so does ui manager (at roughly the same time)
        # so let's just sleep for a second to make sure ui manager is up-to-date and then
        # use their logic to save repeating ourselves (this will also make it easier for doover 2.0 migration)
        await asyncio.sleep(0.5)
        if self.ui_manager.is_being_observed():
            log.info("Enabling power for user observation.")
            await self.power_management.acquire_for(
                timedelta(seconds=UI_CONNECT_POWERON_TIMEOUT_SEC)
            )

    async def on_shutdown_at(self, shutdown_at: datetime):
        self._shutdown_at = shutdown_at
        await self.power_management.release()

    async def handle_control_messages(self):
        while True:
            try:
                async for event in self.device_agent.stream_channel_events(
                    "camera_control"
                ):
                    if isinstance(event, MessageCreateEvent):
                        await self.on_control_message(event.id, event.data)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Control tasks failed: {e}", exc_info=e)
                await asyncio.sleep(5)

    async def on_control_message(self, message_id, payload: dict[str, Any]):
        if payload in (None, "None"):
            # this is dumb, but whatever...
            return

        log.info("Received control command...")

        try:
            data = payload[self.app_key]
        except KeyError:
            log.info("Discarding control command, not for this camera.")
            return

        # try:
        #     task_id = data["task_id"]
        # except KeyError:
        #     log.info("Discarding control command, no task id.")
        #     return
        #
        # if task_id == self.get_tag("last_processed_task", default=None):
        #     log.info("Discarding control command, already processed.")
        #     return

        if data.get("action") == "power_on":
            # just do this here again, no harm in duplicating this...
            await self.setup_rtsp_server()
            await self.power_management.acquire()

        if data.get("action") == "accept_sdp":
            await self.accept_sdp_offer(data.get("value"))

        log.info(f"Received control command, forwarding to engine: {data}.")

        asyncio.create_task(self.engine.on_control_message(message_id, data))
        # try:
        #     await self.engine.on_control_message(data)
        # except Exception as e:
        #     log.error(f"Error processing control message: {e}", exc_info=e)
        # else:
        #     await self.set_tag("last_processed_task", task_id)

    async def main_loop(self):
        if self.check_snapshot_can_run():
            log.info("Running snapshot from main loop.")
            await self.lock_snapshot_and_run()

    def check_snapshot_can_run(self):
        if self.config.snapshot.enabled.value is False:
            return False

        if self.snapshot_running:
            return False

        last_snapshot = self.get_tag("last_cam_snapshot", default=None)
        if last_snapshot is None:
            # this will be pretty rare, but in the case of a snapshot having never ever been run...
            return True

        if (datetime.now() - datetime.fromtimestamp(last_snapshot)) > timedelta(
            seconds=self.config.snapshot.period.value
        ):
            return True

        return False

    async def lock_snapshot_and_run(self):
        self.snapshot_running = True
        try:
            await self.run_snapshot()
        except Exception as e:
            log.error(f"Error getting snapshot: {str(e)}", exc_info=e)
        self.snapshot_running = False

        # should probably migrate all of this to use tags but it's all baked into the UI so I can't be bothered right
        # now, so just duplicate the efforts.
        now = datetime.now()
        await self.set_tag("last_cam_snapshot", now.timestamp())
        self.ui_manager.coerce_command(LAST_SNAPSHOT_CMD_NAME, now.timestamp())
        self.ui_manager.coerce_command(GET_NOW_CMD_NAME, "completed")

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
        data = None
        while error_count < retries:
            try:
                data = await self.engine.get_snapshot()
            except Exception as e:
                log.info(f"Failed to get snapshot: {e}, retrying...")
                await asyncio.sleep(1)
                error_count += 1
                continue

            if data:
                # we've got the camera, keep moving...
                break
            else:
                log.info("Failed to get snapshot, retrying...")
                await asyncio.sleep(1)
                error_count += 1

        if data is None:
            log.info("Failed to get snapshot after retries")
            return False

        message = json.dumps(
            {
                "camera_name": self.app_key,
                "output": data.decode(),
                "output_type": self.config.snapshot.mode_as_filetype,
            }
        )
        log.debug(f"message length is {len(message)}, message: {message}")

        try:
            await self.device_agent.publish_to_channel(
                self.app_key, message, max_age=-1
            )
        except Exception as e:
            log.warning(f"Failed to publish snapshot: {e}", exc_info=e)
        else:
            await asyncio.sleep(2)
            # Publish a message to the ui state to invoke the ui client refreshing the page
            # Publish a message that is informative, but not likely to disrupt the UI.
            # This message should be overridden by the ui_manager very quickly
            # This is a bit of a dirty hack
            msg = {"camera_snapshots": {self.app_key: {"last_snap": int(time.time())}}}
            log.debug(f"snapshot complete message is {msg}")

            # send the message to the DDA and send it now (not waiting for max age)
            response = await self.device_agent.publish_to_channel(
                "ui_state", msg, max_age=-1
            )
            log.info(f"snapshot complete response is {response}")

    async def setup_rtsp_server(self):
        if not self.config.rtsp_server.enabled.value:
            log.info("RTSP server disabled in config. Ignoring...")
            return

        base = self.config.rtsp_server.address.value
        auth = aiohttp.BasicAuth("demo", "demo")

        async with aiohttp.request("GET", f"{base}/streams", auth=auth) as resp:
            data = await resp.json()

        try:
            configured_url = data["payload"][self.app_key]["channels"]["0"]["url"]
        except KeyError:
            method = "add"  # doesn't exist
        else:
            if configured_url == self.config.rtsp_uri:
                log.info("RTSP server stream already exists. Skipping...")
                return  # already exists

            method = "edit"

        body = {
            "name": self.app_key,
            "channels": {
                "0": {
                    "name": self.app_key,
                    "url": self.config.rtsp_uri,
                    "on_demand": True,
                    "debug": False,
                }
            },
        }
        log.info("Creating rtsp server stream...")
        async with aiohttp.request(
            "POST",
            f"{base}/stream/{quote(self.app_key)}/{method}",
            json=body,
            auth=auth,
        ) as resp:
            assert resp.status == 200

    async def accept_sdp_offer(self, offer: str):
        base = self.config.rtsp_server.address.value
        auth = aiohttp.BasicAuth("demo", "demo")

        credentials = await self.device_agent.get_turn_credential()
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
            f"{base}/stream/{quote(self.app_key)}/channel/0/webrtc?uuid={quote(self.app_key)}&channel=0",
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
                await self.device_agent.publish_to_channel_async(
                    self.app_key, {"sdp": answer}, max_age=-1
                )

    async def on_motion_event_callback(self, event: MotionDetectEvent):
        log.info(f"Motion event detected, type: {event.type}.")

        ui_cmds = await self.get_channel_aggregate("ui_cmds")
        if not ui_cmds:
            log.info("ui_cmds unknown, skipping event.")
            return
        # fixme: remove this for doover 2.0

        await self.lock_snapshot_and_run()

        match event.type:
            case MotionDetectEventType.person:
                if ui_cmds.get(f"{self.app_key}_human_detect") is True:
                    await self.publish_to_channel(
                        "significantEvent",
                        f"{self.app_display_name} has detected a person.",
                    )

            case MotionDetectEventType.vehicle:
                if ui_cmds.get(f"{self.app_key}_vehicle_detect") is True:
                    await self.publish_to_channel(
                        "significantEvent",
                        f"{self.app_display_name} has detected a vehicle.",
                    )

            case MotionDetectEventType.unknown:
                log.warning("Unknown event detected.")

    def configure_detect_alerts(self, ui_cmds_payload, name, enabled):
        log.info(f"Alert {name} is {'enabled' if enabled else 'disabled'}.")

        key = f"{self.app_key}_{name}_detect"
        if enabled and key not in ui_cmds_payload:
            # default to no alerts for users.
            log.info("Adding to ui_cmds")
            return {key: False}
        elif enabled is False and key in ui_cmds_payload:
            # get rid of this field from ui_cmds so we don't display to user.
            log.info("Removing from ui_cmds")
            return {key: None}
        return {}

    @ui.callback(GET_NOW_CMD_NAME, global_interaction=True)
    async def on_snapshot_command(self, _command, new_value: str):
        if new_value != "get_immediate_snapshot":
            return

        if self.snapshot_running:
            log.info("Skipping trigger snapshot request, snapshot task already running")
            return

        log.info(f"Snapshot command received: {new_value}")
        await self.lock_snapshot_and_run()

    def _transform_interaction_name(self, name):
        log.info(f"Transform interaction name received: {name}")
        # inject the app key (unique) into the interaction name
        # so we don't have namespace collisions between apps.
        if self.app_key in name:
            return name
        if name in (GET_NOW_CMD_NAME, LAST_SNAPSHOT_CMD_NAME):
            return name
        return f"{self.app_key}_{name.strip()}"

    async def sync_presets(self, active_preset: str = None):
        if self.config.control_enabled.value:
            try:
                presets = await self.engine.fetch_presets()
            except Exception as e:
                log.info(f"Failed to get presets: {e}. Falling back to tag values...")
                presets = self.get_tag("presets")
            else:
                await self.set_tag("presets", presets)

            if active_preset:
                await self.set_tag("active_preset", active_preset)
            else:
                active_preset = self.get_tag("active_preset")

            self.ui.update_presets(presets, active_preset)

    async def clear_preset(self):
        await self.set_tag("active_preset", None)
        await self.ui.update_presets(self.get_tag("presets"), None)
