from enum import Enum


class MotionDetectEventType(Enum):
    vehicle = "vehicle"
    person = "person"
    unknown = "unknown"


class MotionDetectEvent:
    def __init__(self, type_: MotionDetectEventType, data: dict):
        self.type = type_
        self.data = data
