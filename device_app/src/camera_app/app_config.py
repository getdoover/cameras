from enum import Enum
from pathlib import Path

from pydoover import config


class CameraThermalConfig(config.Object):
    def __init__(self):
        super().__init__("Thermal Config")

        self.enabled = config.Boolean(
            "Enabled",
            description="Whether thermal is enabled for this camera",
            default=False,
        )
        self.channel = config.String(
            "Channel",
            description="RTSP channel name for thermal feed. On Hikvision thermal cameras this is usually /Streaming/Channels/201.",
            default="Streaming/Channels/201",
        )


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
            default=0,
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

class Mode(Enum):
    video = "Video"
    image = "Image"


class ScaleSize(Enum):
    p360 = "360:-1"
    p480 = "480:-1"
    p720 = "720:-1"
    p1080 = "1080:-1"


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
            description="Video format. Images are generally preferred as they will load faster than videos.",
            default=Mode.image,
            choices=Mode,
        )
        self.secs = config.Integer(
            "Duration", description="Duration of snapshot", default=6
        )
        self.fps = config.Integer("FPS", description="FPS of snapshot", default=5)
        self.scale = config.Enum(
            "Scale",
            description="Scale of snapshot",
            default=ScaleSize.p360,
            choices=ScaleSize,
        )

    @property
    def mode_as_filetype(self) -> str:
        match Mode(self.mode.value):
            case Mode.video:
                return "mp4"
            case Mode.image:
                return "jpg"

        raise RuntimeError("unknown camera mode")


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
    hikvision_thermal = "Hikvision (Thermal)"


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
        self.thermal = CameraThermalConfig()

    @property
    def rtsp_uri(self) -> str:
        if self.connection.username.value or self.connection.password.value:
            return f"rtsp://{self.connection.username.value}:{self.connection.password.value}@{self.connection.address.value}:{self.connection.rtsp_port.value}/{self.connection.rtsp_channel.value}"
        return f"rtsp://{self.connection.address.value}:{self.connection.rtsp_port.value}/{self.connection.rtsp_channel.value}"

    @property
    def thermal_rtsp_uri(self):
        if not self.thermal.enabled.value:
            return None

        if self.connection.username.value or self.connection.password.value:
            return f"rtsp://{self.connection.username.value}:{self.connection.password.value}@{self.connection.address.value}:{self.connection.rtsp_port.value}/{self.thermal.channel.value}"
        return f"rtsp://{self.connection.address.value}:{self.connection.rtsp_port.value}/{self.thermal.channel.value}"

    @property
    def human_detect_enabled(self):
        return any(
            ObjectDetectionType(e.value) is ObjectDetectionType.person
            for e in self.object_detection.elements
        )

    @property
    def vehicle_detect_enabled(self):
        return any(
            ObjectDetectionType(e.value) is ObjectDetectionType.vehicle
            for e in self.object_detection.elements
        )


def export():
    CameraConfig().export(
        Path(__file__).parents[2] / "doover_config.json", "doover_camera"
    )
