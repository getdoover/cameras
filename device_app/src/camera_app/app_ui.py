from pydoover import ui

from camera_app.app_config import CameraConfig, CameraType


class CameraUI:
    def __init__(self, config: CameraConfig, app_key: str, app_display_name: str):
        self.config = config

        self.history = ui.CameraHistory(app_key)

        self.live_view = ui.CameraLiveView(
            app_key,
            app_key,
            self.config.control_enabled.value,
            name=f"{app_key}_lv",
            display_name="Live View",
        )

        if CameraType(self.config.type.value) is CameraType.hikvision_thermal:
            self.thermal_live_view = ui.CameraLiveView(
                app_key,
                f"{app_key}_thermal",
                self.config.control_enabled.value,
                name=f"{app_key}_thermal_liveview",
                display_name="Live View (Thermal)",
            )
            live_views = [self.live_view, self.thermal_live_view]
        else:
            live_views = [self.live_view]

        self.vehicle_detection = ui.Switch(
            "Alert me for Vehicle Motion",
            icon="fa-car",
            hidden=not config.vehicle_detect_enabled,
        )
        self.human_detection = ui.Switch(
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

        self.tab_container = ui.TabContainer(
            children=[self.history, *live_views, container],
            name="tabs",
            display_name="Tabs",
        )

    def fetch(self):
        return (self.tab_container,)

    def update_presets(self, presets: list[str], active_preset):
        self.live_view.presets = presets
        self.live_view.active_preset = active_preset
