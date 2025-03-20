import re


URI_MATCH = re.compile(r"rtsp://(?P<username>.*):(?P<password>.*)@(?P<address>.*):(?P<port>.*)/(?P<channel>.*)")
DEFAULT_WAKE_DELAY = 5
DEFAULT_POWER_TIMEOUT = 60 * 15  # 15 minutes
DEFAULT_FPS = 5
DEFAULT_SCALE = "360:-1"


class CameraConfig:
    def __init__(self, data):
        self.name = data["NAME"]
        self.display_name = data["DISPLAY_NAME"]
        self.uri = data["URI"]
        self.type = data.get("TYPE", "generic")

        self.rtsp_uri = self.uri

        match = URI_MATCH.match(data["URI"])
        # prefer config, fallback to parsing the URI.
        self.username = self.get_or_fallback_to_match(data, match, "USERNAME", "username")
        self.password = self.get_or_fallback_to_match(data, match, "PASSWORD", "password")
        self.address = self.get_or_fallback_to_match(data, match, "ADDRESS", "address")
        self.rtsp_port = self.get_or_fallback_to_match(data, match, "RTSP_PORT", "port")

        self.control_port = data.get("CONTROL_PORT", 80)
        self.power_timeout = data.get("POWER_TIMEOUT", DEFAULT_POWER_TIMEOUT)
        self.power_pin = data.get("POWER_PIN")
        self.wake_delay = data.get("WAKE_DELAY", DEFAULT_WAKE_DELAY)

        self.remote_component_url = data.get("REMOTE_COMPONENT_URL")
        self.remote_component_name = data.get("REMOTE_COMPONENT_NAME")

        self.object_detection = data.get("OBJECT_DETECTION")
        self.control_enabled = data.get("CONTROL_ENABLED")

        self.snapshot_period = data.get("SNAPSHOT_PERIOD")
        self.snapshot_mode = data.get("SNAPSHOT_MODE") or "mp4"
        self.snapshot_secs = data.get("SNAPSHOT_SECS") or 6
        self.snapshot_fps = data.get("SNAPSHOT_FPS") or DEFAULT_FPS
        self.snapshot_scale = data.get("SNAPSHOT_SCALE") or DEFAULT_SCALE


    @staticmethod
    def get_or_fallback_to_match(config, match, config_key, match_key):
        try:
            return config[config_key]
        except KeyError:
            try:
                return match.group(match_key)
            except AttributeError:
                return None

