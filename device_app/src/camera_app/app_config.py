from datetime import datetime
from enum import Enum
from pathlib import Path

from pydoover import config


class CameraThermalConfig(config.Object):
    enabled = config.Boolean(
        "Enabled",
        description="Whether thermal is enabled for this camera",
        default=False,
    )
    channel = config.String(
        "Channel",
        description="RTSP channel name for thermal feed. On Hikvision thermal cameras this is usually /Streaming/Channels/201.",
        default="Streaming/Channels/201",
        advanced=True,
    )


class CameraConnectionConfig(config.Object):
    username = config.String(
        "Camera Username",
        description="Username to login to camera control",
        default=None,
    )
    password = config.String(
        "Camera Password",
        description="Password to login to camera control",
        default=None,
    )
    address = config.String(
        "IP address", description="IP address of camera (e.g. 192.168.50.100)"
    )
    rtsp_port = config.Integer(
        "RTSP Port",
        description="Port of RTSP feed on camera",
        default=554,
        advanced=True,
    )
    rtsp_channel = config.String(
        "RTSP Channel",
        description="RTSP channel name. On Dahua cameras this is usually 'live'.",
        default="live",
        advanced=True,
    )
    control_port = config.Integer(
        "Control Port",
        description="Port of control page on camera",
        default=80,
        advanced=True,
    )


class CameraPowerConfig(config.Object):
    enabled = config.Boolean(
        "Enabled",
        description="Whether power control is enabled for this camera",
        default=False,
    )

    pin = config.Integer(
        "Power Pin",
        description="Digital Output pin that controls power to camera circuit.",
        default=0,
    )
    timeout = config.Integer(
        "Off After",
        description="Number of seconds after which the camera will be powered off",
        default=60 * 15,
        advanced=True,
    )
    wake_delay = config.Integer(
        "Wake Delay",
        description="Seconds for camera to boot before requesting a snapshot.",
        default=60,
        advanced=True,
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
    enabled = config.Boolean("Enabled", default=True)
    period = config.Integer(
        "Period",
        description="Number of seconds between snapshots",
        default=60 * 60 * 4,
    )
    mode = config.Enum(
        "Mode",
        description="Video format. Images are generally preferred as they will load faster than videos.",
        default=Mode.image,
        choices=Mode,
    )
    secs = config.Integer(
        "Duration",
        description="Duration of snapshot",
        default=6,
        advanced=True,
    )
    fps = config.Integer(
        "FPS",
        description="FPS of snapshot",
        default=10,
        advanced=True,
    )
    scale = config.Enum(
        "Scale",
        description="Scale of snapshot",
        default=ScaleSize.p720,
        choices=ScaleSize,
        advanced=True,
    )
    native_h264 = config.Boolean(
        "Native H264",
        description="Camera streams native H264, so video snapshots can be stream-copied instead of re-encoded. Disable if the camera streams a different codec or if scale/fps must be applied. Note: when enabled, the Scale and FPS settings are ignored for video snapshots.",
        default=True,
        advanced=True,
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
    enabled = config.Boolean("Enabled", default=True)
    address = config.String(
        "Address",
        description="Address of RTSP server",
        default="http://localhost:8083",
    )
    username = config.String("Username", default="demo")
    password = config.String("Password", default="demo")


class CameraType(Enum):
    dahua_ptz = "Dahua (PTZ)"
    dahua_fixed = "Dahua (Fixed)"
    dahua_generic = "Dahua (Generic)"
    unifi_generic = "UniFi (Generic)"
    generic_ip = "Generic IP"
    hikvision_thermal = "Hikvision (Thermal)"
    hikvision_anpr = "Hikvision (ANPR)"
    hikvision_acusense = "Hikvision (AcuSense)"
    hikvision_deepinview = "Hikvision (DeepinView)"
    bosch_ptz = "Bosch (PTZ)"


class CameraANPRConfig(config.Object):
    enabled = config.Boolean(
        "Enabled",
        description="Enable on-camera ANPR (license plate + vehicle detection). "
        "Available on Hikvision ANPR '/P' road-traffic models and DeepinView "
        "(iDS-2CD5xxx) deep-learning cameras.",
        default=False,
    )
    min_confidence = config.Integer(
        "Minimum Confidence",
        description="Ignore plate reads below this confidence (0-100).",
        default=0,
        advanced=True,
    )


class CameraPPEConfig(config.Object):
    """On-camera PPE (hard hat) detection, for site-safety monitoring.

    Only the Hikvision DeepinView (iDS-2CD5xxx) deep-learning cameras run this — the
    camera flags a person in the scene who isn't wearing a hard hat, which arrives on
    the alertStream as its own smart event (see the DeepinView engine).
    """

    enabled = config.Boolean(
        "Enabled",
        description="Enable on-camera PPE / hard-hat detection. Flags a person not "
        "wearing a hard hat. Hikvision DeepinView models only.",
        default=False,
    )
    sensitivity = config.Integer(
        "PPE Detection Sensitivity",
        description="Hard-hat detection sensitivity (0-100). Higher catches more "
        "(smaller/further people) but risks more false alarms.",
        default=50,
        advanced=True,
    )
    notify = config.Boolean(
        "Notify on Violation",
        description="Send a notification (and capture a snapshot) when someone is "
        "detected without a hard hat.",
        default=True,
        advanced=True,
    )


class CameraAlarmConfig(config.Object):
    """Night-time intruder alarm driven by basic motion detection (VMD).

    On Hikvision ANPR models there is no on-camera person classification, so the
    night intruder trigger is plain motion. Between night_start_hour and
    night_end_hour the app pulses an alarm-output relay (external siren/strobe)
    and emits a notification on motion; outside that window motion is ignored.
    """

    intruder_alarm_enabled = config.Boolean(
        "Intruder Alarm Enabled",
        description="At night, pulse the alarm output (external siren/strobe) and "
        "send a notification when motion is detected.",
        default=False,
    )
    output_port = config.Integer(
        "Alarm Output Port",
        description="Camera alarm-output relay to pulse for the external siren/strobe.",
        default=1,
        advanced=True,
    )
    pulse_secs = config.Integer(
        "Alarm Pulse Duration",
        description="Seconds to hold the alarm-output relay on per trigger.",
        default=10,
        advanced=True,
    )
    # -- External strobe + horn wired to the Doovit's own digital outputs --
    # Distinct from the camera's alarm relay above (output_port): this hardware hangs
    # off the Doovit's DO pins, so the app drives them directly via platform_iface
    # (see CameraApplication.run_external_alarm), the same way camera power is driven.
    # Each pin doubles as the enable: leave it unset and that output does nothing. Both
    # run for as long as a night intruder keeps being detected — the strobe stays on
    # for the whole event, while the horn sounds in short bursts.
    doovit_strobe_pin = config.Integer(
        "External Strobe Light Output Pin",
        description="Doovit digital-output pin wired to an external strobe light "
        "(e.g. red/blue beacon). Held on for the whole night-intruder event. Leave "
        "unset to disable it.",
        default=None,
    )
    doovit_horn_pin = config.Integer(
        "External Horn Output Pin",
        description="Doovit digital-output pin wired to an external horn. Sounded in "
        "short repeated bursts for the duration of a night-intruder event. Leave unset "
        "to disable it.",
        default=None,
    )
    beep_enabled = config.Boolean(
        "Camera Buzzer",
        description="Also sound the camera's built-in buzzer on night motion "
        "(fires locally even if doover is offline).",
        default=True,
        advanced=True,
    )
    white_light_deterrent = config.Boolean(
        "Flash Light",
        description="Flash the camera's built-in light (ColorVu white light on "
        "AcuSense) on a smart event at night (built-in, fires even if doover is "
        "offline).",
        default=True,
        advanced=True,
    )
    audio_alarm = config.Boolean(
        "Audible Alarm",
        description="On cameras with a built-in speaker (e.g. AcuSense /SRB), sound "
        "the audible siren on a smart event at night. Combined with the flash light "
        "this uses the camera's built-in flash+siren active response.",
        default=True,
        advanced=True,
    )
    night_start_hour = config.Integer(
        "Night Start Hour",
        description="Hour (0-23, camera/site local time) the intruder alarm arms.",
        default=18,
    )
    night_end_hour = config.Integer(
        "Night End Hour",
        description="Hour (0-23, camera/site local time) the intruder alarm disarms.",
        default=6,
    )
    event_clips_enabled = config.Boolean(
        "Event Video",
        description="On an intruder event at night, upload a video of the event to "
        "doover (instead of a single still). Recording runs for as long as the "
        "intruder keeps being detected, and uploads as one video. Uses the camera's "
        "own microSD recording where a card is fitted, otherwise records the stream "
        "with ffmpeg (which needs the 'full' image). AcuSense only.",
        default=False,
        advanced=True,
    )
    event_clip_cooldown = config.Integer(
        "Event Video Cooldown",
        description="Stop recording this many seconds after the last detection. Each "
        "new detection extends the recording.",
        default=15,
        advanced=True,
    )
    event_clip_max_secs = config.Integer(
        "Event Video Max Length",
        description="Hard limit on a single event video, in seconds. Stops one "
        "long-running event from recording (and uploading) forever.",
        default=120,
        advanced=True,
    )


def value_or(element, fallback):
    """Read a config element, tolerating a config written before it existed.

    pydoover raises from ``.value`` when a key is absent rather than falling back to
    the element's declared default, and an install's stored config is only
    re-materialised when the app's schema is republished. Between those two points
    every read of a newly-added key raises — which on the motion path would mean an
    exception per detection — so the default is applied here instead.
    """
    try:
        return element.value
    except ValueError:
        return fallback


class CameraMotionSnapshotConfig(config.Object):
    """Daytime picture-on-motion capture — the counterpart to the night alarm.

    The intruder alarm owns the night window: motion there means sirens, strobes and
    event video. During the day none of that is wanted, but the picture still is —
    it's what the object detection app analyses for PPE compliance and number plates.

    So this captures a plain still on a classified motion event inside its own hour
    window, with no alarm behaviour attached. It's a separate window rather than
    "whatever isn't night" because the two don't have to be complements: a site may
    want pictures only during working hours, leaving a gap where nothing is captured.
    """

    restrict_to_hours = config.Boolean(
        "Only Capture During Hours",
        description="Limit motion snapshots to the hours below. Off (the default) keeps "
        "the existing behaviour of capturing on every classified person/vehicle event, "
        "whatever the time.",
        default=False,
    )
    start_hour = config.Integer(
        "Start Hour",
        description="Hour (0-23, site-local time) motion snapshots start being taken. "
        "Only applies when 'Only Capture During Hours' is on.",
        default=6,
        minimum=0,
        maximum=23,
    )
    end_hour = config.Integer(
        "End Hour",
        description="Hour (0-23, site-local time) motion snapshots stop being taken. "
        "Only applies when 'Only Capture During Hours' is on.",
        default=18,
        minimum=0,
        maximum=23,
    )
    min_interval_secs = config.Integer(
        "Minimum Seconds Between Snapshots",
        description="Shortest gap between two motion snapshots. The camera reports a "
        "target once rather than re-alarming while it stays in the zone, but that depends "
        "on the camera honouring the setting -- this is the backstop that stops one "
        "vehicle sitting in frame becoming a stream of snapshots, uploads and cloud "
        "inference runs. 0 disables it and captures on every event.",
        default=15,
        minimum=0,
        advanced=True,
    )
    object_detection = config.Boolean(
        "Object Detection",
        description="Offer these snapshots to the Object Detection app for hard-hat / "
        "high-vis and number-plate analysis. Requires that app to be installed and "
        "pointed at this camera; turning it on here only marks the snapshots as "
        "wanted, it doesn't run any inference in this app.",
        default=False,
    )


class ObjectDetectionType(Enum):
    person = "Person"
    vehicle = "Vehicle"


class CameraConfig(config.Schema):
    position = config.ApplicationPosition()
    type = config.Enum(
        "Camera Type",
        default=CameraType.dahua_generic,
        choices=CameraType,
    )

    connection = CameraConnectionConfig("Camera Connection Config")
    power = CameraPowerConfig("Camera Power Config")
    snapshot = CameraSnapshotConfig("Camera Snapshot Config")
    rtsp_server = CameraRTSPServerConfig(
        "Camera RTSP Server Config",
        advanced=True,
    )

    object_detection = config.Array(
        "Object Detection",
        description="Objects to detect. Leave blank to disable object detection.",
        element=config.Enum(
            "Object",
            choices=ObjectDetectionType,
            default=ObjectDetectionType.person,
        ),
        unique_items=True,
    )
    sensitivity = config.Integer(
        "Detection Sensitivity",
        description="On-camera intrusion (field) detection sensitivity (0-100). Higher "
        "detects smaller/further/faster targets but risks more false alarms. Hikvision "
        "AcuSense only.",
        default=50,
        advanced=True,
    )
    control_enabled = config.Boolean(
        "Control Enabled",
        description="Allow control (movement) of PTZ cameras.",
        default=True,
    )
    thermal = CameraThermalConfig("Thermal Config")
    anpr = CameraANPRConfig("ANPR Config")
    ppe = CameraPPEConfig("PPE Detection Config")
    alarm = CameraAlarmConfig("Intruder Alarm Config")
    motion_snapshot = CameraMotionSnapshotConfig("Motion Snapshot Config")

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

    @staticmethod
    def _in_hour_window(hour: int, start: int, end: int) -> bool:
        """Whether ``hour`` falls in the [start, end) window, which may wrap midnight.

        Shared by the night alarm window and the motion-snapshot window so the two
        can't drift apart in how they treat a wrapping range or a start == end.
        """
        if start == end:
            return False
        if start < end:
            return start <= hour < end
        # Window wraps midnight (e.g. 18 -> 6).
        return hour >= start or hour < end

    def is_night(self, now: datetime = None) -> bool:
        """True if the current hour is within the intruder-alarm night window."""
        return self._in_hour_window(
            (now or datetime.now()).hour,
            self.alarm.night_start_hour.value,
            self.alarm.night_end_hour.value,
        )

    def motion_snapshot_allowed(self, now: datetime = None) -> bool:
        """Whether a classified-motion event should capture a still right now.

        Unrestricted unless the hour window is switched on, so an existing install
        keeps capturing on every person/vehicle event exactly as it did before this
        setting existed.
        """
        if not value_or(self.motion_snapshot.restrict_to_hours, False):
            return True
        return self._in_hour_window(
            (now or datetime.now()).hour,
            value_or(self.motion_snapshot.start_hour, 6),
            value_or(self.motion_snapshot.end_hour, 18),
        )

    @property
    def motion_snapshot_window(self) -> tuple | None:
        """The hour window the camera must stay armed for motion snapshots.

        ``None`` when nothing extra is needed. Otherwise a ``(start, end)`` pair for
        :meth:`HikvisionClient.set_event_arming_schedule`, which unions it with the
        night window — the schedule gates the *event*, so an hour missing from it is an
        hour the camera classifies nothing and no snapshot can happen.

        ``(0, 24)`` when the window is unrestricted, because "capture on every
        classified event, whatever the time" means the camera has to be awake all day.
        """
        if not value_or(self.motion_snapshot.restrict_to_hours, False):
            return (0, 24)
        start = value_or(self.motion_snapshot.start_hour, 6)
        end = value_or(self.motion_snapshot.end_hour, 18)
        if start == end:
            return None
        return (start, end)

    @property
    def motion_snapshot_min_interval(self) -> int:
        """Shortest gap between motion snapshots, in seconds. 0 means no floor."""
        return max(0, value_or(self.motion_snapshot.min_interval_secs, 15))

    @property
    def motion_snapshot_object_detection(self) -> bool:
        """Whether motion snapshots should be offered to the Object Detection app."""
        return bool(value_or(self.motion_snapshot.object_detection, False))


def export():
    CameraConfig().export(
        Path(__file__).parents[2] / "doover_config.json", "doover_camera"
    )
