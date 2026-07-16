from enum import Enum
from typing import Any


CAMERA_CONTROL_CHANNEL = "camera_control"


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
