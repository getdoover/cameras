from pydoover import ui

from camera_app.app_config import CameraConfig


class PTZCameraLiveView(ui.Element):
    type = "uiCameraLiveView"

    def __init__(self, camera_name: str, **kwargs):
        self.camera_name = camera_name
        self.presets = []
        self.active_preset = None
        super().__init__(**kwargs)

    def to_dict(self):
        res = super().to_dict()
        res["cameraName"] = self.camera_name
        res["ptzControl"] = True
        res["presets"] = self.presets
        res["activePreset"] = self.active_preset
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

        self.live_view = PTZCameraLiveView(
            app_key, name=f"{app_key}_lv", display_name=app_display_name
        )

    def fetch(self):
        return (self.camera, self.live_view)

    def update_presets(self, presets: list[str], active_preset):
        self.live_view.presets = presets
        self.live_view.active_preset = active_preset
