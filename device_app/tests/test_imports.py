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


def test_build_capture_names_media_and_thumbnail():
    import asyncio
    import types
    from camera_app.engines.base import CameraBase, Capture
    from pydoover.models import File

    cam = CameraBase.__new__(CameraBase)
    cam.config = types.SimpleNamespace(
        snapshot=types.SimpleNamespace(
            mode_as_filetype="jpg",
        )
    )

    def make(name):
        return File(filename=name, data=b"x", size=1, content_type="image/jpeg")

    async def fake_thumb():
        return make("raw.jpg")

    cam.get_thumbnail = fake_thumb

    # A preset's thumbnail sits beside its media: Preset 1.jpg / Preset 1-thumbnail.jpg
    capture = asyncio.run(cam.build_capture("Preset 1", make("x.jpg")))
    assert capture.media.filename == "Preset_1.jpg"
    assert capture.thumbnail.filename == "Preset_1-thumbnail.jpg"
    assert len(capture.files()) == 2

    # Views that a thumbnail wouldn't represent (e.g. a thermal channel) skip it.
    bare = asyncio.run(cam.build_capture("thermal", make("x.jpg"), with_thumbnail=False))
    assert bare.thumbnail is None
    assert bare.files() == [bare.media]

    # A thumbnail failure must not lose the snapshot.
    async def broken_thumb():
        raise RuntimeError("no ffmpeg")

    cam.get_thumbnail = broken_thumb
    survived = asyncio.run(cam.build_capture("Preset 2", make("x.jpg")))
    assert survived.thumbnail is None and survived.media.filename == "Preset_2.jpg"
    assert isinstance(survived, Capture)


def test_upload_media_sends_every_capture():
    """A PTZ camera returns one capture per preset - all of them must go up.

    Regression: the upload path once took files[0], silently dropping every preset
    after the first (and the thermal view on thermal cameras).
    """
    import asyncio
    import types
    from camera_app.application import CameraApplication
    from camera_app.engines.base import Capture
    from pydoover.models import File

    def make(name):
        return File(filename=name, data=b"x", size=1, content_type="image/jpeg")

    captures = [
        Capture("Preset1", make("Preset1.jpg"), make("Preset1-thumbnail.jpg")),
        Capture("Preset2", make("Preset2.jpg"), make("Preset2-thumbnail.jpg")),
    ]

    sent = {}

    async def fake_create_message(app_key, payload, files):
        sent["payload"], sent["files"] = payload, files

    async def fake_night():
        return True

    app = CameraApplication.__new__(CameraApplication)
    app.app_key = "cam"
    app.device_agent = types.SimpleNamespace(create_message=fake_create_message)
    app.engine = types.SimpleNamespace(detect_night=fake_night)

    asyncio.run(app.upload_media(captures, "schedule"))

    # Every preset, and both its files.
    assert [f.filename for f in sent["files"]] == [
        "Preset1.jpg",
        "Preset1-thumbnail.jpg",
        "Preset2.jpg",
        "Preset2-thumbnail.jpg",
    ]
    assert sent["payload"] == {
        "reason": "schedule",
        "night": True,
        "media": [
            {"name": "Preset1", "file": "Preset1.jpg", "thumbnail": "Preset1-thumbnail.jpg"},
            {"name": "Preset2", "file": "Preset2.jpg", "thumbnail": "Preset2-thumbnail.jpg"},
        ],
    }


def test_detection_zone_payload():
    from camera_app.events import DetectionTarget, DetectionZone, DetectionZonesPayload

    payload = DetectionZonesPayload.from_dict(
        {
            "zones": [
                {
                    "id": 1,
                    "name": "gate",
                    "enabled": True,
                    "points": [[0.1, 0.2], [0.9, 0.2], [0.9, 0.8]],
                    "targets": ["person", "vehicle", "wormhole"],
                    "sensitivity": 60,
                }
            ]
        }
    )
    zone = payload.zones[0]
    assert zone.id == 1 and zone.name == "gate" and zone.sensitivity == 60
    # An unknown target from a newer frontend is dropped, not fatal.
    assert zone.targets == [DetectionTarget.person, DetectionTarget.vehicle]
    assert zone.to_dict()["points"] == [[0.1, 0.2], [0.9, 0.2], [0.9, 0.8]]

    # A UI drag can overshoot the frame; coords are clamped rather than rejected.
    out = DetectionZone.from_dict({"id": 1, "points": [[-0.5, 1.7], [0.5, 0.5], [1, 0]]})
    assert out.points == [(0.0, 1.0), (0.5, 0.5), (1.0, 0.0)]


def test_zone_native_roundtrip():
    from camera_app.engines.hikvision_acusense import HikvisionAcuSenseCamera
    from camera_app.engines.dahua_base import DahuaCameraBase

    hik = HikvisionAcuSenseCamera.__new__(HikvisionAcuSenseCamera)
    dahua = DahuaCameraBase.__new__(DahuaCameraBase)

    for cam in (hik, dahua):
        for point in ((0.0, 0.0), (0.5, 0.5), (1.0, 1.0), (0.25, 0.75)):
            x, y = cam._from_native(*cam._to_native(*point))
            assert abs(x - point[0]) < 0.001, (cam, point)
            assert abs(y - point[1]) < 0.001, (cam, point)

    # Hikvision's y axis is flipped relative to our top-left origin, so the top of
    # the frame must map to the far end of its range - not to 0.
    assert hik._to_native(0.0, 0.0) == (0, 1000)
    assert hik._to_native(1.0, 1.0) == (1000, 0)
    # Dahua shares our top-left origin, so no flip.
    assert dahua._to_native(0.0, 0.0) == (0, 0)
    assert dahua._to_native(1.0, 1.0) == (8191, 8191)


def test_set_zones_blanks_unused_slots():
    """Dropping a zone must delete it, not leave the old polygon behind.

    The camera keeps all four region slots, so a slot left out of the body silently
    keeps whatever it had - zones would be editable but never deletable.
    """
    import asyncio
    import types
    from camera_app.engines.hikvision_acusense import HikvisionAcuSenseCamera
    from camera_app.events import DetectionZone, DetectionTarget

    cam = HikvisionAcuSenseCamera.__new__(HikvisionAcuSenseCamera)
    cam.config = types.SimpleNamespace(sensitivity=types.SimpleNamespace(value=50))

    written = {}

    async def fake_write(regions, channel=1):
        written["regions"] = regions

    cam.client = types.SimpleNamespace(set_field_detection_regions=fake_write)

    zone = DetectionZone(
        id=1,
        points=[(0.0, 0.0), (0.5, 0.0), (0.5, 0.5)],
        targets=[DetectionTarget.person],
        sensitivity=70,
    )
    asyncio.run(cam.set_detection_zones([zone]))

    regions = written["regions"]
    # Every slot is written, not just the one we set.
    assert len(regions) == HikvisionAcuSenseCamera.ZONE_CAPABILITIES["max_zones"]
    assert [r["id"] for r in regions] == [1, 2, 3, 4]
    # The real zone keeps its points; the rest are blanked to clear them.
    assert len(regions[0]["points"]) == 3
    assert regions[0]["sensitivity"] == 70
    assert regions[0]["targets"] == ["human"]
    assert all(r["points"] == [] for r in regions[1:])


def test_zone_capabilities_advertised():
    from camera_app.engines.hikvision_acusense import HikvisionAcuSenseCamera
    from camera_app.engines.generic import GenericRTSPCamera

    caps = HikvisionAcuSenseCamera.ZONE_CAPABILITIES
    assert caps["supported"] and caps["max_zones"] == 4
    assert (caps["min_points"], caps["max_points"]) == (3, 10)
    # The camera ignores a region's enabled flag, so the UI must not offer a toggle.
    assert caps["supports_disable"] is False
    # A camera with no zone support must say so, so the UI hides the editor.
    assert GenericRTSPCamera.ZONE_CAPABILITIES["supported"] is False


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

def test_ppe_event_parsing():
    from camera_app.events import PPEEvent

    # The violation count is pulled from whatever key ends in hardHat...Num, without
    # assuming one exact path (the firmware's element names aren't documented).
    assert PPEEvent.from_alert(
        {"HardHatDetection.targetAttrs.noHardHatNum": "3"}
    ).no_hardhat == 3
    # A missing/garbled count is tolerated - the active event is still the violation.
    assert PPEEvent.from_alert({"eventType": "hardHatDetection"}).no_hardhat is None
    assert PPEEvent.from_alert({"someHelmetCount": "nope"}).no_hardhat is None


def test_deepinview_routes_events():
    """DeepinView routes ANPR->anpr, hard-hat->ppe, and everything else to AcuSense."""
    import asyncio
    import types
    from camera_app.engines.hikvision_deepinview import HikvisionDeepinViewCamera
    from camera_app.events import ANPREvent, PPEEvent

    cam = HikvisionDeepinViewCamera.__new__(HikvisionDeepinViewCamera)
    cam.config = types.SimpleNamespace(
        anpr=types.SimpleNamespace(min_confidence=types.SimpleNamespace(value=0))
    )
    got = {}
    cam.on_anpr_event_callback = lambda e: got.__setitem__("anpr", e)
    cam.on_ppe_event_callback = lambda e: got.__setitem__("ppe", e)

    # ANPR event -> anpr callback with a parsed plate.
    asyncio.run(cam.on_cam_event({"eventType": "ANPR", "ANPR.licensePlate": "ABC123"}))
    assert isinstance(got["anpr"], ANPREvent) and got["anpr"].plate == "ABC123"

    # Hard-hat event -> ppe callback.
    asyncio.run(
        cam.on_cam_event(
            {"eventType": "hardHatDetection", "eventState": "active",
             "HardHatDetection.noHardHatNum": "1"}
        )
    )
    assert isinstance(got["ppe"], PPEEvent) and got["ppe"].no_hardhat == 1


def test_external_alarm_drives_strobe_and_horn():
    """Strobe raised while an intruder is present; both outputs drop when it clears.

    The event is made to have already gone quiet (last detection is well past the
    zero cooldown), so the watcher trips after a single reconcile tick - no real burst
    timing is waited on. The point is the safety-critical behaviour: the strobe is
    driven on, the horn is driven, and neither pin is left high once nobody holds it.
    """
    import asyncio
    import types
    from datetime import datetime, timedelta, timezone
    from camera_app.application import CameraApplication

    calls = []

    class FakeIface:
        async def set_do(self, pin, state):
            calls.append((pin, state))

    def make_app(strobe_pin, horn_pin, shared=None):
        v = lambda x: types.SimpleNamespace(value=x)
        store = shared if shared is not None else {}

        class FakeTags:
            def get_tag(self, name, default=0, app_key=None):
                return store.get(name, default)

            async def set_tag(self, name, value, app_key=None):
                store[name] = value

        app = CameraApplication.__new__(CameraApplication)
        app.platform_iface = FakeIface()
        app.tag_manager = FakeTags()
        app.config = types.SimpleNamespace(
            alarm=types.SimpleNamespace(
                doovit_strobe_pin=v(strobe_pin),
                doovit_horn_pin=v(horn_pin),
                event_clip_cooldown=v(0),
            )
        )
        app._external_alarm_task = None
        # Already quiet: last detection is old, so the watcher ends the event at once.
        app._last_intruder_event_at = datetime.now(tz=timezone.utc) - timedelta(seconds=100)
        return app

    # Both wired, nobody else holding: strobe driven on, horn driven, both end OFF.
    app = make_app(3, 4)
    asyncio.run(app.run_external_alarm())
    assert (3, True) in calls  # strobe raised while active
    assert any(pin == 4 for pin, _ in calls)  # horn driven
    last = {}
    for pin, state in calls:
        last[pin] = state
    assert last == {3: False, 4: False}  # nothing left on

    # Strobe only (horn unset): horn pin 4 is never driven, strobe ends OFF.
    calls.clear()
    app = make_app(3, None)
    asyncio.run(app.run_external_alarm())
    assert (3, True) in calls and calls[-1] == (3, False)
    assert all(pin == 3 for pin, _ in calls)

    # Neither wired: nothing is touched, no None pin ever driven.
    calls.clear()
    app = make_app(None, None)
    asyncio.run(app.run_external_alarm())
    assert calls == []

    # Shared output: another camera app still holds the strobe (future deadline in the
    # shared tag). When our intruder clears we must NOT switch the strobe off - only
    # the horn, which nobody else holds.
    calls.clear()
    from camera_app.application import ALARM_HOLD_TAG_PREFIX

    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    shared = {f"{ALARM_HOLD_TAG_PREFIX}3": now_ms + 60_000}  # peer holds strobe 60s
    app = make_app(3, 4, shared=shared)
    asyncio.run(app.run_external_alarm())
    assert (3, True) in calls  # we drove the strobe on while active
    assert (3, False) not in calls  # ...but never off - the peer still holds it
    assert (4, False) in calls  # the horn, held by nobody, is released


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