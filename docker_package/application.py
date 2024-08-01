import asyncio
import base64
import json
import logging
import io
import random
import re
import time

import aiohttp

from PIL import Image
from pydoover.docker import app_base, run_app

from pydoover import ui

from dahua import DahuaClient

EVENT_MATCH = re.compile(r"(?P<boundary>.*)\r\n"
                         r"Content-Type: (?P<content>.*)\r\n"
                         r"Content-Length: (?P<content_length>\d*)\r\n\r\n"
                         r"Code=(?P<code>.*);action=(?P<action>.*);index=(?P<index>.*);data=(?P<data>.*)", re.DOTALL)

SNAPSHOT_MATCH = re.compile(b"Content-Type: (?P<content>.*)\r\n"
                            b"Content-Length:(?P<content_length>.*)\r\n\r\n"
                            b"(?P<data>.*)", re.DOTALL)


class Application(app_base):
    system_voltage: ui.NumericVariable
    system_temp: ui.NumericVariable
    online_since: ui.DateTimeVariable

    last_loop_start: time.time
    became_online_time: time.time

    def on_zoom(self, new_val):
        print(new_val)
        self.should_zoom = True
        if 1 < new_val < 100:
            new_val = new_val / 100
        else:
            new_val = max(min(new_val, 1), 0)

        self.zoom = new_val

    def on_pt(self, new_val):
        if new_val is None or new_val == "stop":
            self.pt_action = "stop"
        elif new_val.upper() == "FORWARD":
            self.pt_action = "Up"
        elif new_val.upper() == "BACKWARD":
            self.pt_action = "Down"
        elif new_val.upper() == "RIGHT":
            self.pt_action = "Right"
        elif new_val.upper() == "LEFT":
            self.pt_action = "Left"
        elif new_val.upper() == "ZOOMIN":
            self.pt_action = "ZoomTele"
        elif new_val.upper() == "ZOOMOUT":
            self.pt_action = "ZoomWide"

        self.should_pt = True
        print("hi", new_val)

    def on_cam_notif_config(self, new_val):
        self.human_notif = "Human" in new_val
        self.vehicle_notif = "Vehicle" in new_val
        self.should_change_notif = True

    async def publish_snapshot(self):
        snap = await self.client.get_snapshot(1)

        # we need to do a bit of compression because normal images are ~255kB,
        # we have a 128kB max limit on the websocket. by reducing the quality to 10% we can get them down to ~50kB.
        im = Image.open(io.BytesIO(snap))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=10)

        message = json.dumps({
            'camera_name': "ptz_cam",
            'output': base64.b64encode(buf.getbuffer()).decode(),
            'output_type': "jpg",
        })
        print(len(message))
        self.publish_to_channel("ptz_cam", message)

    async def on_cam_event(self, data: bytes, channel):
        match = EVENT_MATCH.search(data.decode())
        if not (match and match.group("action") == "Start"):
            return  # this will also ignore heartbeat events

        data = json.loads(match.group("data"))

        await self.publish_snapshot()
        # print(data, match.group("code"), match.group("action"))
        if match.group("code") == "SmartMotionHuman":
            self.publish_to_channel("significantEvent", "Camera has detected a person.")
            print("Human Detected", data)
        elif match.group("code") == "SmartMotionVehicle":
            self.publish_to_channel("significantEvent", "Camera has detected a vehicle.")
            print("Vehicle Detected", data)

    async def on_cam_snapshot(self, data, channel):
        try:
            # if b"--myboundary" in data:
            #     self.exp_length = int(data.encode().split("Content-Length: ")[1].split("\r\n")[0])

            # with open("test.txt", "wb") as fp:
            #     fp.write(data)
            # print(data)
            match = SNAPSHOT_MATCH.search(data)
            if not match:
                return

            if match.group("content").strip() != b"image/jpeg":
                return

            self.counter += 1
            data = match.group("data")
            with open(f"snapshot_{self.counter}.jpeg", "wb") as fp:
                fp.write(data)

            im = Image.open(io.BytesIO(data))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=10)

            message = json.dumps({
                'camera_name': "ptz_cam",
                'output': base64.b64encode(buf.getbuffer()).decode(),
                'output_type': "jpg",
            })
            print(len(message))
            self.publish_to_channel("ptz_cam", message)

            print(self.counter, len(data))
            print("snapshot")
            self.buffer = b""
        except Exception as e:
            print(e)


    async def setup(self):
        self.counter = 0
        self.buffer = b""

        super().setup()
        self.get_platform_iface()
        ui_manager = self.get_ui_manager()
        ui_manager.add_interaction(ui.HiddenValue("ptz_cam_zoom", callback=self.on_zoom))
        ui_manager.add_interaction(ui.HiddenValue("ptz_cam_pt", callback=self.on_pt))
        ui_manager.add_interaction(ui.HiddenValue("ptz_cam_notif", callback=self.on_cam_notif_config))
        self.ui_manager.add_children(ui.AlertStream("significantEvent", "Send me notifications"))

        self.last_loop_start = time.time()
        self.became_online_time = None
        self.should_zoom = False
        self.should_pt = False
        self.should_change_notif = False

        self.client = DahuaClient("admin", "19HandleyDrive", "192.168.0.102", 80, 554, session=aiohttp.ClientSession())
        logging.getLogger("root").setLevel(logging.WARN)

        await self.client.enable_smart_motion_detection(0)
        events = ["SmartMotionHuman", "SmartMotionVehicle"]
        self.stream_events_task = asyncio.create_task(self.client.stream_events(self.on_cam_event, events, 1))
        # self.stream_snapshots_task = asyncio.create_task(self.client.stream_snapshots(self.on_cam_snapshot, events, 1))

    async def main_loop(self):
        super().main_loop()
        # print(self.ui_manager.get_interaction("ptz_cam_zoom").current_value)

        # print("Main Loop")
        # await asyncio.sleep(5)
        if not self.ui_manager.has_been_connected():
            print("Cycling - Waiting to ensure connectivity to the cloud")
            await asyncio.sleep(1)
            return

        # print("Iterating...")
        # print("Last loop = " + str(time.time() - self.last_loop_start) + " seconds")
        self.last_loop_start = time.time()

        if self.should_zoom and False:
            self.should_zoom = False
            await self.client.adjust_focus_v1(1, self.zoom)

        if self.should_pt:
            print("pan-tilting", self.pt_action)
            self.should_pt = False
            if self.pt_action == "stop":
                await self.client.stop_ptz()
            else:
                # await self.client.stop_ptz()
                print("adjusting ptz")
                await self.client.adjust_ptz(self.pt_action, 1, 0 if "Zoom" in self.pt_action else 5)

        if self.should_change_notif:
            print(f"setting notifications, human: {self.human_notif}, vehicle: {self.vehicle_notif}")
            self.should_change_notif = False
            await self.client.enable_smart_motion_detection(0, self.human_notif, self.vehicle_notif)


if __name__ == '__main__':
    run_app(Application())
