from pathlib import Path

from pydoover import config


class CameraConfig(config.Schema):
    def __init__(self):
        self.display_name = config.String(
            "Camera Name", description="User friendly name for camera"
        )
        self.name = config.String("cam_name", description="Internal name for camera.")
        # self.uri = config.String("Camera URI", description="Camera URI in form of rtsp://username:password@address:port/channelName")
        self.type = config.Enum(
            "Camera Type",
            default="dahua_ptz",
            choices=["dahua_ptz", "dahua_fixed", "dahua_generic"],
        )

        self.username = config.String(
            "Camera Username", description="Username to login to camera control"
        )
        self.password = config.String(
            "Camera Password", description="Password to login to camera control"
        )
        self.address = config.String(
            "IP address", description="IP address of camera (e.g. 192.168.50.100)"
        )
        self.rtsp_port = config.Integer(
            "RTSP Port", description="Port of RTSP feed on camera", default=554
        )
        self.rtsp_channel = config.String(
            "RTSP Channel",
            description="RTSP channel name. On Dahua cameras this is usually 'live'.",
            default="live",
        )
        self.control_port = config.Integer(
            "Control Port", description="Port of control page on camera", default=80
        )

        self.power_pin = config.Integer(
            "Power Pin",
            description="Digital Output pin that controls power to camera circuit. Defaults to None (no power control).",
            default=None,
        )
        self.power_timeout = config.Integer(
            "Power Timeout", description="Power Timeout in seconds", default=60 * 15
        )
        self.wake_delay = config.Integer(
            "Wake Delay",
            description="Seconds for camera to boot before requesting a snapshot.",
            default=5,
        )

        self.remote_component_url = config.String(
            "Remote Component URL",
            description="URL for live view component. Leave blank to disable live view.",
            default="https://getdoover.github.io/cameras/HLSLiveView.js",
        )
        self.remote_component_name = config.String(
            "Remote Component Name",
            description="Name of live view component",
            default="Live View",
        )

        self.object_detection = config.Array(
            "Object Detection",
            description="Objects to detect. Leave blank to disable object detection.",
            element=config.Enum("Object", choices=["Person", "Vehicle"]),
        )
        self.control_enabled = config.Boolean(
            "Control Enabled",
            description="Allow control (movement) of PTZ cameras.",
            default=True,
        )

        self.snapshot_period = config.Integer(
            "Snapshot Period",
            description="Snapshot period in seconds",
            default=60 * 60 * 4,
        )
        self.snapshot_mode = config.Enum(
            "Snapshot Mode",
            description="Video format",
            default="mp4",
            choices=["mp4", "jpg"],
        )
        self.snapshot_secs = config.Integer(
            "Snapshot Duration", description="Duration of snapshot", default=6
        )
        self.snapshot_fps = config.Integer(
            "Snapshot FPS", description="FPS of snapshot", default=5
        )
        self.snapshot_scale = config.String(
            "Snapshot Scale", description="Scale of snapshot", default="360:-1"
        )
        self.position = config.Integer(
            "Position",
            description="Position of the application in the UI",
            default=50,
            hidden=True,
        )

    @property
    def rtsp_uri(self) -> str:
        if self.username.value or self.password.value:
            return f"rtsp://{self.username.value}:{self.password.value}@{self.address.value}:{self.rtsp_port.value}/{self.rtsp_channel.value}"
        return f"rtsp://{self.address.value}:{self.rtsp_port.value}/{self.rtsp_channel.value}"


if __name__ == "__main__":
    config = CameraConfig()
    config.export(Path("../doover_config.json"), "dahua_camera")
