import asyncio
import base64
import io
import json
import logging
import uuid
import pathlib
import re
from typing import Optional

import aiohttp

from PIL import Image

from pydoover.docker import device_agent_iface
from pydoover import ui

from dahua import DahuaClient
from power_management import CameraPowerManagement


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



class CameraConfig:
    def __init__(self, data):
        self.name = data["NAME"]
        self.display_name = data["DISPLAY_NAME"]
        self.uri = data["URI"]
        self.type = data.get("TYPE", "generic")

        self.rtsp_uri = data.get("RTSP_URI")

        match = URI_MATCH.match(data["URI"])
        # prefer config, fallback to parsing the URI.
        self.username = self.get_or_fallback_to_match(data, match, "USERNAME", "username")
        self.password = self.get_or_fallback_to_match(data, match, "PASSWORD", "password")
        self.address = self.get_or_fallback_to_match(data, match, "ADDRESS", "address")
        self.rtsp_port = self.get_or_fallback_to_match(data, match, "RTSP_PORT", "port")

        self.control_port = data.get("CONTROL_PORT", 80)
        self.power_timeout = data.get("POWER_TIMEOUT", DEFAULT_POWER_TIMEOUT)

        self.remote_component_url = data.get("REMOTE_COMPONENT_URL")
        self.remote_component_name = data.get("REMOTE_COMPONENT_NAME")

        self.object_detection = data.get("OBJECT_DETECTION")
        self.control_enabled = data.get("CONTROL_ENABLED")

        self.snapshot_period = data.get("SNAPSHOT_PERIOD")
        self.snapshot_mode = data.get("SNAPSHOT_MODE") or "mp4"
        self.snapshot_secs = data.get("SNAPSHOT_SECS") or 6
        self.snapshot_fps = data.get("SNAPSHOT_FPS")
        self.snapshot_scale = data.get("SNAPSHOT_SCALE")


    @staticmethod
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
    def __init__(self, config: CameraConfig, dda_iface, power_manager: CameraPowerManagement):
        self.name = config.name

        self.config = config
        self.dda_iface = dda_iface
        self.power_manager = power_manager

    @classmethod
    def from_config(cls, config, dda_iface, power_manager):
        return cls(config, dda_iface, power_manager)

    async def setup(self):
        pass

    async def get_snapshot(self, mode: str = None):
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
        cmd = f"ffmpeg -y -r 1 -i {self.config.rtsp_uri} -vf 'scale=720:-1' -vsync vfr -r 1 -vframes 1 {filepath}"
        return await self.run_cmd(cmd)

    async def run_video_snapshot(self, filepath):
        # possible alternative, allegedly h265 is the "new" best high-compression format.
        # ffmpeg -y -rtsp_transport tcp -i rtsp://10.144.239.221:554/s0 -vf
        # scale=420:-1 -r 10 -t 6 -vcodec libx265 -tag:v hvc1 -c:a aac output.mp4
        cmd = f"ffmpeg -y -rtsp_transport tcp -i {self.config.rtsp_uri} -vf 'fps={self.config.snapshot_fps},scale={self.config.snapshot_scale}," \
              f"format=yuv420p,pad=ceil(iw/2)*2:ceil(ih/2)*2' -t {self.config.snapshot_secs} -c:v libx264 -c:a aac {filepath}"
        return await self.run_cmd(cmd)

    async def run_cmd(self, cmd):
        self.ensure_output_dir()
        log.info(f"running cmd: {cmd}")
        proc = await asyncio.create_subprocess_shell(cmd)
        await proc.communicate()

    async def on_control_message(self, data):
        if data.get("action") == "power_on":
            await self.power_manager.acquire_for(self.config.rtsp_uri, self.config.power_timeout)  # 15 minutes

    def fetch_ui_elements(self):
        if self.config.remote_component_url is None:
            return ui.Camera(self.config.name, self.config.display_name, self.config.uri)

        liveview_element_name = f"{self.name}_liveview"
        liveview_display_name = f"{self.config.display_name} Liveview"
        ui_liveview = ui.RemoteComponent(
            name=liveview_element_name,
            display_name=liveview_display_name,
            cam_name=self.name,
            component_url=self.config.remote_component_url,
            address=self.config.address,
            port=self.config.rtsp_port,
            rtsp_uri=self.config.rtsp_uri,
            cam_type=self.config.type,
        )
        # pretty hacky, but this basically tells the UI to never overwrite these fields since
        # we manage them in the camera interface. Possibly not the right way of going about it?
        # ui_liveview._retain_fields = ("presets", "active_preset", "cam_position", "allow_absolute_position")

        ## Set the Dispaly Name to blank to avoid title in submodule
        original_cam_history = ui.CameraHistory(self.config.name, "", self.config.uri)

        yield ui.Camera(self.config.name, self.config.display_name, self.config.uri, children=[original_cam_history, ui_liveview])


class DahuaCamera(Camera):
    def __init__(
            self, config, dda_iface: device_agent_iface, power_manager
    ):
        super().__init__(config, dda_iface, power_manager)

        self.completed_tasks = []

        self.stream_events_task = None
        self.client: Optional[DahuaClient] = None

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
        human = "human" in self.config.object_detection
        vehicle = "vehicle" in self.config.object_detection

        self.client = DahuaClient(
            self.config.username,
            self.config.password,
            self.config.address,
            self.config.control_port,
            self.config.rtsp_port,
            aiohttp.ClientSession()
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
            log.info(f"Starting motion detection for camera {self.name}: {self.config.object_detection}")
            await self.client.enable_smart_motion_detection(human=human, vehicle=vehicle)
            events = ["SmartMotionHuman", "SmartMotionVehicle"]
            self.stream_events_task = asyncio.create_task(self.client.stream_events(self.on_cam_event, events))

        return True

    async def get_snapshot(self, mode: str = None):
        mode = mode or self.config.snapshot_mode

        log.info(f"Getting snapshot for camera {self.name}, type: {mode}, length: {self.config.snapshot_secs}")
        if mode == "mp4":
            # todo: use the in-built recording of camera...
            task_id = str(uuid.uuid4())
            fp = self.get_output_filepath(task_id, mode)

            await self.run_video_snapshot(fp)

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
            await self.dda_iface.publish_to_channel("significantEvent", f"{self.config.display_name} has detected a person.")
            log.info(f"Human Detected, {data}")
        elif match.group("code") == "SmartMotionVehicle" and ui_cmds["cmds"].get(f"{self.name}_vehicle_detect") is True:
            await self.dda_iface.publish_to_channel("significantEvent", f"{self.config.display_name} has detected a vehicle.")
            log.info(f"Vehicle Detected, {data}")

    def check_control_message(self, data):
        if not (self.config.control_enabled or self.client):
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

    async def get_snapshot(self, mode: str = None):
        mode = mode or self.config.snapshot_mode

        task_id = str(uuid.uuid4())
        filepath = self.get_output_filepath(task_id, mode)

        if mode == 'mp4':
            await self.run_video_snapshot(filepath)
        else:
            await self.run_still_snapshot(filepath)

        try:
            with open(filepath, 'rb') as f:
                return base64.b64encode(f.read()).decode('utf-8')
        except Exception as e:
            logging.exception(f"get_snapshot: {str(e)}", exc_info=e)

    def close(self):
        pass
