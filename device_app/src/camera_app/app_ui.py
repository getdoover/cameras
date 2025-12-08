from pydoover import ui

from camera_app.app_config import CameraConfig


class CameraUI:
    def __init__(self, config: CameraConfig):
        self.config = config
        name = self.config.name.value.replace(" ", "_").lower()

        if self.config.remote_component.enabled.value is False:
            self.camera = ui.Camera(name, self.config.name.value, self.config.rtsp_uri)
        else:
            ui_liveview = ui.RemoteComponent(
                name=f"{name}_liveview",
                display_name=f"{self.config.name.value} {self.config.remote_component.name.value}",
                cam_name=name,
                component_url=self.config.remote_component.url.value,
                address=self.config.connection.address.value,
                port=self.config.connection.rtsp_port.value,
                rtsp_uri=self.config.rtsp_uri,
                cam_type=self.config.type.value,
                position=52,
            )

            # Set the Display Name to blank to avoid title in submodule
            original_cam_history = ui.CameraHistory(
                name, "", self.config.rtsp_uri, position=51
            )

            self.camera = ui.Camera(
                name,
                self.config.name.value,
                self.config.rtsp_uri,
                children=[original_cam_history, ui_liveview],
                position=50,
            )

    def fetch(self):
        return (self.camera,)
