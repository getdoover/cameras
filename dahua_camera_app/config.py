from pathlib import Path

from pydoover import config


class CameraConfig(config.Schema):
    def __init__(self):
        self.display_name = config.String("Camera Name", description="User friendly name for camera")
        self.name = config.String("cam_name", default=self.display_name.default.lower().replace(" ", "_"))
        # self.uri = config.String("Camera URI", description="Camera URI in form of rtsp://username:password@address:port/channelName")
        self.type = config.Option("Camera Type", default="dahua_ptz", choices=["dahua_ptz", "dahua_fixed", "dahua_generic"])

        self.username = config.String("Camera Username", description="Username to login to camera control")
        self.password = config.String("Camera Password", description="Password to login to camera control")
        self.address = config.String("IP address", description="IP address of camera (e.g. 192.168.50.100)")
        self.rtsp_port = config.Integer("RTSP Port", description="Port of RTSP feed on camera", default=554)
        self.rtsp_channel = config.String("RTSP Channel", description="RTSP channel name. On Dahua cameras this is usually 'live'.", default="live")
        self.control_port = config.Integer("Control Port", description="Port of control page on camera", default=80)

        self.power_pin = config.Integer("Power Pin", description="Digital Output pin that controls power to camera circuit. Defaults to -1 (no power control).", default=-1)
        self.power_timeout = config.Integer("Power Timeout", description="Power Timeout in seconds", default=60 * 15)
        self.wake_delay = config.Integer("Wake Delay", description="Seconds for camera to boot before requesting a snapshot.", default=5)

        self.remote_component_url = config.String("Remote Component URL", description="URL for live view component. Leave blank to disable live view.", default="https://getdoover.github.io/cameras/HLSLiveView.js")
        self.remote_component_name = config.String("Remote Component Name", description="Name of live view component", default="Live View")

        self.object_detection = config.Option("Object Detection", default=[], choices=["Human", "Vehicle"], select_many=True)
        self.control_enabled = config.Boolean("Control Enabled", description="Allow control (movement) of PTZ cameras.", default=True)

        self.snapshot_period = config.Integer("Snapshot Period", description="Snapshot period in seconds", default=60 * 60 * 4)
        self.snapshot_mode = config.Option("Snapshot Mode", description="Video format", default="mp4", choices=["mp4", "jpg"])
        self.snapshot_secs = config.Integer("Snapshot Duration", description="Duration of snapshot", default=6)
        self.snapshot_fps = config.Integer("Snapshot FPS", description="FPS of snapshot", default=5)
        self.snapshot_scale = config.String("Snapshot Scale", description="Scale of snapshot", default="360:-1")

    @property
    def rtsp_uri(self):
        if self.username or self.password:
            return f"rtsp://{self.username}:{self.password}@{self.address}:{self.rtsp_port}/{self.rtsp_channel}"
        return f"rtsp://{self.address}:{self.rtsp_port}/{self.rtsp_channel}"


if __name__ == "__main__":
    config = CameraConfig()
    config.export(Path("config_schema.json"))
