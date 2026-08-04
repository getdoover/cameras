from enum import Enum
from typing import Any


CAMERA_CONTROL_CHANNEL = "camera_control"

# Detection zones are written with this command, which goes over `ui_cmds` rather
# than the camera control channel so it runs through the commands system and picks up
# its audit logging. It needs a matching interaction in the UI tree (see
# app_ui.CameraUI) — the UI command manager looks the interaction up by name to build
# the handler's context. There's no matching "get": the command's own value in the
# `ui_cmds` aggregate holds the current zones, as it would for any other interaction.
SET_ZONES_CMD = "set_detection_zones"

# Where a vendor client stashes the bounding boxes it found in an alert, on the same
# flattened event dict it hands the engines. It lives here rather than in a client
# because it's the contract between the two: the client fills it, TargetBox reads it.
TARGET_REGIONS_KEY = "TargetRegions"


class MotionDetectEventType(Enum):
    vehicle = "vehicle"
    person = "person"
    # Unclassified motion (e.g. Hikvision basic VMD, which has no on-camera
    # human/vehicle classification). Used for the night-intruder trigger.
    motion = "motion"
    unknown = "unknown"


class MotionDetectEvent:
    def __init__(self, type_: MotionDetectEventType, data: dict):
        self.type = type_
        self.data = data
        # Where the camera saw the target(s), when it says. Empty on cameras (or
        # firmware) that don't report a rect, so callers must treat it as optional.
        self.boxes = TargetBox.list_from_alert(data)


class ANPREvent:
    """A license-plate / vehicle detection from a Hikvision ANPR camera.

    Built from the flattened ``<EventNotificationAlert>`` dict produced by the
    ISAPI alertStream, where the plate details live under the nested ``<ANPR>``
    block (flattened to dotted keys, e.g. ``ANPR.licensePlate``).
    """

    def __init__(
        self,
        plate: str,
        plate_type: str = None,
        vehicle_type: str = None,
        country: str = None,
        confidence: int = None,
        lane: str = None,
        direction: str = None,
        data: dict = None,
    ):
        self.plate = plate
        self.plate_type = plate_type
        self.vehicle_type = vehicle_type
        self.country = country
        self.confidence = confidence
        self.lane = lane
        self.direction = direction
        self.data = data or {}

    @classmethod
    def from_alert(cls, alert: dict) -> "ANPREvent":
        def g(*keys):
            for k in keys:
                if alert.get(k):
                    return alert[k]
            return None

        confidence = g("ANPR.confidenceLevel", "ANPR.confidence")
        try:
            confidence = int(confidence) if confidence is not None else None
        except (TypeError, ValueError):
            confidence = None

        return cls(
            plate=g("ANPR.licensePlate", "ANPR.plateNumber", "licensePlate"),
            plate_type=g("ANPR.plateType"),
            vehicle_type=g("ANPR.vehicleInfo.vehicleType", "ANPR.vehicleType"),
            country=g("ANPR.country", "ANPR.countryIndex"),
            confidence=confidence,
            lane=g("ANPR.laneNo", "ANPR.line"),
            direction=g("ANPR.direction"),
            data=alert,
        )

class PPEEvent:
    """A PPE (hard-hat) detection from a Hikvision DeepinView camera.

    Built from the flattened ``<EventNotificationAlert>`` dict produced by the ISAPI
    alertStream. The camera's hard-hat rule fires when it sees a person who *isn't*
    wearing a hard hat, so an active event is itself the violation; ``no_hardhat`` is
    the count of offending targets where the camera reports one.

    The exact child element names for this event vary by firmware/model and aren't
    publicly documented for the newer DeepinViewX analytics, so parsing is deliberately
    tolerant — VERIFY against a real alertStream capture when the camera is on the bench
    and tighten the key lookups below if needed.
    """

    def __init__(self, no_hardhat: int = None, data: dict = None):
        self.no_hardhat = no_hardhat
        self.data = data or {}

    @classmethod
    def from_alert(cls, alert: dict) -> "PPEEvent":
        # The camera reports its target count under a key that ends in something like
        # "targetAttrs.noHardHatNum" / "hardHatNum"; scan flattened keys rather than
        # assume one exact path.
        count = None
        for key, value in alert.items():
            k = key.lower()
            if ("hardhat" in k or "helmet" in k) and ("num" in k or "count" in k):
                try:
                    count = int(value)
                except (TypeError, ValueError):
                    continue
                break
        return cls(no_hardhat=count, data=alert)


class DetectionTarget(Enum):
    """What a zone should detect, in app terms.

    Vendors each have their own vocabulary (Hikvision ``human``, Dahua ``Human``);
    engines map to/from these so the frontend only ever sees one set of names.
    """

    person = "person"
    vehicle = "vehicle"
    animal = "animal"
    other = "other"


class TargetBox:
    """Where a camera saw a target in the frame, and what it thought it was.

    ``box`` is ``[x1, y1, x2, y2]`` as **fractions of the frame, origin top-left**. That
    ordering matches how the Object Detection app publishes its own findings, so the two
    can be compared without a translation step; the units differ deliberately (fractions,
    not pixels) so a box stays meaningful whatever resolution the snapshot came back at,
    and it's the same space as :class:`DetectionZone` points.

    The camera's own vocabulary is normalised on the way in — Hikvision says ``human``,
    everything doover-side says ``person``. An unrecognised token is passed through
    rather than dropped: a newer firmware inventing a target we don't know about is
    still worth reporting.
    """

    def __init__(self, box: list, target: str = None):
        self.box = box
        self.target = target

    @staticmethod
    def _app_target(token: str) -> str:
        if not token:
            return None
        token = token.strip().lower()
        if token == "human":
            return DetectionTarget.person.value
        try:
            return DetectionTarget(token).value
        except ValueError:
            return token

    @classmethod
    def list_from_alert(cls, alert: dict, default_target: str = None) -> list:
        """Every box in a camera alert, in the order the camera reported them.

        ``default_target`` labels boxes the alert didn't classify — an ANPR plate rect,
        say, which is a plate by virtue of the event it arrived on rather than because
        the camera said so.
        """
        boxes = []
        for region in (alert or {}).get(TARGET_REGIONS_KEY) or []:
            box = region.get("box")
            if not box:
                continue
            target = cls._app_target(region.get("target")) or default_target
            boxes.append(cls(box, target))
        return boxes

    def to_dict(self) -> dict[str, Any]:
        payload = {"box": self.box}
        if self.target:
            payload["target"] = self.target
        return payload


class DetectionZone:
    """A detection zone in a device-agnostic coordinate space.

    ``points`` are ``(x, y)`` pairs of floats in ``0..1``, with the origin at the
    **top-left** of the frame, x increasing right and y increasing down — the same
    space a frontend overlay on a video element uses. That way the UI sends back
    exactly what the user drew and never has to know about the camera underneath;
    each engine converts to its own native space (Hikvision 0..1000, Dahua 0..8191)
    and flips axes where needed.
    """

    def __init__(
        self,
        id: int,
        points: list,
        enabled: bool = True,
        name: str = None,
        targets: list = None,
        sensitivity: int = None,
    ):
        self.id = id
        self.points = points
        self.enabled = enabled
        self.name = name
        self.targets = targets or []
        self.sensitivity = sensitivity

    @staticmethod
    def _clamp(value: float) -> float:
        # A UI drag can overshoot the frame edge; cameras reject out-of-range
        # coordinates, so pull them back rather than fail the whole write.
        return min(1.0, max(0.0, float(value)))

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DetectionZone":
        points = [
            (cls._clamp(p[0]), cls._clamp(p[1])) for p in payload.get("points", [])
        ]
        targets = []
        for t in payload.get("targets", []):
            try:
                targets.append(DetectionTarget(t))
            except ValueError:
                pass  # unknown target from a newer frontend - ignore, don't fail

        sensitivity = payload.get("sensitivity")
        return cls(
            id=int(payload.get("id", 1)),
            points=points,
            enabled=bool(payload.get("enabled", True)),
            name=payload.get("name"),
            targets=targets,
            sensitivity=int(sensitivity) if sensitivity is not None else None,
        )

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "id": self.id,
            "enabled": self.enabled,
            "points": [[round(x, 4), round(y, 4)] for x, y in self.points],
            "targets": [t.value for t in self.targets],
        }
        if self.name is not None:
            payload["name"] = self.name
        if self.sensitivity is not None:
            payload["sensitivity"] = self.sensitivity
        return payload


class DetectionZonesPayload:
    """Payload for the ``set_detection_zones`` command."""

    def __init__(self, zones: list):
        self.zones = zones

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DetectionZonesPayload":
        return cls([DetectionZone.from_dict(z) for z in payload.get("zones", [])])


class SDPOfferPayload:
    def __init__(self, stream_name: str, value: str):
        self.stream_name = stream_name
        self.value = value

    @classmethod
    def from_dict(cls, payload: dict[str, Any]):
        return cls(payload["stream_name"], payload["value"])


class FixedZoomEvent:
    def __init__(self, value: str, app_key: str):
        self.value = value
        self.app_key = app_key

    @classmethod
    def from_dict(cls, payload: dict[str, Any]):
        return cls(payload["value"], payload["app_key"])

class PTZControlEvent:
    def __init__(self, pan: int, tilt: int):
        self.pan = pan
        self.tilt = tilt

    @classmethod
    def from_dict(cls, payload: dict[str, Any]):
        return cls(payload["pan"], payload["tilt"])
