from enum import Enum
from typing import Any


CAMERA_CONTROL_CHANNEL = "camera_control"


class MotionDetectEventType(Enum):
    vehicle = "vehicle"
    person = "person"
    unknown = "unknown"


class MotionDetectEvent:
    def __init__(self, type_: MotionDetectEventType, data: dict):
        self.type = type_
        self.data = data

class GenericCameraControlEvent:
    def __init__(self, app_key: str):
        self.app_key = app_key

    @classmethod
    def from_dict(cls, data: dict[str, Any]):
        return cls(data["app_key"])

class SDPOfferPayload:
    def __init__(self, stream_name: str, value: str, app_key: str):
        self.stream_name = stream_name
        self.value = value
        self.app_key = app_key

    @classmethod
    def from_dict(cls, payload: dict[str, Any]):
        return cls(payload["stream_name"], payload["value"], payload["app_key"])


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
