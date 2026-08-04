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

    async def fake_static_alarm(enabled, channel=1):
        written["static_alarm"] = enabled
        return True

    cam.client = types.SimpleNamespace(
        set_field_detection_regions=fake_write,
        set_static_target_alarm=fake_static_alarm,
    )

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
    # Writing regions rebuilds the rule body and drops rule-level fields with it, so the
    # static-target re-alarm switch has to be re-asserted -- or editing a zone quietly costs
    # the night alarm the only signal it has that an intruder is still present.
    assert written["static_alarm"] is True


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
    body = client._arming_schedule_body([(18, 6)])
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
    assert client._arming_schedule_body([(9, 18)]).count("<TimeBlock>") == 7


def test_merged_hour_segments():
    """The union is what stops a night deterrent blinding daytime detection.

    The arming schedule gates the *event* on this firmware, not just its linkages, so
    every window the camera must classify in has to end up in one merged schedule.
    """
    from camera_app.clients.hikvision import HikvisionClient as C

    # Night alone still wraps midnight into two segments.
    assert C._merged_hour_segments([(18, 6)]) == [
        ("00:00:00", "06:00:00"),
        ("18:00:00", "24:00:00"),
    ]

    # Night + working hours, abutting exactly: must collapse to all day, not three
    # blocks the camera might not join up.
    assert C._merged_hour_segments([(18, 6), (6, 18)]) == [("00:00:00", "24:00:00")]

    # An unrestricted daytime window swallows everything.
    assert C._merged_hour_segments([(18, 6), (0, 24)]) == [("00:00:00", "24:00:00")]

    # A gap is preserved — 05:00-08:00 is genuinely unarmed here.
    assert C._merged_hour_segments([(20, 5), (8, 17)]) == [
        ("00:00:00", "05:00:00"),
        ("08:00:00", "17:00:00"),
        ("20:00:00", "24:00:00"),
    ]

    # Overlapping windows merge rather than duplicate.
    assert C._merged_hour_segments([(9, 15), (12, 18)]) == [("09:00:00", "18:00:00")]

    # Empty / degenerate windows contribute nothing.
    assert C._merged_hour_segments([(6, 6)]) == []
    assert C._merged_hour_segments([]) == []


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

def test_target_regions_from_alert():
    """Bounding boxes: every target in an alert, in the app's own coordinate space."""
    import xml.etree.ElementTree as ET

    from camera_app.clients.hikvision import HikvisionClient, _xml_to_dict

    def alert(entries):
        return ET.fromstring(
            '<EventNotificationAlert xmlns="http://www.hikvision.com/ver20/XMLSchema">'
            "<eventType>regionEntrance</eventType><DetectionRegionList>"
            f"{entries}</DetectionRegionList></EventNotificationAlert>"
        )

    def entry(target, x, y, w, h, tag="TargetRect"):
        return (
            f"<DetectionRegionEntry><detectionTarget>{target}</detectionTarget>"
            f"<{tag}><X>{x}</X><Y>{y}</Y><width>{w}</width><height>{h}</height></{tag}>"
            f"</DetectionRegionEntry>"
        )

    # Fractional units (0..1) pass through; x/y/w/h becomes x1/y1/x2/y2.
    root = alert(entry("human", 0.1, 0.2, 0.3, 0.4))
    assert HikvisionClient._target_regions(root) == [
        {"box": [0.1, 0.2, 0.4, 0.6], "target": "human"}
    ]

    # Normalized 0..1000 units (the space the zone endpoints use) are scaled down, so
    # both firmware conventions land in the same 0..1 space.
    root = alert(entry("vehicle", 500, 250, 100, 200))
    assert HikvisionClient._target_regions(root) == [
        {"box": [0.5, 0.25, 0.6, 0.45], "target": "vehicle"}
    ]

    # TWO targets in one alert must both survive. The flattened event dict cannot do
    # this - repeated siblings collapse onto one key - which is why boxes are read off
    # the tree instead.
    root = alert(entry("human", 0.1, 0.1, 0.1, 0.1) + entry("human", 0.5, 0.5, 0.2, 0.2))
    assert len(HikvisionClient._target_regions(root)) == 2
    flat = _xml_to_dict(root)
    assert flat["DetectionRegionList.DetectionRegionEntry.TargetRect.X"] == "0.5"

    # A rect the camera didn't classify (e.g. an ANPR plate) still yields a box.
    root = ET.fromstring(
        "<EventNotificationAlert><ANPR><boundingBox><X>0.2</X><Y>0.2</Y>"
        "<width>0.1</width><height>0.05</height></boundingBox></ANPR>"
        "</EventNotificationAlert>"
    )
    assert HikvisionClient._target_regions(root) == [{"box": [0.2, 0.2, 0.3, 0.25]}]

    # An alert with no rect at all - most of them - carries no boxes, and doesn't fail.
    assert HikvisionClient._target_regions(ET.fromstring("<EventNotificationAlert/>")) == []
    # Nor does a malformed one (empty or non-numeric coordinates).
    root = alert(entry("human", "", 0.2, 0.3, 0.4))
    assert HikvisionClient._target_regions(root) == []
    # ...or one missing a dimension entirely.
    root = alert("<DetectionRegionEntry><TargetRect><X>0.1</X></TargetRect></DetectionRegionEntry>")
    assert HikvisionClient._target_regions(root) == []

    # Out-of-frame coordinates are clamped rather than published as-is.
    root = alert(entry("human", 0.9, 0.9, 0.5, 0.5))
    assert HikvisionClient._target_regions(root)[0]["box"] == [0.9, 0.9, 1.0, 1.0]


def test_target_box_payload():
    from camera_app.events import (
        TARGET_REGIONS_KEY,
        MotionDetectEvent,
        MotionDetectEventType,
        TargetBox,
    )

    alert = {
        TARGET_REGIONS_KEY: [
            {"box": [0.1, 0.2, 0.4, 0.6], "target": "human"},
            {"box": [0.5, 0.5, 0.6, 0.6]},
        ]
    }

    # The camera's vocabulary is normalised on the way in: it says human, we say person.
    boxes = MotionDetectEvent(MotionDetectEventType.person, alert).boxes
    assert [b.to_dict() for b in boxes] == [
        {"box": [0.1, 0.2, 0.4, 0.6], "target": "person"},
        {"box": [0.5, 0.5, 0.6, 0.6]},  # unclassified: no target key at all
    ]

    # An unclassified rect can be labelled by the event it arrived on (ANPR -> plate),
    # and a classified one is never overridden by that default.
    labelled = TargetBox.list_from_alert(alert, default_target="plate")
    assert [b.target for b in labelled] == ["person", "plate"]

    # An event with no boxes yields none, and doesn't blow up on a missing/None alert.
    assert TargetBox.list_from_alert({}) == []
    assert TargetBox.list_from_alert(None) == []


def test_upload_media_publishes_detections():
    import asyncio
    import types

    from camera_app.application import CameraApplication
    from camera_app.engines.base import Capture
    from camera_app.events import TargetBox

    published = []

    def make_app():
        app = CameraApplication.__new__(CameraApplication)
        app.app_key = "doover_camera_1"
        app.engine = types.SimpleNamespace(detect_night=_none)
        app.config = types.SimpleNamespace(motion_snapshot_object_detection=True)
        app.device_agent = types.SimpleNamespace(create_message=_capture)
        return app

    async def _none():
        return None

    async def _capture(app_key, payload, files):
        published.append(payload)

    media = types.SimpleNamespace(filename="snapshot.jpg")
    capture = Capture("snapshot", media)

    boxes = [TargetBox([0.1, 0.2, 0.4, 0.6], "person")]
    asyncio.run(make_app().upload_media([capture], "person", boxes))
    assert published[-1]["detections"] == [
        {"box": [0.1, 0.2, 0.4, 0.6], "target": "person"}
    ]

    # Absent, not empty, when the camera reported nothing - so a consumer can tell
    # "no boxes this time" from "this camera doesn't do boxes".
    asyncio.run(make_app().upload_media([capture], "person", []))
    assert "detections" not in published[-1]
    asyncio.run(make_app().upload_media([capture], "schedule"))
    assert "detections" not in published[-1]


def test_intrusion_is_the_only_rule():
    """One rule, day and night: entrance/exit/line are off, and their events ignored."""
    from camera_app.clients.hikvision import INTRUSION_DWELL_SECS, HikvisionClient
    from camera_app.engines.hikvision_acusense import (
        SMART_EVENT_TYPES,
        UNUSED_SMART_RULES,
    )

    assert SMART_EVENT_TYPES == {"fielddetection"}
    # Accepting an event type we never enable is a latent duplicate per target.
    assert "regionEntrance" not in SMART_EVENT_TYPES
    assert "regionEntrance" in UNUSED_SMART_RULES

    # Dwell time is the "regardless of how fast they run in" knob, and the default is the
    # camera's own floor: report it as soon as it's classified. The stock rule shipped 5s,
    # enough to miss a vehicle crossing the zone entirely.
    assert INTRUSION_DWELL_SECS == 0
    client = HikvisionClient.__new__(HikvisionClient)
    body = client._default_field_detection_body(True, ["human"], 50, 1)
    assert "<timeThreshold>0</timeThreshold>" in body
    # A zone that doesn't specify one gets the same default...
    region = {"id": 1, "points": [(10, 10)], "targets": ["human"], "sensitivity": 50}
    assert "<timeThreshold>0</timeThreshold>" in client._field_detection_region(region)
    # ...and one that does gets what it asked for, since it's per-zone now.
    region["time_threshold"] = 4
    assert "<timeThreshold>4</timeThreshold>" in client._field_detection_region(region)


def test_rewrite_element_leaves_absent_tags_alone():
    from camera_app.clients.hikvision import HikvisionClient

    rewrite = HikvisionClient._rewrite_element
    body = "<FieldDetection><contAlarmForStaticTargetEnabled>true</contAlarmForStaticTargetEnabled></FieldDetection>"
    assert "<contAlarmForStaticTargetEnabled>false</" in rewrite(
        body, "contAlarmForStaticTargetEnabled", "false"
    )
    # A tag the firmware doesn't have is NOT invented - these cameras 400 on unexpected
    # content, which would lose the whole write.
    assert rewrite("<FieldDetection/>", "contAlarmForStaticTargetEnabled", "false") == (
        "<FieldDetection/>"
    )
    # Every occurrence is rewritten: one <timeThreshold> per region slot, and they must
    # agree or detection differs by corner of the frame for no visible reason.
    four = "".join("<timeThreshold>5</timeThreshold>" for _ in range(4))
    assert rewrite(four, "timeThreshold", "1").count("<timeThreshold>1<") == 4


def test_arming_schedule_assert_and_ownership():
    """The schedule gates whether the camera detects at all, so it's kept written."""
    import asyncio
    import types
    from datetime import timedelta

    from camera_app.engines.hikvision_acusense import REASSERT_SECS, HikvisionAcuSenseCamera

    def make_cam(day_window, accepted=True):
        written = {"schedules": [], "linkage": []}
        v = lambda x: types.SimpleNamespace(value=x)

        async def set_schedule(windows, event="fielddetection", index=1, channel=1):
            written["schedules"].append(windows)
            return accepted

        async def set_linkage(armed, methods=None, **kwargs):
            written["linkage"].append(armed)

        async def set_light(mode, channel=1):
            written["light"] = mode

        cam = HikvisionAcuSenseCamera.__new__(HikvisionAcuSenseCamera)
        cam.client = types.SimpleNamespace(
            set_event_arming_schedule=set_schedule,
            set_smart_alarm_linkage=set_linkage,
            set_supplement_light_mode=set_light,
        )
        cam.config = types.SimpleNamespace(
            alarm=types.SimpleNamespace(
                white_light_deterrent=v(True),
                audio_alarm=v(True),
                night_start_hour=v(18),
                night_end_hour=v(6),
            ),
            motion_snapshot_window=day_window,
            is_night=lambda: False,
        )
        cam.event_clip_mode = None
        cam._deterrent_armed = None
        cam._deterrent_asserted_at = None
        cam.native_schedule_active = False
        cam._schedule_windows = None
        cam._schedule_asserted_at = None
        return cam, written

    # Intrusion covers the day too, so the schedule must include the day window - outside
    # it this firmware emits no event at all and the app hears nothing.
    cam, written = make_cam((6, 18))
    asyncio.run(cam.assert_arming_schedule())
    assert written["schedules"] == [[(18, 6), (6, 18)]]
    # ...and because the schedule no longer means "night", the camera must NOT be trusted
    # to gate the deterrent: a permanently-armed linkage would siren at a delivery driver
    # at midday. The app arms it instead - disarmed here, since it's daytime.
    assert cam.native_schedule_active is False
    asyncio.run(cam.setup_night_deterrent())
    assert written["linkage"] == [False]

    # Re-asserting inside the window is a no-op: nothing changed, nothing stale.
    asyncio.run(cam.assert_arming_schedule())
    assert len(written["schedules"]) == 1

    # A config edit (night hours changed) is picked up on the next loop - no restart, and
    # no waiting out the re-assert interval.
    cam.config.alarm.night_start_hour.value = 20
    asyncio.run(cam.assert_arming_schedule())
    assert written["schedules"][-1] == [(20, 6), (6, 18)]

    # Otherwise it's rewritten every REASSERT_SECS, because the schedule lives on the
    # camera and a web-UI visit or factory reset can change it silently.
    cam._schedule_asserted_at -= timedelta(seconds=REASSERT_SECS)
    asyncio.run(cam.assert_arming_schedule())
    assert len(written["schedules"]) == 3

    # An unrestricted window is still a day window: (0, 24) must not read as "no window".
    cam, written = make_cam((0, 24))
    asyncio.run(cam.assert_arming_schedule())
    assert cam.native_schedule_active is False

    # No day window at all (motion snapshots off): the schedule means night, so the camera
    # gates it and the deterrent survives doover being offline across dusk.
    cam, written = make_cam(None)
    asyncio.run(cam.assert_arming_schedule())
    assert written["schedules"] == [[(18, 6)]]
    assert cam.native_schedule_active is True
    asyncio.run(cam.setup_night_deterrent())
    assert written["linkage"] == [True]  # permanently armed; the schedule gates it

    # Firmware that rejects the schedule always falls back to app-driven arming, and is
    # retried next loop rather than remembered as written.
    cam, written = make_cam(None, accepted=False)
    asyncio.run(cam.assert_arming_schedule())
    assert cam.native_schedule_active is False
    assert cam._schedule_windows is None
    asyncio.run(cam.assert_arming_schedule())
    assert len(written["schedules"]) == 2


def test_arming_schedule_reasserted_without_an_alarm():
    """The schedule decides when the camera detects, so it isn't an alarm setting."""
    import asyncio
    import types

    from camera_app.application import CameraApplication
    from camera_app.engines.hikvision_acusense import HikvisionAcuSenseCamera

    calls = []

    engine = HikvisionAcuSenseCamera.__new__(HikvisionAcuSenseCamera)

    async def assert_schedule(force=False):
        calls.append("schedule")

    async def arm(armed):
        calls.append(("arm", armed))

    engine.assert_arming_schedule = assert_schedule
    engine.arm_night_deterrent = arm

    app = CameraApplication.__new__(CameraApplication)
    app.engine = engine
    app.config = types.SimpleNamespace(
        alarm=types.SimpleNamespace(
            intruder_alarm_enabled=types.SimpleNamespace(value=False)
        ),
        is_night=lambda: True,
    )

    asyncio.run(app.update_alarm_schedule())
    # No alarm to arm, but the schedule still has to be kept written - otherwise a
    # daytime-snapshots-only site can silently stop detecting for part of the day.
    assert calls == ["schedule"]

    calls.clear()
    app.config.alarm.intruder_alarm_enabled.value = True
    asyncio.run(app.update_alarm_schedule())
    assert calls == ["schedule", ("arm", True)]


def test_claim_motion_snapshot_cooldown():
    """The picture is throttled; the alarm never is."""
    import types
    from datetime import timedelta

    from camera_app.application import CameraApplication

    def make_app(interval):
        app = CameraApplication.__new__(CameraApplication)
        app.config = types.SimpleNamespace(motion_snapshot_min_interval=interval)
        app._last_motion_snapshot_at = None
        return app

    app = make_app(15)
    assert app.claim_motion_snapshot() is True
    # The camera re-reporting the same parked car must not cost another snapshot,
    # upload and cloud inference run.
    assert app.claim_motion_snapshot() is False
    app._last_motion_snapshot_at -= timedelta(seconds=15)
    assert app.claim_motion_snapshot() is True

    # 0 means "capture on every event" - no floor, and no state kept.
    app = make_app(0)
    assert all(app.claim_motion_snapshot() for _ in range(5))
    assert app._last_motion_snapshot_at is None


def test_alarm_pulse_does_not_block_the_event_stream():
    """The relay pulse must not be awaited inline: it's how we go deaf mid-intrusion."""
    import asyncio
    import types

    from camera_app.application import CameraApplication

    async def scenario():
        pulses = []

        async def fire_alarm():
            pulses.append("start")
            await asyncio.sleep(0.05)  # stands in for pulse_secs (10s in the field)
            pulses.append("end")

        app = CameraApplication.__new__(CameraApplication)
        app.engine = types.SimpleNamespace(fire_alarm=fire_alarm)
        app._alarm_pulse_task = None

        app.start_alarm_pulse()
        # Returns immediately - the alertStream reader awaiting this callback has to stay
        # free to hear the re-alarms proving the intruder is still there.
        assert pulses == ["start"] or pulses == []

        # A re-alarm while the relay is still held does NOT start a second pulse: the
        # second one's release would cut the first short.
        app.start_alarm_pulse()
        app.start_alarm_pulse()
        await app._alarm_pulse_task
        assert pulses == ["start", "end"]

        # Once it's finished, the next event pulses again.
        app.start_alarm_pulse()
        await app._alarm_pulse_task
        assert pulses.count("start") == 2

        # An engine with no relay (generic cameras) is a no-op, not an AttributeError.
        app.engine = types.SimpleNamespace()
        app._alarm_pulse_task = None
        app.start_alarm_pulse()
        assert app._alarm_pulse_task is None

    asyncio.run(scenario())


def test_duration_alert_keeps_the_alarm_alive():
    """A target that stays put is reported as a `duration` event, not a repeated one.

    Captured from an iDS-2CD5T87G2/V-XHSY (V5.9.20) with somebody standing in the zone:
    `fielddetection` fired `active` once at 14:06:48 and never again, and from then on the
    camera sent these every 5s. Dropping them ended every night alarm one cooldown after
    the first detection, however long the intruder stayed.
    """
    import asyncio
    import xml.etree.ElementTree as ET

    from camera_app.clients.hikvision import _xml_to_dict
    from camera_app.engines.hikvision_acusense import HikvisionAcuSenseCamera
    from camera_app.events import MotionDetectEventType

    # Verbatim from the capture, minus the boilerplate address fields.
    duration_alert = _xml_to_dict(ET.fromstring(
        "<EventNotificationAlert><channelID>1</channelID>"
        "<dateTime>2026-08-04T14:06:51+10:00</dateTime>"
        "<eventType>duration</eventType><eventState>active</eventState>"
        "<eventDescription>duration alarm</eventDescription>"
        "<DurationList><Duration><relationEvent>fielddetection</relationEvent></Duration>"
        "</DurationList></EventNotificationAlert>"
    ))

    def route(alert, last_target=MotionDetectEventType.person):
        seen = []
        cam = HikvisionAcuSenseCamera.__new__(HikvisionAcuSenseCamera)
        cam._last_target = last_target
        cam.on_motion_event_callback = seen.append
        asyncio.run(cam.on_cam_event(alert))
        return seen

    (event,) = route(duration_alert)
    assert event.continuation is True
    # It carries no target of its own, so it inherits the last real classification -
    # otherwise a "person" event would degrade to "motion" halfway through.
    assert event.type is MotionDetectEventType.person

    # A duration alert for a rule we don't act on is not our event to extend.
    other = dict(duration_alert)
    other["DurationList.Duration.relationEvent"] = "linedetection"
    assert route(other) == []


def test_continuation_extends_only_a_live_event():
    """A continuation extends an intruder event; it never starts or revives one."""
    import asyncio
    import types
    from datetime import datetime, timedelta, timezone

    from camera_app.application import CameraApplication

    def make_app(last_event_at):
        app = CameraApplication.__new__(CameraApplication)
        app.config = types.SimpleNamespace(
            alarm=types.SimpleNamespace(
                event_clip_cooldown=types.SimpleNamespace(value=15)
            )
        )
        app.engine = types.SimpleNamespace(event_clip_mode=None, fire_alarm=None)
        app._last_intruder_event_at = last_event_at
        app._external_alarm_task = types.SimpleNamespace(done=lambda: False)
        # An in-flight pulse, so the guard in start_alarm_pulse short-circuits rather than
        # trying to schedule one on a stub.
        app._alarm_pulse_task = types.SimpleNamespace(done=lambda: False)
        app.pulses = []
        return app

    now = datetime.now(tz=timezone.utc)

    # Live event: the timestamp the strobe/horn/recording all watch moves forward, so they
    # keep running for as long as the intruder is there.
    app = make_app(now - timedelta(seconds=10))
    asyncio.run(app.on_detection_continues())
    assert (app._last_intruder_event_at - now).total_seconds() > -1

    # ...and the camera's relay is re-driven. It is pulsed, not held, so on an install with
    # no Doovit strobe/horn pins wired it is the ONLY alarm output and stops after one
    # pulse unless each continuation chains another.
    fired = []
    app = make_app(now - timedelta(seconds=10))
    app._alarm_pulse_task = None  # nothing in flight
    app.start_alarm_pulse = lambda: fired.append(True)
    asyncio.run(app.on_detection_continues())
    assert fired == [True]

    # Already over (cooldown lapsed): the clip has been fetched and uploaded by now, so
    # reviving it would produce a second clip of an intruder we've already reported.
    stale = now - timedelta(seconds=60)
    app = make_app(stale)
    asyncio.run(app.on_detection_continues())
    assert app._last_intruder_event_at == stale

    # Never had one: a continuation on its own must not start an alarm.
    app = make_app(None)
    asyncio.run(app.on_detection_continues())
    assert app._last_intruder_event_at is None


def test_event_clip_search_uses_the_cameras_clock():
    """The card is searched in the camera's time base, not ours.

    Measured on a DS-2CD2387G3 sitting 44s behind the doovit: every event-clip search
    returned "no recording" while the footage sat on the card the whole time, 44 seconds
    from where the app looked. The search window is ~25s wide, so sub-minute drift is
    enough to miss every single clip.
    """
    import asyncio
    import types
    from datetime import datetime, timedelta, timezone

    from camera_app.engines.hikvision_acusense import (
        EVENT_CLIP_SEARCH_MARGIN,
        MAX_CLOCK_DRIFT_SECS,
        HikvisionAcuSenseCamera,
    )

    # Hour-level tolerance is no good for a second-level search.
    assert MAX_CLOCK_DRIFT_SECS <= 5

    asked = {}
    event_start = datetime(2026, 8, 4, 4, 35, 40, tzinfo=timezone.utc)
    event_end = datetime(2026, 8, 4, 4, 36, 6, tzinfo=timezone.utc)

    # What the camera really had on the card for that event, on its own clock: 44s earlier.
    segment = {
        "timeSpan.startTime": "2026-08-04T04:35:00Z",
        "timeSpan.endTime": "2026-08-04T04:35:16Z",
        "mediaSegmentDescriptor.playbackURI": "rtsp://cam/the-event",
    }
    # A neighbouring event's segment, which the widened window also pulls in.
    other = {
        "timeSpan.startTime": "2026-08-04T04:33:00Z",
        "timeSpan.endTime": "2026-08-04T04:33:20Z",
        "mediaSegmentDescriptor.playbackURI": "rtsp://cam/the-wrong-one",
    }

    cam = HikvisionAcuSenseCamera.__new__(HikvisionAcuSenseCamera)
    cam.clock_offset = timedelta(seconds=-44)

    async def search_recordings(start, end, **kwargs):
        asked["start"], asked["end"] = start, end
        return [other, segment]

    async def download_recording(uri):
        asked["uri"] = uri
        return b"IMKH-bytes"

    async def remux(data, name):
        return f"{name}.mp4"

    cam.client = types.SimpleNamespace(
        search_recordings=search_recordings, download_recording=download_recording
    )
    cam.remux_to_mp4 = remux

    assert asyncio.run(cam._fetch_sd_video(event_start, event_end)) == "event.mp4"
    # The window is shifted onto the camera's clock and widened by the margin.
    assert asked["start"] == event_start - timedelta(seconds=44) - EVENT_CLIP_SEARCH_MARGIN
    assert asked["end"] == event_end - timedelta(seconds=44) + EVENT_CLIP_SEARCH_MARGIN
    # Widening can pull in a neighbour, so the one overlapping the event most is taken -
    # not whichever the camera happened to list first.
    assert asked["uri"] == "rtsp://cam/the-event"

    # A match with no parsable span scores zero rather than blowing up the sort.
    assert cam._overlap_secs({}, event_start, event_end) == 0.0


def _power_mgr(always_on=True, threshold=3, cycle_secs=15):
    """A CameraPowerManagement wired to fakes, with no background tasks started."""
    import types

    from camera_app.power_management import CameraPowerManagement

    v = lambda x: types.SimpleNamespace(value=x)
    do_calls = []

    async def set_do(pin, state):
        do_calls.append((pin, state))

    async def publish(*args, **kwargs):
        pass

    mgr = CameraPowerManagement.__new__(CameraPowerManagement)
    mgr.config = types.SimpleNamespace(
        enabled=v(True),
        always_on=v(always_on),
        pin=v(4),
        timeout=v(900),
        wake_delay=v(0),
        watchdog_failures=v(threshold),
        power_cycle_secs=v(cycle_secs),
    )
    mgr.app = types.SimpleNamespace(
        platform_iface=types.SimpleNamespace(set_do=set_do),
        engine=types.SimpleNamespace(),
        app_key="doover_camera_1",
        device_agent=types.SimpleNamespace(update_channel_aggregate=publish),
        tag_manager=types.SimpleNamespace(
            get_tag=lambda *a, **k: 0, set_tag=publish
        ),
    )
    mgr._powered_on_at = None
    mgr._is_pingable = False
    mgr._last_ping_at = None
    mgr._ping_task = None
    mgr._watchdog_task = None
    mgr._cycling = False
    mgr._suspended = False
    mgr.tasks = []
    return mgr, do_calls


def test_always_on_needs_power_management_enabled():
    mgr, _ = _power_mgr(always_on=True)
    assert mgr.always_on is True
    # Always On is meaningless without a power pin to hold.
    mgr.config.enabled.value = False
    assert mgr.always_on is False
    mgr.config.enabled.value = True
    mgr.config.always_on.value = False
    assert mgr.always_on is False


def test_power_cycle_sequence():
    """A cycle drops the pin, waits, restores it - and can't be interrupted mid-way."""
    import asyncio
    from datetime import datetime, timedelta

    mgr, do_calls = _power_mgr(cycle_secs=0)
    mgr._powered_on_at = datetime.now()

    async def scenario():
        # A snapshot arriving mid-cycle must NOT raise the pin: that would abort the reboot
        # we're doing precisely because the camera stopped answering.
        async def acquire_during_cycle():
            await asyncio.sleep(0)
            await mgr.acquire_for(timedelta(seconds=60))

        await asyncio.gather(mgr._power_cycle(), acquire_during_cycle())

    asyncio.run(scenario())

    assert do_calls == [(4, False), (4, True)]
    assert mgr._cycling is False  # released even though a caller raced it
    assert mgr.power_is_on is True


def test_watchdog_only_cycles_on_consecutive_failures():
    import asyncio

    from camera_app import power_management as pm

    mgr, _ = _power_mgr(threshold=3)
    cycles = []

    async def fake_cycle():
        cycles.append(len(cycles) + 1)

    mgr._power_cycle = fake_cycle

    # A single dropped ping, then a success, then three in a row. Only the run of three
    # should reboot the camera - cutting power on one blip is worse than no watchdog.
    seq = [False, True, False, False, False]
    state = {"i": 0}

    async def ping(timeout):
        i = state["i"]
        state["i"] += 1
        if i >= len(seq):
            raise asyncio.CancelledError
        return seq[i]

    mgr.app.engine.ping = ping

    real_sleep = pm.asyncio.sleep

    async def no_sleep(_secs):
        return None

    pm.asyncio.sleep = no_sleep
    try:
        asyncio.run(mgr._watchdog())
    finally:
        pm.asyncio.sleep = real_sleep

    assert cycles == [1]
    assert state["i"] == len(seq) + 1  # ran to the end of the script


def test_always_on_survives_expiry_but_honours_a_deliberate_release():
    """The expiry check must not drop power, but a device shutdown must."""
    import asyncio
    from datetime import timedelta

    mgr, do_calls = _power_mgr()

    # A deliberate release (on_shutdown_at) latches, so the always-on loop can't
    # immediately power the camera back up behind the shutdown's back.
    asyncio.run(mgr.release())
    assert mgr._suspended is True
    assert (4, False) in do_calls

    # ...and anything that actually wants the camera clears the latch again.
    asyncio.run(mgr.acquire_for(timedelta(seconds=60)))
    assert mgr._suspended is False
    assert do_calls[-1] == (4, True)


def _alert_xml(event_type="fielddetection", state="active", pictures=1):
    """An alert shaped like the real ones, with or without an advertised picture."""
    pic = (
        "<detectionPictureTransType>binary</detectionPictureTransType>"
        f"<detectionPicturesNumber>{pictures}</detectionPicturesNumber>"
        f"<picturesNumber>{pictures}</picturesNumber>"
        if pictures
        else ""
    )
    return (
        "<EventNotificationAlert><channelID>1</channelID>"
        f"<eventType>{event_type}</eventType><eventState>{state}</eventState>"
        "<DetectionRegionList><DetectionRegionEntry>"
        "<detectionTarget>human</detectionTarget>"
        "<TargetRect><X>0.7766</X><Y>0.0764</Y><width>0.0344</width>"
        "<height>0.2722</height></TargetRect>"
        "</DetectionRegionEntry></DetectionRegionList>"
        f"{pic}</EventNotificationAlert>"
    ).encode()


def _image_part(jpeg: bytes) -> bytes:
    """The multipart wrapper the camera really uses for the event frame."""
    return (
        b"--boundary\r\n"
        b'Content-Disposition: form-data; name="fielddetection"; filename="fielddetection"\r\n'
        b"Content-Type: image/jpeg\r\n"
        b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg
    )


def test_event_frame_is_paired_with_its_alert():
    """The camera attaches its own frame of the event; it used to be discarded."""
    import asyncio

    from camera_app.clients.hikvision import HikvisionClient
    from camera_app.events import EVENT_IMAGE_KEY

    jpeg = b"\xff\xd8\xff\xe0" + b"x" * 500 + b"\xff\xd9"
    client = HikvisionClient.__new__(HikvisionClient)

    def drain(chunks, pending=None):
        got = []
        buffer = b""
        for chunk in chunks:
            buffer += chunk
            buffer, pending = asyncio.run(
                client._drain(got.append, buffer, pending)
            )
        return got, buffer, pending

    # The frame follows the XML, and arrives with it as one event.
    got, _, pending = drain([b"--boundary\r\n" + _alert_xml() + _image_part(jpeg)])
    assert len(got) == 1 and got[0][EVENT_IMAGE_KEY] == jpeg
    assert pending is None
    # The box survives alongside it.
    assert got[0]["TargetRegions"][0]["target"] == "human"

    # Split across TCP chunks part-way through the JPEG: the alert waits for the rest
    # rather than going out frameless.
    head = b"--boundary\r\n" + _alert_xml()
    whole = head + _image_part(jpeg)
    cut = len(head) + 200  # inside the image body
    got, buffer, pending = drain([whole[:cut]])
    assert got == [] and pending is not None
    got2, _, pending = drain([buffer + whole[cut:]], pending)
    assert len(got2) == 1 and got2[0][EVENT_IMAGE_KEY] == jpeg
    assert pending is None

    # An alert that advertises no picture is dispatched immediately - the alarm path can't
    # wait on a frame that was never coming.
    got, _, pending = drain([b"--boundary\r\n" + _alert_xml(pictures=0)])
    assert len(got) == 1 and EVENT_IMAGE_KEY not in got[0]
    assert pending is None

    # If the next alert beats the picture, the picture isn't coming: release the first
    # rather than staple a later event's frame onto it.
    got, _, pending = drain(
        [b"--boundary\r\n" + _alert_xml() + b"--boundary\r\n" + _alert_xml(pictures=0)]
    )
    assert len(got) == 2
    assert EVENT_IMAGE_KEY not in got[0] and EVENT_IMAGE_KEY not in got[1]


def test_alert_has_picture_gate():
    from camera_app.clients.hikvision import HikvisionClient

    has = HikvisionClient._alert_has_picture
    # Verified on both firmware lines for a classified smart event.
    assert has(
        {
            "detectionPictureTransType": "binary",
            "detectionPicturesNumber": "1",
            "picturesNumber": "1",
        }
    )
    # Heartbeat and videoloss alerts carry no picture, and must not hold up dispatch.
    assert not has({"eventType": "duration", "eventState": "active"})
    assert not has({"detectionPictureTransType": "binary", "picturesNumber": "0"})
    assert not has({"detectionPictureTransType": "url", "picturesNumber": "1"})
    assert not has({})


def test_event_frame_uploads_alongside_the_snapshot():
    import asyncio
    import types

    from camera_app.application import EVENT_FRAME_NAME, CameraApplication
    from camera_app.engines.base import Capture

    published = []

    async def _none():
        return None

    async def _capture(app_key, payload, files):
        published.append((payload, files))

    def make_app():
        app = CameraApplication.__new__(CameraApplication)
        app.app_key = "doover_camera_1"
        app.engine = types.SimpleNamespace(detect_night=_none)
        app.config = types.SimpleNamespace(motion_snapshot_object_detection=True)
        app.device_agent = types.SimpleNamespace(create_message=_capture)
        return app

    jpeg = b"\xff\xd8\xff\xe0eventframe\xff\xd9"
    frame = CameraApplication.build_event_frame(jpeg)
    assert frame.media.filename == f"{EVENT_FRAME_NAME}.jpg"
    assert frame.media.data == jpeg
    assert frame.thumbnail is None

    snapshot = Capture("snapshot", types.SimpleNamespace(filename="snapshot.jpg"))
    asyncio.run(make_app().upload_media([snapshot, frame], "vehicle"))
    payload, _ = published[-1]
    # Both views are described, so a gallery (and the cloud) can tell them apart.
    assert [m["name"] for m in payload["media"]] == ["snapshot", EVENT_FRAME_NAME]
    assert payload["media"][1]["file"] == f"{EVENT_FRAME_NAME}.jpg"
    assert "thumbnail" not in payload["media"][1]


def test_classified_detections_always_notify():
    """No UI switch gates a notification, and none is read on the motion path.

    The switches this replaces were created without values, so reading one raised
    `KeyError: alert_me_on_human_motion` out of the middle of the motion callback and took
    the rest of the handler - camera_event, snapshot, alarm - down with it.
    """
    import asyncio
    import types

    from camera_app.application import CameraApplication
    from camera_app.engines.hikvision_acusense import HikvisionAcuSenseCamera
    from camera_app.events import MotionDetectEvent, MotionDetectEventType

    notifications, events = [], []

    def make_app():
        app = CameraApplication.__new__(CameraApplication)
        app.app_display_name = "Camera 1"
        app.engine = HikvisionAcuSenseCamera.__new__(HikvisionAcuSenseCamera)
        app.config = types.SimpleNamespace(
            alarm=types.SimpleNamespace(
                intruder_alarm_enabled=types.SimpleNamespace(value=False)
            ),
            is_night=lambda: False,
            motion_snapshot_allowed=lambda: False,  # no picture, keeps the test to notifies
        )

        async def notify(msg, severity=None, topic=None):
            notifications.append((topic, msg))

        async def publish(kind, **extra):
            events.append(kind)

        app.send_notification = notify
        app.publish_camera_event = publish
        # Deliberately absent: any attempt to read a UI value must fail the test rather
        # than being silently tolerated.
        app.ui_manager = None
        return app

    for kind in (MotionDetectEventType.person, MotionDetectEventType.vehicle):
        asyncio.run(make_app().on_motion_event_callback(MotionDetectEvent(kind, {})))

    assert [t for t, _ in notifications] == [
        "motion_event_person",
        "motion_event_vehicle",
    ]
    assert events == ["person", "vehicle"]


def test_third_tab_is_empty_and_still_present():
    """The alert switches are gone; the tab keeps its place so the layout doesn't shift."""
    import inspect

    from camera_app import app_ui

    source = inspect.getsource(app_ui)
    # No switch is declared anywhere, including in the commented-out future-UI sketch.
    # (The names still appear in a comment explaining why they went, which is the point.)
    assert 'name="alert_me_on' not in source
    assert "ui.Switch" not in source
    # The container is still built and still handed to the TabContainer.
    assert 'name="detection"' in source
    assert "children=[self.history, *live_views, container]" in source


def test_zone_threshold_seconds_round_trip():
    """Per-zone dwell time travels with the zone, like sensitivity."""
    import asyncio
    import types

    from camera_app.clients.hikvision import INTRUSION_DWELL_SECS
    from camera_app.engines.hikvision_acusense import HikvisionAcuSenseCamera
    from camera_app.events import DetectionTarget, DetectionZone

    caps = HikvisionAcuSenseCamera.ZONE_CAPABILITIES
    # The frontend can't guess 0..60 the way it can assume 0..100 for sensitivity.
    assert caps["supports_threshold"] is True
    assert (caps["threshold_min"], caps["threshold_max"]) == (0, 60)

    # --- write ---
    written = {}

    async def fake_write(regions, channel=1):
        written["regions"] = regions

    async def fake_static(enabled, channel=1):
        return 5

    cam = HikvisionAcuSenseCamera.__new__(HikvisionAcuSenseCamera)
    cam.config = types.SimpleNamespace(sensitivity=types.SimpleNamespace(value=50))
    cam.client = types.SimpleNamespace(
        set_field_detection_regions=fake_write, set_static_target_alarm=fake_static
    )

    points = [(0.2, 0.2), (0.6, 0.2), (0.6, 0.6)]
    zones = [
        DetectionZone(id=1, points=points, targets=[DetectionTarget.person], threshold_secs=4),
        DetectionZone(id=2, points=points, targets=[DetectionTarget.vehicle]),  # unset
        DetectionZone(id=3, points=points, threshold_secs=900),  # beyond the camera's max
    ]
    asyncio.run(cam.set_detection_zones(zones))
    regions = written["regions"]
    assert regions[0]["time_threshold"] == 4
    # Unset means the default, not "leave the slot alone" - the body is rebuilt regardless.
    assert regions[1]["time_threshold"] == INTRUSION_DWELL_SECS
    # Clamped rather than rejected, like out-of-frame coordinates.
    assert regions[2]["time_threshold"] == 60
    # Blanked slots get the default too, so a deleted zone doesn't leave a stale dwell.
    assert regions[3]["time_threshold"] == INTRUSION_DWELL_SECS

    # --- read back ---
    async def fake_read(channel=1):
        return [
            {
                "id": 1,
                "enabled": True,
                "points": [(200, 200), (600, 200), (600, 600)],
                "targets": ["human"],
                "sensitivity": 70,
                "time_threshold": 4,
            }
        ]

    cam.client = types.SimpleNamespace(get_field_detection_regions=fake_read)
    (zone,) = asyncio.run(cam.get_detection_zones())
    assert zone.threshold_secs == 4
    assert zone.to_dict()["threshold_secs"] == 4

    # A zero must survive the round trip rather than being dropped as falsy - it's the
    # default and it means something ("report it immediately").
    assert DetectionZone.from_dict({"id": 1, "threshold_secs": 0}).threshold_secs == 0
    assert DetectionZone(id=1, points=[], threshold_secs=0).to_dict()["threshold_secs"] == 0
    # Absent stays absent, so a camera that can't do it doesn't advertise a bogus value.
    assert "threshold_secs" not in DetectionZone(id=1, points=[]).to_dict()
