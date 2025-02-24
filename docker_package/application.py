import asyncio
import time

from pydoover.docker import app_base, run_app

from pydoover import ui


class Application(app_base):
    system_voltage: ui.NumericVariable
    system_temp: ui.NumericVariable
    online_since: ui.DateTimeVariable

    last_loop_start: time.time
    became_online_time: time.time

    async def setup(self):
        self.counter = 0
        self.buffer = b""

        ui_manager = self.get_ui_manager()
        self.ui_manager.add_children(ui.AlertStream("significantEvent", "Send me notifications"))
        super().setup()
        self.get_platform_iface()

        config = self.get_config("camera_config")
        for cam in config["CAMERAS"].values():
            comp = ui.RemoteComponent(
                cam["NAME"],
                cam["DISPLAY_NAME"],
                component_url="https://raw.githubusercontent.com/getdoover/cameras/refs/heads/main/camera_ui/assets/DahuaCameraUi.js",
                address=cam["ADDRESS"],
                port=cam["RTSP_PORT"],
                rtsp_uri=cam["URI"],
                cam_type=cam["TYPE"],
            )
            self.ui_manager.add_children(ui.Submodule(cam['NAME'] + "_liveview_submodule", cam["DISPLAY_NAME"] + " Live View", children=[comp]))

        print(config)
        # for cam in self.config
        # self.ui_manager.add_children(ui.RemoteComponent(""))

        self.last_loop_start = time.time()
        self.became_online_time = None

    async def main_loop(self):
        super().main_loop()
        if not self.ui_manager.has_been_connected():
            print("Cycling - Waiting to ensure connectivity to the cloud")
            await asyncio.sleep(1)
            return

        self.last_loop_start = time.time()


if __name__ == '__main__':
    run_app(Application())
