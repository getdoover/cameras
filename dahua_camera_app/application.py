import asyncio
import logging
import os
import re
import time
from typing import Any
from urllib.parse import quote

import aiohttp
from pydoover.docker import app_base, run_app
from pydoover.ui import SlimCommand

from camera_iface import DahuaPTZCamera, DahuaFixedCamera, GenericRTSPCamera, Camera, MessageTooLong
from config import CameraConfig
from power_management import CameraPowerManagement

log = logging.getLogger(__name__)
HOST_MATCH = re.compile(r"rtsp://(.*:.*@)?(?P<host>.*):[0-9]*/.*")


class DahuaCameraApplication(app_base):
    camera: Camera
    power_management: CameraPowerManagement
    
    config: CameraConfig

    snapshot_running: bool
    last_camera_snapshot: float = 0

    camera_snap_cmd_name: str = "camera_snapshots"
    last_snapshot_cmd_name: str = "last_cam_snapshot"

    async def setup(self):
        agent_iface = self.get_agent_iface()
        self.subscribe_to_channel("camera_control", self.on_control_message)

        config_manager = self.get_config_manager()
        await config_manager.get_config_async([])
        config: dict[str, Any] = config_manager.last_deployment_config
        
        self.config = CameraConfig(config)
        self.power_management = CameraPowerManagement(self.platform_iface, self.config)

        if self.config.type == "dahua_ptz":
            self.camera = DahuaPTZCamera.from_config(self.config, agent_iface, self.power_management)
        elif self.config.type == "dahua_fixed":
            self.camera = DahuaFixedCamera.from_config(self.config, agent_iface, self.power_management)
        else:
            # this is two-fold - matches generic (and unknown) camera types, but also
            # falls back to a generic camera if some of the config is missing (username, etc.)
            # and one of the the above matches fails
            self.camera = GenericRTSPCamera.from_config(self.config, agent_iface, self.power_management)

        await self.camera.setup()

        # dunno what this is transforming from, but just make it a very basic {name: uri} dict.
        try:
            await self.setup_rtsp_server_config(self.config.name, self.config.rtsp_uri)
        except Exception as e:
            # we don't really care if this fails but just let us know anyway...
            log.error("Failed to setup rtsp server config.", exc_info=e)

        self.snapshot_running = False

        self.camera_snap_cmd_name = "camera_snapshots"
        self.last_snapshot_cmd_name = "last_cam_snapshot"

        self.ui_manager.add_children(*self.camera.fetch_ui_elements())
        self.ui_manager._add_interaction(SlimCommand(self.camera_snap_cmd_name, callback=self.on_snapshot_command))


    async def main_loop(self):
        if not self.snapshot_running and time.time() - self.last_camera_snapshot > self.config.snapshot_period:
            await self._lock_snapshot_and_run()

    async def _lock_snapshot_and_run(self):
        self.snapshot_running = True
        try:
            await self.run_snapshot()
        except Exception as e:
            log.error(f"Error getting snapshot: {str(e)}", exc_info=e)
        self.snapshot_running = False
        self.set_last_snapshot_time()

    async def run_snapshot(self, retries=3, ping_wait=20):

        success = False
        async with self.power_management.acquire(self.camera.config.rtsp_uri):

            # await a successful ping to the camera
            try:
                hostname = self.camera.config.address
                if not hostname:
                    raise ValueError(f"Failed to extract hostname config: {self.camera.config.address}")

                start_time = time.time()
                while time.time() - start_time < ping_wait:
                    response = os.system(f"ping -c 1 {hostname}")
                    if response == 0:
                        break
                    else:
                        log.debug(f"Awaiting ping from camera {hostname}")
                        await asyncio.sleep(1)

            except Exception as e:
                log.exception(f"Failed to ping camera: {str(e)}")

            ## attempt to take a snapshot
            error_count = 0
            while not success and error_count < retries:
                data = await self.camera.get_snapshot()
                if not data:
                    error_count += 1
                    continue

                try:
                    result = await self.camera.publish_snapshot(data, self.config.snapshot_mode)
                except MessageTooLong:
                    if self.config.snapshot_secs is not None and isinstance(self.config.snapshot_secs, (int, float)):
                        log.info(f"Reducing snapshot length from {self.config.snapshot_secs} to {self.config.snapshot_secs * 0.7}")
                        self.config.snapshot_secs = self.config.snapshot_secs * 0.7

                    result = None

                log.debug("publish snapshot result: " + str(result))
                if result:
                    success = True
                    break
                else:
                    log.warning("Failed to publish snapshot, retrying...")
                    error_count += 1

        if success:
            await asyncio.sleep(2)
            await self.publish_snapshot_complete()
        return True

    async def publish_snapshot_complete(self):
        ## Publish a message to the ui state to invoke the ui client refreshing the page
        ## Publish a message that is informative, but not likely to disrupt the UI.
        ## This message should be overridden by the ui_manager very quickly
        ## This is a bit of a dirty hack
        msg = {"camera_snapshots": {self.camera.name: {"last_snap": int(time.time())}}}
        log.debug(f"snapshot complete message is {msg}")

        # send the message to the DDA
        response = await self.publish_to_channel("ui_state", msg)
        log.info(f"snapshot complete response is {response}")

    @staticmethod
    async def setup_rtsp_server_config(name, uri):
        base = "http://localhost:8083"
        auth = aiohttp.BasicAuth("demo", "demo")

        async with aiohttp.request("GET", "http://localhost:8083/streams", auth=auth) as resp:
            data = await resp.json()

        if name in data.get("payload"):
            return  # already exists

        body = {"name": name, "channels": {"0": {"name": name, "url": uri, "on_demand": True, "debug": False}}}
        async with aiohttp.request("POST", f"{base}/stream/{quote(name)}/add", json=body, auth=auth) as resp:
            assert resp.status == 200

    async def on_control_message(self, _, payload):
        if not hasattr(self, "camera"):
            # fixme: make a `.wait_until_ready()` function, or better yet don't invoke these until setup is complete.
            return

        if payload in (None, "None"):
            return

        for name, data in payload.items():
            if name != self.camera.name:
                continue

            try:
                await self.camera.on_control_message(data)
            except Exception as e:
                log.error(f"Error processing control message for camera {name}: {str(e)}", exc_info=e)
                continue

        # await asyncio.gather(*tasks)

    async def on_snapshot_command(self, new_value: str):
        print("running snapshot command")
        if new_value != "get_immediate_snapshot":
            return

        self.ui_manager.coerce_command(self.camera_snap_cmd_name, None)
        if self.snapshot_running:
            log.info("Skipping trigger snapshot request, snapshot task already running")
            return

        await self._lock_snapshot_and_run()

    def set_last_snapshot_time(self, ts=None):
        ts = ts or time.time()

        self.last_camera_snapshot = ts
        self.ui_manager.coerce_command(self.last_snapshot_cmd_name, ts)


if __name__ == "__main__":
    run_app(DahuaCameraApplication())
