from pydoover import ui

from camera_app.app_config import CameraConfig, CameraType


class CameraLiveView(ui.Element):
    type = "uiCameraLiveView"

    def __init__(
        self,
        camera_name: str,
        stream_name: str,
        allow_ptz_control: bool,
        display_name: str = "Live View",
        name: str = "History",
        **kwargs,
    ):
        self.stream_name = stream_name
        self.camera_name = camera_name
        self.allow_ptz_control = allow_ptz_control
        self.presets = []
        self.active_preset = None
        super().__init__(name=name, display_name=display_name, **kwargs)

    def to_dict(self):
        res = super().to_dict()
        res["cameraName"] = self.camera_name
        res["streamName"] = self.stream_name
        res["ptzControl"] = self.allow_ptz_control
        res["presets"] = self.presets
        res["activePreset"] = self.active_preset
        return res


class CameraHistory(ui.Element):
    type = "uiCameraHistory"

    def __init__(
        self,
        camera_name: str,
        display_name: str = "History",
        name: str = "history",
        **kwargs,
    ):
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
        super().__init__(
            name=name.replace(" ", "_").lower(), display_name=name, **kwargs
        )
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

        self.history = CameraHistory(app_key)

        self.live_view = CameraLiveView(
            app_key,
            app_key,
            self.config.control_enabled.value,
            name=f"{app_key}_lv",
            display_name="Live View",
        )

        if CameraType(self.config.type.value) is CameraType.hikvision_thermal:
            self.thermal_live_view = CameraLiveView(
                app_key,
                f"{app_key}_thermal",
                self.config.control_enabled.value,
                name=f"{app_key}_thermal_liveview",
                display_name="Live View (Thermal)",
            )
            live_views = [self.live_view, self.thermal_live_view]
        else:
            live_views = [self.live_view]

        self.vehicle_detection = Switch(
            "Alert me for Vehicle Motion",
            icon="fa-car",
            hidden=not config.vehicle_detect_enabled,
        )
        self.human_detection = Switch(
            "Alert me for Human Motion",
            icon="fa-user",
            hidden=not config.human_detect_enabled,
        )
        container = ui.Container(
            children=[self.vehicle_detection, self.human_detection],
            name="detection",
            display_name="Object Detection",
            hidden=not (config.vehicle_detect_enabled or config.human_detect_enabled),
        )

        self.tab_container = TabContainer(
            children=[self.history, *live_views, container],
            name="tabs",
            display_name="Tabs",
        )

    def fetch(self):
        return (self.tab_container,)

    def update_presets(self, presets: list[str], active_preset):
        self.live_view.presets = presets
        self.live_view.active_preset = active_preset
