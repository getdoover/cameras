from pydoover import ui

from camera_app.app_config import CameraConfig


class CameraLiveView(ui.Element):
    type = "uiCameraLiveView"

    def __init__(self, camera_name: str, display_name: str = "Live View", name: str = "History", **kwargs):
        self.camera_name = camera_name
        self.presets = []
        self.active_preset = None
        super().__init__(name=name, display_name=display_name, **kwargs)

    def to_dict(self):
        res = super().to_dict()
        res["cameraName"] = self.camera_name
        res["ptzControl"] = True
        res["presets"] = self.presets
        res["activePreset"] = self.active_preset
        return res


class CameraHistory(ui.Element):
    type = "uiCameraHistory"

    def __init__(self, camera_name: str, display_name: str = "History", name: str = "history", **kwargs):
        self.camera_name = camera_name
        self.presets = []
        self.active_preset = None
        super().__init__(name=name, display_name=display_name, **kwargs)

    def to_dict(self):
        res = super().to_dict()
        res["cameraName"] = self.camera_name
        res["ptzControl"] = True
        res["presets"] = self.presets
        res["activePreset"] = self.active_preset
        return res


class TabContainer(ui.Element):
    type = "uiTabs"

    def __init__(self, children: list[ui.Element], **kwargs):
        self.children = children
        super().__init__(**kwargs)

    def to_dict(self):
        res = super().to_dict()
        res["children"] = {c.name: c.to_dict() for c in self.children}
        return res

class Switch(ui.Interaction):
    type = "uiSwitch"

    def __init__(self, name: str, icon: str = None, colour: ui.Colour = None, **kwargs):
        super().__init__(name=name.replace(" ", "_").lower(), display_name=name, **kwargs)
        self.icon = icon
        self.colour = colour

    def to_dict(self):
        res = super().to_dict()
        if self.icon:
            res["icon"] = self.icon
        if self.colour:
            res["colour"] = self.colour
        return res

class CameraUI:
    def __init__(self, config: CameraConfig, app_key: str, app_display_name: str):
        self.config = config

        if self.config.remote_component.enabled.value is False:
            self.camera = ui.Camera(app_key, app_display_name, self.config.rtsp_uri)
        else:
            ui_liveview = ui.RemoteComponent(
                name=f"{app_key}_liveview",
                display_name=f"{app_display_name} {self.config.remote_component.name.value}",
                cam_name=app_key,
                component_url=self.config.remote_component.url.value,
                address=self.config.connection.address.value,
                port=self.config.connection.rtsp_port.value,
                rtsp_uri=self.config.rtsp_uri,
                cam_type=self.config.type.value,
                position=52,
            )

            # Set the Display Name to blank to avoid title in submodule
            original_cam_history = ui.CameraHistory(
                app_key, "", self.config.rtsp_uri, position=51
            )

            self.camera = ui.Camera(
                app_key,
                app_display_name,
                self.config.rtsp_uri,
                children=[original_cam_history, ui_liveview],
                position=50,
            )

        self.history = CameraHistory(app_key)

        self.live_view = CameraLiveView(
            app_key, name=f"{app_key}_lv", display_name="Live View"
        )

        self.vehicle_detection = Switch("Alert me for Vehicle Motion", icon="fa-car", hidden=not config.vehicle_detect_enabled)
        self.human_detection = Switch("Alert me for Human Motion", icon="fa-user", hidden=not config.human_detect_enabled)
        container = ui.Container(children=[self.vehicle_detection, self.human_detection], name="detection", display_name="Object Detection")

        self.tab_container = TabContainer(children=[self.history, self.live_view, container], name="tabs", display_name="Tabs")


    def fetch(self):
        return (self.tab_container, )

    def update_presets(self, presets: list[str], active_preset):
        self.live_view.presets = presets
        self.live_view.active_preset = active_preset
