import argparse
import asyncio
import base64
import io
import json
import logging
import uuid
import os
import time
import pathlib
import re
from urllib.parse import quote

import aiohttp
import grpc

from PIL import Image

from pydoover.docker import device_agent_iface
from pydoover.docker.camera.grpc_stubs import camera_iface_pb2, camera_iface_pb2_grpc
from pydoover.docker.camera import CameraPowerManagement
from pydoover.docker.platform.platform import platform_iface
from pydoover.docker.doover_docker import deployment_config_manager

from dahua import DahuaClient


OUTPUT_FILE_DIR = pathlib.Path("/tmp/camera")
DEFAULT_FPS = 5
DEFAULT_SCALE = "360:-1"
MAX_MESSAGE_SIZE = 125_000
DEFAULT_POWER_TIMEOUT = 60 * 15  # 15 minutes

log = logging.getLogger(__name__)


EVENT_MATCH = re.compile(r"(?P<boundary>.*)\r\n"
                         r"Content-Type: (?P<content>.*)\r\n"
                         r"Content-Length: (?P<content_length>\d*)\r\n\r\n"
                         r"Code=(?P<code>.*);action=(?P<action>.*);index=(?P<index>.*);data=(?P<data>.*)", re.DOTALL)

URI_MATCH = re.compile(r"rtsp://(?P<username>.*):(?P<password>.*)@(?P<address>.*):(?P<port>.*)/(?P<channel>.*)")


def get_or_fallback_to_match(config, match, config_key, match_key):
    try:
        return config[config_key]
    except KeyError:
        try:
            return match.group(match_key)
        except AttributeError:
            return None


class MessageTooLong(Exception):
    pass


class Camera:
    def __init__(self, name, display_name, rtsp_uri, power_timeout, dda_iface, power_manager: CameraPowerManagement):
        self.name = name
        self.display_name = display_name
        self.rtsp_uri = rtsp_uri
        self.dda_iface = dda_iface

        self.power_manager = power_manager
        self.camera_power_timeout = power_timeout  # 15 minutes

    @classmethod
    def from_config(cls, config, dda_iface, power_manager):
        return cls(config["NAME"], config["DISPLAY_NAME"], config["URI"], config.get("POWER_TIMEOUT", DEFAULT_POWER_TIMEOUT), dda_iface, power_manager)

    async def setup(self):
        pass

    async def get_snapshot(self, snapshot_type, length, fps, scale):
        raise NotImplementedError

    async def publish_snapshot(self, output_b64, snapshot_type):
        message = json.dumps({
            "camera_name": self.name,
            "output": output_b64,
            "output_type": snapshot_type,
        })
        logging.debug(f"message to send is {message}")
        logging.debug(f"message length is {len(message)}")

        if len(message) > MAX_MESSAGE_SIZE:
            logging.error(f"Snapshot message too large to send: {len(message)} > {MAX_MESSAGE_SIZE}")
            raise MessageTooLong

        resp = await self.dda_iface.publish_to_channel(self.name, message)
        logging.debug(f"response is {resp}")
        return resp

    @staticmethod
    def get_output_filepath(task_id, snapshot_type):
        return OUTPUT_FILE_DIR / f"{task_id}.{snapshot_type}"

    @staticmethod
    def ensure_output_dir() -> None:
        OUTPUT_FILE_DIR.mkdir(parents=True, exist_ok=True)

    async def run_still_snapshot(self, filepath):
        cmd = f"ffmpeg -y -r 1 -i {self.rtsp_uri} -vf 'scale=720:-1' -vsync vfr -r 1 -vframes 1 {filepath}"
        return await self.run_cmd(cmd)

    async def run_video_snapshot(self, filepath, snapshot_length, fps, scale):
        # possible alternative, allegedly h265 is the "new" best high-compression format.
        # ffmpeg -y -rtsp_transport tcp -i rtsp://10.144.239.221:554/s0 -vf
        # scale=420:-1 -r 10 -t 6 -vcodec libx265 -tag:v hvc1 -c:a aac output.mp4
        cmd = f"ffmpeg -y -rtsp_transport tcp -i {self.rtsp_uri} -vf 'fps={fps},scale={scale}," \
              f"format=yuv420p,pad=ceil(iw/2)*2:ceil(ih/2)*2' -t {snapshot_length} -c:v libx264 -c:a aac {filepath}"
        return await self.run_cmd(cmd)

    async def run_cmd(self, cmd):
        self.ensure_output_dir()
        log.info(f"running cmd: {cmd}")
        proc = await asyncio.create_subprocess_shell(cmd)
        await proc.communicate()

    async def on_control_message(self, data):
        if data.get("action") == "power_on":
            await self.power_manager.acquire_for(self.rtsp_uri, self.camera_power_timeout)  # 15 minutes

    def close(self):
        pass


class DahuaCamera(Camera):
    def __init__(
            self, name, display_name, rtsp_uri, power_timeout, username, password, address, rtsp_port, control_port,
            object_detection, control_enabled, dda_iface: device_agent_iface, power_manager
    ):
        super().__init__(name, display_name, rtsp_uri, power_timeout, dda_iface, power_manager)

        self.object_detection = object_detection
        self.control_enabled = control_enabled

        self.username = username
        self.password = password
        self.address = address
        self.rtsp_port = rtsp_port
        self.control_port = control_port

        self.completed_tasks = []

        self.stream_events_task = None
        self.client: DahuaClient = None

    def close(self):
        if self.stream_events_task:
            self.stream_events_task.cancel()

    def configure_detect_alerts(self, ui_cmds_payload, name, enabled):
        key = f"{self.name}_{name}_detect"
        if enabled and key not in ui_cmds_payload["cmds"]:
            # default to no alerts for users.
            return {key: False}
        elif enabled is False and key in ui_cmds_payload["cmds"]:
            # get rid of this field from ui_cmds so we don't display to user.
            return {key: None}
        return {}

    async def get_ui_payload(self, force_allow_absolute: bool = False):
        return {}

    async def sync_ui(self, force_allow_absolute: bool = False, payload_extra=None):
        # fixme: this relies on the DODGY notion that the camera UI
        #  component is in a submodule called _liveview_submodule.
        #  need to fix this when merging the live view into camera UI.
        payload = await self.get_ui_payload(force_allow_absolute=force_allow_absolute)
        if not payload:
            return
        if payload_extra:
            payload.update(payload_extra)

        to_send = {
            "cmds": { f"{self.name}": payload }
        }
        log.info(f"syncing ui: {to_send}")
        await self.dda_iface.publish_to_channel("ui_cmds", json.dumps(to_send))

    async def setup(self):
        human = "human" in self.object_detection
        vehicle = "vehicle" in self.object_detection

        self.client = DahuaClient(
            self.username, self.password, self.address, self.control_port, self.rtsp_port, aiohttp.ClientSession()
        )
        status = await self.client.get_status()
        if not status:
            return False

        ui_cmds = await self.dda_iface.get_channel_aggregate("ui_cmds")
        print(ui_cmds, type(ui_cmds))
        if isinstance(ui_cmds, str):
            ui_cmds = json.loads(ui_cmds)

        to_send = {}
        to_send.update(self.configure_detect_alerts(ui_cmds, "human", human))
        to_send.update(self.configure_detect_alerts(ui_cmds, "vehicle", vehicle))
        if to_send:
            await self.dda_iface.publish_to_channel("ui_cmds", json.dumps({"cmds": to_send}))
        await self.sync_ui()  # sync ui_state

        if human or vehicle:
            log.info(f"Starting motion detection for camera {self.name}: {self.object_detection}")
            await self.client.enable_smart_motion_detection(human=human, vehicle=vehicle)
            events = ["SmartMotionHuman", "SmartMotionVehicle"]
            self.stream_events_task = asyncio.create_task(self.client.stream_events(self.on_cam_event, events))

        return True

    @classmethod
    def from_config(cls, config, dda_iface, power_manager):
        match = URI_MATCH.match(config["URI"])
        # prefer config, fallback to parsing the URI.
        username = get_or_fallback_to_match(config, match, "USERNAME", "username")
        password = get_or_fallback_to_match(config, match, "PASSWORD", "password")
        address = get_or_fallback_to_match(config, match, "ADDRESS", "address")
        rtsp_port = get_or_fallback_to_match(config, match, "RTSP_PORT", "port")
        control_port = config.get("CONTROL_PORT", 80)
        power_timeout = config.get("POWER_TIMEOUT", DEFAULT_POWER_TIMEOUT)

        return cls(
            config["NAME"],
            config["DISPLAY_NAME"],
            config["URI"],
            power_timeout,
            username,
            password,
            address,
            rtsp_port,
            control_port,
            config.get("OBJECT_DETECTION"),
            config.get("CONTROL_ENABLED"),
            dda_iface,
            power_manager,
        )

    async def get_snapshot(self, snapshot_type, length: int = 4, fps: int = DEFAULT_FPS, scale: str = DEFAULT_SCALE):
        log.info(f"Getting snapshot for camera {self.name}, type: {snapshot_type}, length: {length}")
        if snapshot_type == "mp4":
            # todo: use the in-built recording of camera...
            task_id = str(uuid.uuid4())
            fp = self.get_output_filepath(task_id, snapshot_type)

            fps = fps or DEFAULT_FPS
            scale = scale or DEFAULT_SCALE
            await self.run_video_snapshot(fp, length, fps, scale)

            try:
                with open(fp, 'rb') as f:
                    return base64.b64encode(f.read()).decode('utf-8')
            except Exception as e:
                logging.exception(f"get_snapshot: {str(e)}", exc_info=e)
                return None

        else:
            snap = await self.client.get_snapshot()
            # we need to do a bit of compression because normal images are ~255kB,
            # we have a 128kB max limit on the websocket. by reducing the quality to 10% we can get them down to ~50kB.
            proj = base64.b64encode(snap).decode()
            log.info(f"Original resolution image is {len(proj)/1000}kB.")
            if len(proj) > MAX_MESSAGE_SIZE:
                log.info("Downscaling original image to 10% quality.")
                im = Image.open(io.BytesIO(snap))
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=10)
                proj = base64.b64encode(buf.getbuffer()).decode()

            return proj

    async def on_cam_event(self, data: bytes, _):
        match = EVENT_MATCH.search(data.decode())
        if not (match and match.group("action") == "Start"):
            return  # this will also ignore heartbeat events

        data = json.loads(match.group("data"))

        snapshot = await self.get_snapshot("jpg")
        if not snapshot:
            return

        await self.publish_snapshot(snapshot, "jpg")

        ui_cmds = await self.dda_iface.get_channel_aggregate("ui_cmds")
        if not ui_cmds:
            return
        if isinstance(ui_cmds, str):
            ui_cmds = json.loads(ui_cmds)

        print(data, match.group("code"), match.group("action"))
        if match.group("code") == "SmartMotionHuman" and ui_cmds["cmds"].get(f"{self.name}_human_detect") is True:
            await self.dda_iface.publish_to_channel("significantEvent", f"{self.display_name} has detected a person.")
            log.info(f"Human Detected, {data}")
        elif match.group("code") == "SmartMotionVehicle" and ui_cmds["cmds"].get(f"{self.name}_vehicle_detect") is True:
            await self.dda_iface.publish_to_channel("significantEvent", f"{self.display_name} has detected a vehicle.")
            log.info(f"Vehicle Detected, {data}")

    def check_control_message(self, data):
        if not (self.control_enabled or self.client):
            return False

        try:
            task_id = data["task_id"]
        except (KeyError, TypeError):
            log.info("No task_id in control message. Skipping...")
            return False

        if task_id in self.completed_tasks:
            return False

        self.completed_tasks.append(data["task_id"])
        return True


class DahuaPTZCamera(DahuaCamera):
    HORIZONTAL_RANGE = (0, 360)
    VERTICAL_RANGE = (-15, 90)
    ZOOM_RANGE = (100, 2500)

    def __init__(self, *args, **kwargs):
        self.last_position = None
        self.last_absolute_control = True
        super().__init__(*args, **kwargs)

    @staticmethod
    def normalise(value, actual_range, desired_range):
        prop = desired_range[0] + \
                (value - actual_range[0]) * (desired_range[1] - desired_range[0]) / (actual_range[1] - actual_range[0])
        return max(min(prop, desired_range[1]), desired_range[0])

    def validate_value(self, value, min_val, max_val, new_min, new_max):
        value = max(min(value, max_val), min_val)
        if new_min <= value <= new_max:
            return value

        return self.normalise(value, (min_val, max_val), (new_min, new_max))

    def normalise_position(self, x, y, zoom):
        if 0 <= x < 180:
            x = x / 180
        elif 180 < x <= 360:
            x = (x - 360) / 180

        if -180 <= y <= 180:
            y = y / -180

        return x, y, self.normalise(zoom, self.ZOOM_RANGE, (0, 1))
        # return self.normalise(x, self.HORIZONTAL_RANGE, (0, 1)), \
        #        self.normalise(y, self.VERTICAL_RANGE, (0, 1)), \
        #        self.normalise(zoom, self.ZOOM_RANGE, (-1, 1))

    async def get_ui_payload(self, force_allow_absolute: bool = False):
        presets = await self.client.get_presets(fetch=True)
        x, y, z = await self.get_position(fetch=True)
        log.info(f"syncing position: x: {x}, y: {y}, z: {z}")
        self.last_absolute_control = True
        return {
            f"presets": list(presets.keys()),
            f"cam_position": {
                "pan": x, "tilt": y, "zoom": z
            },
            "allow_absolute_position": True
        }

    async def set_absolute_control_disabled(self):
        if self.last_absolute_control is False:
            return

        self.last_absolute_control = False
        await self.sync_ui()

    async def get_position(self, fetch: bool = False):
        if fetch is False and self.last_position is not None:
            return self.last_position

        pos = await self.client.get_ptz_position()
        normalised = self.normalise_position(*pos)
        self.last_position = normalised
        return normalised

    async def check_for_move_complete(self):
        retries = 0
        status = None
        while status != "Idle" and retries < 30:
            data = await self.client.get_ptz_status()
            status = data.get("status.MoveStatus")
            retries += 1
            await asyncio.sleep(0.1)

        await self.sync_ui()

    async def on_control_message(self, data):
        # check for power on message
        await super().on_control_message(data)

        if not self.check_control_message(data):
            return

        log.info(f"Executing control command for camera {self.name}: {data}")

        action = data.get("action", "")
        amount = data.get("value")
        if amount is None and action not in ("stop", "sync_ui"):
            return

        if action == "stop":
            await self.client.stop_ptz()
            await self.check_for_move_complete()
        elif action == "zoom":
            x, y, z = await self.get_position(fetch=True)
            z = self.normalise(amount, (0, 100), (0, 1))
            await self.client.absolute_ptz(x, y, z)
            await self.check_for_move_complete()
        elif action == "pantilt_continuous":
            pan, tilt = amount.get("pan"), amount.get("tilt")
            pan = self.normalise(pan, (-1, 1), (-10, 10))
            tilt = self.normalise(tilt, (-1, 1), (-10, 10))
            # pan = self.validate_value(pan, -100, 100, -10, 10)
            # tilt = self.validate_value(tilt, -100, 100, -10, 10)
            log.info(f"pan-tilting: {pan}, {tilt}")
            await self.client.continuous_ptz(pan, tilt, 0, timeout=0.5)
            await self.set_absolute_control_disabled()
        elif action == "pantilt_absolute":
            pan, tilt = amount.get("pan"), amount.get("tilt")
            log.info(f"pan-tilting absolute: {pan}, {tilt}")
            curr_pos = await self.get_position()
            await self.client.absolute_ptz(pan, tilt, curr_pos[2])
            await self.check_for_move_complete()
        elif "incremental" in action:
            amount = self.validate_value(amount, -100, 100, -1, 1)
            log.info(f"incremental moving: {action}, {amount}")

            if action == "incremental_pan":
                await self.client.relative_ptz(amount, 0, 0)
            elif action == "incremental_tilt":
                await self.client.relative_ptz(0, amount, 0)
            elif action == "incremental_zoom":
                await self.client.relative_ptz(0, 0, amount)
        elif action == "goto_preset":
            log.info(f"moving to preset {amount}")
            await self.client.goto_preset(amount)
            await self.set_absolute_control_disabled()
            await self.check_for_move_complete()
            await self.sync_ui(payload_extra={"active_preset": amount})
        elif action == "create_preset":
            log.info(f"creating preset {amount}")
            await self.client.create_preset(amount)
            await self.sync_ui()
        elif action == "delete_preset":
            log.info(f"deleting preset {amount}")
            await self.client.delete_preset(amount)
            await self.sync_ui()
        elif action == "sync_ui":
            await self.sync_ui()


class DahuaFixedCamera(DahuaCamera):

    async def get_ui_payload(self, force_allow_absolute: bool = False):
        status = await self.client.get_focus_status()
        return {
            "cam_position": {"zoom": float(status["status.Zoom"])*100},
            "allow_absolute_position": force_allow_absolute or status["status.Status"] == "Normal"
        }

    async def check_for_zoom_complete(self):
        retries = 0
        status = None
        while status != "Normal" and retries < 30:
            data = await self.client.get_focus_status()
            status = data.get("status.Status")
            retries += 1
            await asyncio.sleep(0.1)

        await self.sync_ui()

    async def on_control_message(self, data):
        # check for power on message
        await super().on_control_message(data)

        if not self.check_control_message(data):
            return

        action = data.get("action")
        if action == "sync_ui":
            await self.sync_ui()
        elif action == "reset":
            await self.client.adjust_manual_zoom(zoom=-1, focus=-1)
            await self.check_for_zoom_complete()
            await self.sync_ui(force_allow_absolute=True)

        if data.get("action") != "zoom":
            return

        log.info(f"Executing control command for camera {self.name}: {data}")
        zoom = data.get("value", 0)
        if 1 < zoom < 100:
            zoom = zoom / 100
        else:
            zoom = max(min(zoom, 1), 0)
        zoom = round(zoom, 1)
        log.info(f"adjusting zoom: {zoom}")

        await self.client.adjust_manual_zoom(zoom)
        await asyncio.sleep(1)

        await self.check_for_zoom_complete()
        # await self.client.auto_focus()


class GenericRTSPCamera(Camera):

    async def get_snapshot(self, snapshot_type, length, fps: int = DEFAULT_FPS, scale: str = DEFAULT_SCALE):
        task_id = str(uuid.uuid4())
        filepath = self.get_output_filepath(task_id, snapshot_type)

        if snapshot_type == 'mp4':
            fps = fps or DEFAULT_FPS
            scale = scale or DEFAULT_SCALE
            await self.run_video_snapshot(filepath, length, fps, scale)
        else:
            await self.run_still_snapshot(filepath)

        try:
            with open(filepath, 'rb') as f:
                return base64.b64encode(f.read()).decode('utf-8')
        except Exception as e:
            logging.exception(f"get_snapshot: {str(e)}", exc_info=e)

    def close(self):
        pass
