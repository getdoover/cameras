"""
Basic tests for an application.

This ensures all modules are importable and that the config is valid.
"""

def test_import_app():
    from camera_app.application import CameraApplication
    assert CameraApplication

def test_config():
    from camera_app.app_config import CameraConfig

    config = CameraConfig()
    assert isinstance(config.to_schema(), dict)

def test_ui():
    from camera_app.app_ui import CameraUI
    assert CameraUI

def test_import_engines():
    from camera_app.engines.hikvision_anpr import HikvisionANPRCamera
    from camera_app.engines.hikvision_acusense import HikvisionAcuSenseCamera
    from camera_app.events import ANPREvent, MotionDetectEventType
    assert HikvisionANPRCamera
    assert HikvisionAcuSenseCamera
    assert ANPREvent
    assert MotionDetectEventType.motion


def test_night_time_segments():
    from camera_app.clients.hikvision import HikvisionClient

    # A window that wraps midnight can't be one segment - it must become two.
    assert HikvisionClient._night_time_segments(18, 6) == [
        ("00:00:00", "06:00:00"),
        ("18:00:00", "24:00:00"),
    ]
    assert HikvisionClient._night_time_segments(9, 17) == [("09:00:00", "17:00:00")]
    # start == end means "never armed", not "always armed".
    assert HikvisionClient._night_time_segments(6, 6) == []
    assert HikvisionClient._night_time_segments(20, 0) == [("20:00:00", "24:00:00")]


def test_arming_schedule_body():
    import xml.etree.ElementTree as ET
    from camera_app.clients.hikvision import HikvisionClient

    client = HikvisionClient.__new__(HikvisionClient)
    body = client._arming_schedule_body(18, 6)
    root = ET.fromstring(body)  # must be well-formed

    # The camera rejects the schedule if it carries a version/xmlns, the way the
    # trigger body does - it must match what the web UI sends.
    assert "xmlns" not in body and "version" not in body
    # A midnight-wrapping window is two blocks a day, every day.
    blocks = [e for e in root.iter() if e.tag == "TimeBlock"]
    assert len(blocks) == 14
    assert sorted({e.text for e in root.iter() if e.tag == "dayOfWeek"}) == [
        str(d) for d in range(1, 8)
    ]
    assert body.count("<beginTime>00:00:00</beginTime>") == 7
    assert body.count("<endTime>24:00:00</endTime>") == 7

    # A non-wrapping window is a single block a day.
    assert client._arming_schedule_body(9, 18).count("<TimeBlock>") == 7


def test_smart_alarm_linkage_ids():
    from camera_app.clients.hikvision import HikvisionClient

    client = HikvisionClient.__new__(HikvisionClient)
    # record is per video input, so it is id'd with the channel; others are not.
    assert client._linkage_id("record", 1) == "record-1"
    assert client._linkage_id("whiteLight", 1) == "whiteLight"
    # beep must be stripped with audio or the buzzer survives a disarm.
    assert "beep" in client._managed_linkage_ids(1)
    assert {"whiteLight", "audio", "record-1"} <= client._managed_linkage_ids(1)
    # `center` is what puts events on the alertStream, so we own it too - it must be
    # stripped from the preserved set, then re-added on every write (see below).
    assert "center" in client._managed_linkage_ids(1)

    # whiteLight/record are rejected without their required child element.
    assert "<WhiteLightAction>" in client._linkage_notification("whiteLight")
    assert "<videoInputID>1</videoInputID>" in client._linkage_notification("record", 1)


def test_default_field_detection_body():
    import xml.etree.ElementTree as ET
    from camera_app.clients.hikvision import HikvisionClient

    client = HikvisionClient.__new__(HikvisionClient)
    body = client._default_field_detection_body(True, ["human", "vehicle"], 75, 1)

    root = ET.fromstring(body)  # must be well-formed
    text = "".join(root.itertext())
    assert "human,vehicle" in body
    assert "<sensitivityLevel>75</sensitivityLevel>" in body
    # A region is what the camera needs to actually run the rule.
    assert "FieldDetectionRegion" in body
    assert text  # sanity


def test_has_recording_storage():
    import asyncio
    from camera_app.clients.hikvision import HikvisionClient

    def probe(volumes):
        client = HikvisionClient.__new__(HikvisionClient)
        client.get_storage = lambda: _async(volumes)
        return asyncio.run(client.has_recording_storage())

    async def _async(value):
        return value

    # A formatted, mounted card is usable.
    assert probe([{"hddType": "SD", "status": "ok", "capacity": "60000"}]) is True
    # An unformatted card would silently record nothing - treat as unusable.
    assert probe([{"hddType": "SD", "status": "unformatted", "capacity": "60000"}]) is False
    # No card at all.
    assert probe([]) is False
    assert probe([{"hddType": "SD", "status": "notexist", "capacity": "0"}]) is False
    # Status ok but zero capacity is not something we can record to.
    assert probe([{"hddType": "SD", "status": "ok", "capacity": "0"}]) is False
    # Junk capacity must not raise.
    assert probe([{"hddType": "SD", "status": "ok", "capacity": ""}]) is False


def test_acusense_target_extraction():
    from camera_app.engines.hikvision_acusense import HikvisionAcuSenseCamera

    # Mirrors the flattened alertStream event dict for a fielddetection alarm.
    event = {
        "eventType": "fielddetection",
        "eventState": "active",
        "DetectionRegionList.DetectionRegionEntry.detectionTarget": "human",
    }
    assert HikvisionAcuSenseCamera._extract_target(event) == "human"

def test_is_night():
    from camera_app.app_config import CameraConfig
    from datetime import datetime

    config = CameraConfig()
    config.alarm.night_start_hour.value = 18
    config.alarm.night_end_hour.value = 6
    assert config.is_night(datetime(2026, 1, 1, 22, 0)) is True  # inside wrap window
    assert config.is_night(datetime(2026, 1, 1, 3, 0)) is True   # after midnight
    assert config.is_night(datetime(2026, 1, 1, 12, 0)) is False  # midday

# def test_state():
#     from app_template.app_state import SampleState
#     assert SampleState