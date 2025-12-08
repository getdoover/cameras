from enum import Enum
from pathlib import Path

from pydoover import config


class CameraConnectionConfig(config.Object):
    def __init__(self):
        super().__init__("Camera Connection Config")
        self.username = config.String(
            "Camera Username",
            description="Username to login to camera control",
            default=None,
        )
        self.password = config.String(
            "Camera Password",
            description="Password to login to camera control",
            default=None,
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


class CameraPowerConfig(config.Object):
    def __init__(self):
        super().__init__("Camera Power Config")

        self.enabled = config.Boolean(
            "Enabled",
            description="Whether power control is enabled for this camera",
            default=False,
        )

        self.pin = config.Integer(
            "Power Pin",
            description="Digital Output pin that controls power to camera circuit.",
            default=None,
        )
        self.timeout = config.Integer(
            "Off After",
            description="Number of seconds after which the camera will be powered off",
            default=60 * 15,
        )
        self.wake_delay = config.Integer(
            "Wake Delay",
            description="Seconds for camera to boot before requesting a snapshot.",
            default=5,
        )


class CameraRemoteComponentConfig(config.Object):
    def __init__(self):
        super().__init__("Camera Remote Component Config")

        self.enabled = config.Boolean(
            "Enabled",
            description="Whether remote component is enabled for this camera",
            default=True,
        )
        self.url = config.String(
            "URL",
            description="URL for live view component. Leave blank to disable live view.",
            default="https://getdoover.github.io/cameras/HLSLiveView.js",
        )
        self.name = config.String(
            "Name",
            description="Name of live view component",
            default="Live View",
        )


class CameraSnapshotConfig(config.Object):
    def __init__(self):
        super().__init__("Camera Snapshot Config")

        self.enabled = config.Boolean("Enabled", default=True)
        self.period = config.Integer(
            "Period",
            description="Number of seconds between snapshots",
            default=60 * 60 * 4,
        )
        self.mode = config.Enum(
            "Mode",
            description="Video format",
            default="mp4",
            choices=["mp4", "jpg"],
        )
        self.secs = config.Integer(
            "Duration", description="Duration of snapshot", default=6
        )
        self.fps = config.Integer("FPS", description="FPS of snapshot", default=5)
        self.scale = config.String(
            "Scale", description="Scale of snapshot", default="360:-1"
        )


class CameraRTSPServerConfig(config.Object):
    def __init__(self):
        super().__init__("Camera RTSP Server Config")

        self.enabled = config.Boolean("Enabled", default=True)
        self.address = config.String(
            "Address",
            description="Address of RTSP server",
            default="http://localhost:8083",
        )
        self.username = config.String("Username", default="demo")
        self.password = config.String("Password", default="demo")


class CameraType(Enum):
    dahua_ptz = "Dahua (PTZ)"
    dahua_fixed = "Dahua (Fixed)"
    dahua_generic = "Dahua (Generic)"
    unifi_generic = "UniFi (Generic)"
    generic_ip = "Generic IP"


class ObjectDetectionType(Enum):
    person = "Person"
    vehicle = "Vehicle"


class CameraConfig(config.Schema):
    def __init__(self):
        self.type = config.Enum(
            "Camera Type",
            default=CameraType.dahua_generic,
            choices=CameraType,
        )

        self.connection = CameraConnectionConfig()
        self.power = CameraPowerConfig()
        self.remote_component = CameraRemoteComponentConfig()
        self.snapshot = CameraSnapshotConfig()
        self.rtsp_server = CameraRTSPServerConfig()

        self.object_detection = config.Array(
            "Object Detection",
            description="Objects to detect. Leave blank to disable object detection.",
            element=config.Enum(
                "Object",
                choices=ObjectDetectionType,
                default=ObjectDetectionType.person,
            ),
            unique_items=True,
        )
        self.control_enabled = config.Boolean(
            "Control Enabled",
            description="Allow control (movement) of PTZ cameras.",
            default=True,
        )

    @property
    def rtsp_uri(self) -> str:
        if self.connection.username.value or self.connection.password.value:
            return f"rtsp://{self.connection.username.value}:{self.connection.password.value}@{self.connection.address.value}:{self.connection.rtsp_port.value}/{self.connection.rtsp_channel.value}"
        return f"rtsp://{self.connection.address.value}:{self.connection.rtsp_port.value}/{self.connection.rtsp_channel.value}"

    @property
    def human_detect_enabled(self):
        return any(ObjectDetectionType(e.value) is ObjectDetectionType.person for e in self.object_detection.elements)

    @property
    def vehicle_detect_enabled(self):
        return any(ObjectDetectionType(e.value) is ObjectDetectionType.vehicle for e in self.object_detection.elements)


def export():
    CameraConfig().export(
        Path(__file__).parents[2] / "doover_config.json", "doover_camera"
    )
