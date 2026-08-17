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

# Where a vendor client stashes the JPEG the camera attached to an alert — its own frame of
# the event, taken at the moment it classified the target. Raw bytes, absent when the camera
# didn't send one.
EVENT_IMAGE_KEY = "EventImage"

# Where a vendor client stashes the ids of the rule regions an alert fired for — the link
# between "something happened" and *which zone the user drew*, without which every
# detection has to be treated identically.
#
# A list because one alert can name several: two people entering two zones at the same
# moment is one alert with two `<DetectionRegionEntry>` blocks. Absent on cameras and
# rules that don't report regions at all.
TRIGGERED_REGIONS_KEY = "TriggeredRegions"


def event_image(alert: dict) -> bytes | None:
    """The camera's own frame of an event, if it sent one with the alert."""
    return (alert or {}).get(EVENT_IMAGE_KEY)


def region_ids_from_alert(alert: dict) -> list:
    """Which rule regions an alert fired for, as ints, in the order reported."""
    return list((alert or {}).get(TRIGGERED_REGIONS_KEY) or [])


class ZoneKind(Enum):
    """How a zone triggers — which camera rule owns it.

    Only two, because that is the only axis the *camera* has: presence in the region, or
    crossing into it. What a zone looks *for* is a separate question, answered by
    ``targets`` (things the camera classifies) and :class:`ZoneDetector` (things the object
    detection app finds in the resulting snapshot).

    Keeping those apart is what lets one zone want a person, their hard hat and any plate
    in the same polygon. An earlier design made PPE and ANPR kinds of their own, which
    forced a separate zone per question and made "person but not PPE" unexpressible.
    """

    # Presence in the region. The default, shown to users as "Regular".
    intrusion = "intrusion"
    # Somewhere nobody should be, so crossing *into* it is the event.
    excluded_area = "excluded_area"


class ZoneDetector(Enum):
    """Extra things to look for in a zone that the camera itself cannot detect.

    An AcuSense can classify a person or a vehicle, but it can't tell whether the person
    is wearing a hard hat or read the vehicle's plate. Those come from the object
    detection app running its models over the snapshot the zone produced, so a detector
    is carried to that app in the snapshot payload and used as a spatial filter.

    Each one depends on the camera classifying something first, because that is what
    causes a snapshot to exist at all — see :data:`DETECTOR_REQUIRES_TARGET`.
    """

    ppe = "ppe"
    anpr = "anpr"


# The camera target each detector needs the zone to be watching for.
#
# Not a style rule — a hard dependency. The object detection app only ever sees frames the
# camera published, and the camera only publishes one when it classified something. A zone
# asking for PPE while ignoring people produces no snapshots, so the PPE model never runs
# and the zone silently does nothing. The UI selects the target automatically; the app
# warns if something else writes a zone without it.
DETECTOR_REQUIRES_TARGET = {
    ZoneDetector.ppe: "person",
    ZoneDetector.anpr: "vehicle",
}


# Which camera rule each on-camera kind is written to, and reported by. These are ISAPI
# names, so the casing is the firmware's, not ours — `regionEntrance` is camelCase where
# `FieldDetection` is PascalCase and the camera is strict about it (see
# HikvisionClient.disable_smart_rule). The event's `eventType` uses lowercase
# `fielddetection`, which is why matching goes through ZONE_RULE_EVENT_TYPES rather than
# comparing to these directly.
RULE_FIELD_DETECTION = "FieldDetection"
RULE_REGION_ENTRANCE = "regionEntrance"

KIND_TO_RULE = {
    ZoneKind.intrusion: RULE_FIELD_DETECTION,
    ZoneKind.excluded_area: RULE_REGION_ENTRANCE,
}

# `eventType` as it appears on the alertStream -> the rule that produced it. The camera
# lowercases `fielddetection` in events while the ISAPI path is `FieldDetection`, so this
# mapping exists to stop that inconsistency leaking into every comparison.
ZONE_RULE_EVENT_TYPES = {
    "fielddetection": RULE_FIELD_DETECTION,
    "regionentrance": RULE_REGION_ENTRANCE,
}


def rule_for_event_type(event_type: str) -> str | None:
    """Which rule an alert's ``eventType`` belongs to, or None if it isn't a zone rule."""
    return ZONE_RULE_EVENT_TYPES.get((event_type or "").strip().lower())


class MotionDetectEventType(Enum):
    vehicle = "vehicle"
    person = "person"
    # Unclassified motion (e.g. Hikvision basic VMD, which has no on-camera
    # human/vehicle classification). Used for the night-intruder trigger.
    motion = "motion"
    unknown = "unknown"


class MotionDetectEvent:
    """A detection from a camera, or a report that one is still going on.

    ``continuation`` marks the latter: the target hasn't left yet. It is **not** a new
    detection and must not be treated as one — no notification, no snapshot, no
    ``camera_event`` — because a camera sends one every few seconds for as long as
    somebody stands there. What it is for is telling the alarm the intruder is still
    present, which is otherwise unknowable (there's nothing to poll).
    """

    def __init__(
        self,
        type_: MotionDetectEventType,
        data: dict,
        continuation: bool = False,
        rule: str = None,
        region_ids: list = None,
    ):
        self.type = type_
        self.data = data
        self.continuation = continuation
        # Which camera rule reported this, and which of its regions the target was in —
        # together these identify the zone the user drew, so the app can honour that
        # zone's own settings instead of treating every detection alike.
        #
        # Both are optional and often absent: only the Hikvision perimeter rules report a
        # region, and a `duration` continuation carries none at all. A consumer that
        # can't identify a zone must fall back to the camera-wide behaviour rather than
        # drop the event — see CameraApplication.zones_for_event.
        self.rule = rule
        self.region_ids = region_ids or region_ids_from_alert(data)
        # Where the camera saw the target(s), when it says. Empty on cameras (or
        # firmware) that don't report a rect, so callers must treat it as optional.
        self.boxes = TargetBox.list_from_alert(data)
        # The camera's own JPEG of this event, when it attached one — the frame at the
        # moment of detection, rather than one fetched a moment later.
        self.image = event_image(data)


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

    ``threshold_secs`` is how long a target must stay inside the zone before it counts —
    the other half of "will this catch someone walking past", alongside ``sensitivity``.
    ``0`` means report it as soon as the camera classifies it, which is what you want on a
    road; a second or two suppresses things that flicker in and out at the boundary. Check
    ``capabilities.supports_threshold`` and its min/max before offering the control.
    Region entrance has no dwell time at all, so it is ignored for ``excluded_area``.

    ``kind`` says what the zone is for (see :class:`ZoneKind`) and so which rule — or
    which *app* — acts on it. ``notify`` is per-zone on purpose: the point of drawing a
    zone is usually to be told about that one thing, and to stop being told about
    everything else.
    """

    def __init__(
        self,
        id: int,
        points: list,
        enabled: bool = True,
        name: str = None,
        targets: list = None,
        sensitivity: int = None,
        threshold_secs: int = None,
        kind: "ZoneKind" = ZoneKind.intrusion,
        notify: bool = None,
        detectors: list = None,
    ):
        self.id = id
        self.points = points
        self.enabled = enabled
        self.name = name
        self.targets = targets or []
        self.sensitivity = sensitivity
        self.threshold_secs = threshold_secs
        self.kind = kind
        # Extra things to look for here that the camera can't (see ZoneDetector). Empty on
        # most zones; a zone can carry both.
        self.detectors = detectors or []
        # An excluded area is by definition somewhere nobody should be, so it defaults
        # loud. The others default quiet: a camera that notified on every person it
        # classified is the noise this feature exists to remove, and someone who draws a
        # zone to get pictures shouldn't be signed up to alerts as a side effect.
        self.notify = self.default_notify(kind) if notify is None else bool(notify)

    @staticmethod
    def default_notify(kind: "ZoneKind") -> bool:
        return kind is ZoneKind.excluded_area

    @property
    def rule(self) -> str | None:
        """The camera rule this zone is written to, or None if the camera never sees it."""
        return KIND_TO_RULE.get(self.kind)

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
        threshold = payload.get("threshold_secs")

        detectors = []
        for d in payload.get("detectors", []):
            try:
                detectors.append(ZoneDetector(d))
            except ValueError:
                pass  # unknown detector from a newer frontend - ignore, don't fail

        raw_kind = payload.get("kind") or ZoneKind.intrusion.value
        try:
            kind = ZoneKind(raw_kind)
        except ValueError:
            # PPE and plates used to be kinds of their own before they became detectors.
            # A stored record or an older frontend can still say so, and it means "an
            # ordinary zone that also looks for this" — so migrate rather than discard,
            # or upgrading would turn somebody's PPE zone into a plain one and silently
            # stop the model running over it.
            try:
                migrated = ZoneDetector(raw_kind)
            except ValueError:
                # Genuinely unknown, e.g. a newer frontend. Fall back rather than reject
                # the write: `intrusion` keeps the zone detecting, where dropping it would
                # silently lose a region the user drew.
                kind = ZoneKind.intrusion
            else:
                kind = ZoneKind.intrusion
                if migrated not in detectors:
                    detectors.append(migrated)

        return cls(
            id=int(payload.get("id", 1)),
            points=points,
            enabled=bool(payload.get("enabled", True)),
            name=payload.get("name"),
            targets=targets,
            sensitivity=int(sensitivity) if sensitivity is not None else None,
            # 0 is a real value here (report immediately), so this can't collapse a
            # supplied 0 into "unset" the way a truthiness check would.
            threshold_secs=int(threshold) if threshold is not None else None,
            kind=kind,
            detectors=detectors,
            # Absent means "use the default for this kind", not False — an older
            # frontend that doesn't send the field must not silence every excluded area.
            notify=payload.get("notify"),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "id": self.id,
            "enabled": self.enabled,
            "points": [[round(x, 4), round(y, 4)] for x, y in self.points],
            "targets": [t.value for t in self.targets],
            # Always emitted, unlike the optional fields below: the frontend needs them to
            # render the zone at all, and `notify` has no "unset" state once a zone exists.
            "kind": self.kind.value,
            "notify": self.notify,
            # Always emitted, even when empty: the object detection app reads this to
            # decide whether the zone concerns it, and an absent key would be
            # indistinguishable from an older app that never sent one.
            "detectors": [d.value for d in self.detectors],
        }
        if self.name is not None:
            payload["name"] = self.name
        if self.sensitivity is not None:
            payload["sensitivity"] = self.sensitivity
        if self.threshold_secs is not None:
            payload["threshold_secs"] = self.threshold_secs
        return payload

    def contains(self, x: float, y: float) -> bool:
        """Whether a normalised point falls inside this zone's polygon.

        Ray casting, so it handles the concave polygons the editor allows. Used to decide
        whether a detection the *app* made (a hard hat, a plate) happened inside a zone —
        the camera-backed kinds never need this, since the camera reports the region.

        Points exactly on an edge are not guaranteed either way, which is inherent to the
        method and fine here: a target box centre landing precisely on a boundary is not a
        distinction worth defining, and both answers are defensible.
        """
        points = self.points
        if len(points) < 3:
            return False

        inside = False
        j = len(points) - 1
        for i, (xi, yi) in enumerate(points):
            xj, yj = points[j]
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                inside = not inside
            j = i
        return inside


class DetectionZonesPayload:
    """Payload for the ``set_detection_zones`` command."""

    def __init__(self, zones: list):
        self.zones = zones

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DetectionZonesPayload":
        return cls([DetectionZone.from_dict(z) for z in payload.get("zones", [])])


class SDPOfferPayload:
    def __init__(self, stream_name: str, value: str, request_id: str | None = None):
        self.stream_name = stream_name
        self.value = value
        # Names the caller so its answer can be addressed to it alone — see
        # CameraApplication.accept_sdp. Optional: older clients don't send one
        # and get only the shared `sdp` slot, as they always did.
        self.request_id = request_id

    @classmethod
    def from_dict(cls, payload: dict[str, Any]):
        return cls(
            payload["stream_name"], payload["value"], payload.get("request_id")
        )


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
