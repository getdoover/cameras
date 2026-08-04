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

    # Dwell time is the "regardless of how fast they run in" knob. The stock rule shipped
    # 5s, which silently misses anyone crossing the region quicker than that.
    assert INTRUSION_DWELL_SECS == 1
    client = HikvisionClient.__new__(HikvisionClient)
    body = client._default_field_detection_body(True, ["human"], 50, 1)
    assert "<timeThreshold>1</timeThreshold>" in body
    # ...and the same value on the path that writes user-drawn zones, so editing a zone
    # can't quietly change how fast a target has to move to be missed.
    region = {"id": 1, "points": [(10, 10)], "targets": ["human"], "sensitivity": 50}
    assert "<timeThreshold>1</timeThreshold>" in client._field_detection_region(region)


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
