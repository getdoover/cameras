"""Hikvision DeepinView (DeepinViewX) engine.

Targets the Hikvision iDS-2CD5xxx DeepinView deep-learning cameras (e.g.
iDS-2CD5T87G2/V-X 8MP DeepinViewX bullet). These carry the full on-camera analytics
engine and, unlike the fixed-function AcuSense and ANPR "/P" models, run *several*
algorithms at once: perimeter protection (human/vehicle), ANPR, and PPE / hard-hat
detection.

Rather than reimplement all of that, this builds on :class:`HikvisionAcuSenseCamera`:
the perimeter-protection intrusion rule, detection-zone editing, ColorVu night
deterrent, on-card event clips, snapshots, thumbnails, day/night and clock-sync all
behave identically and are inherited unchanged. This engine adds the two extra
DeepinView algorithms on top:

  * ``eventType == ANPR``            -> plate / vehicle read  -> ``anpr_callback``
  * ``eventType == hardHatDetection`` -> PPE violation        -> ``ppe_callback``

Day and night are triggered by **different detectors**, which is where this engine
diverges from the AcuSense base:

  * **Night** is the on-camera perimeter analytics (``fielddetection``), classified
    human / vehicle. Only a real target should set off a siren, and the camera's own
    arming schedule keeps the deterrent working while doover is offline.
  * **Day** is plain built-in motion detection (``VMD``) — *not* the smart rules. Every
    bit of motion takes a frame, which goes to the cloud marked for object detection and
    is classified there for high-vis / PPE and plates. The camera's classifier is the
    thing being deliberately bypassed: it decides "person" or "nothing" on a 2MP frame
    with no notion of what we're looking for, so anything it rejects is a frame we never
    get to run a real model over. Capturing broadly and deduplicating in the cloud
    trades bandwidth (and some uninteresting frames) for not missing detections.

So VMD and the perimeter rules never both drive capture: the engine drops VMD at night
and drops perimeter events during the day. That holds regardless of whether the camera
accepted its arming schedules, which is what stops a target from being captured twice.

Because DeepinView subclasses AcuSense, the ``isinstance(engine, HikvisionAcuSense
Camera)`` checks in application.py (clock sync, native arming schedule, intruder
handling) automatically apply to this engine too.
"""

import logging

from ..events import ANPREvent, MotionDetectEvent, MotionDetectEventType, PPEEvent
from .hikvision_acusense import SMART_EVENT_TYPES, HikvisionAcuSenseCamera


log = logging.getLogger(__name__)

# alertStream eventType strings that carry a PPE / hard-hat result. The firmware's
# exact casing for this newer algorithm isn't in the public ISAPI spec, so match a
# few plausible spellings — VERIFY against a real capture on the bench and trim this.
HARD_HAT_EVENT_TYPES = {
    "hardHatDetection",
    "hardhatdetection",
    "HardHatDetection",
    "helmetDetection",
}


class HikvisionDeepinViewCamera(HikvisionAcuSenseCamera):
    # Daytime capture is driven by VMD (see the module docstring), which the app has to
    # know about — unclassified motion means nothing to it otherwise.
    daytime_motion_capture = True

    def __init__(
        self,
        config,
        motion_detect_callback,
        anpr_callback,
        ppe_callback,
        sync_presets_func,
        clear_active_preset_func,
    ):
        super().__init__(
            config,
            motion_detect_callback,
            sync_presets_func,
            clear_active_preset_func,
        )
        self.on_anpr_event_callback = anpr_callback
        self.on_ppe_event_callback = ppe_callback

    async def setup(self):
        # The base sets up the client + session, configures perimeter protection and
        # the night deterrent, and starts the alertStream reader.
        ok = await super().setup()
        if not ok:
            return False

        if self.config.anpr.enabled.value:
            log.info("Enabling on-camera ANPR (vehicle + license plate).")
            try:
                await self.client.enable_vehicle_detection(True)
            except Exception as e:
                log.warning(f"Failed to enable ANPR: {e}", exc_info=e)

        if self.config.ppe.enabled.value:
            sensitivity = self.config.ppe.sensitivity.value
            log.info(f"Enabling on-camera PPE / hard-hat detection (sensitivity={sensitivity}).")
            try:
                await self.client.set_hard_hat_detection(True, sensitivity)
            except Exception as e:
                log.warning(f"Failed to enable PPE detection: {e}", exc_info=e)

        await self.setup_daytime_motion()

        return True

    async def setup_daytime_motion(self):
        """Turn on basic motion detection, which drives daytime capture.

        Left enabled around the clock and gated in :meth:`on_cam_event` instead of by an
        arming schedule. VMD's schedule lives somewhere other than the smart events'
        (:attr:`HikvisionClient._SCHEDULE_COLLECTIONS` has no entry for it) and a wrong
        guess there reads as a malformed body rather than a bad path, so the day/night
        split is enforced where it can be relied on. The cost is the camera evaluating
        motion at night for events we throw away, which costs us nothing.

        The camera's own sensitivity and region are left alone — this is the one detector
        an operator is likely to have already tuned in the web UI, and "capture more" is
        the point, so nothing here should narrow it.
        """
        try:
            await self.client.set_motion_detection(enabled=True)
        except Exception as e:
            log.warning(
                f"Failed to enable basic motion detection: {e} — daytime capture will "
                f"not happen on this camera.",
                exc_info=e,
            )
            return

        try:
            await self.client.ensure_motion_stream_linkage()
        except Exception as e:
            log.info(f"Couldn't confirm motion events are sent to doover: {e}")

        log.info("Basic motion detection (VMD) enabled; it drives daytime capture.")

    async def setup_region_entrance(self, sensitivity: int):
        """Disable region entrance — VMD owns the day on this engine, not the analytics.

        The AcuSense base arms this rule for the daytime snapshot window. Here it would
        be a second trigger for the same person walking through, so it's switched off
        (its polygons survive, see ``disable_smart_rule``). Its events are dropped in
        :meth:`on_cam_event` regardless, in case the camera has it on for other reasons.
        """
        if not await self.client.disable_smart_rule("regionEntrance"):
            log.info(
                "Couldn't confirm region entrance is disabled; its events are ignored "
                "on this engine anyway."
            )

    async def on_cam_event(self, event: dict):
        event_type = event.get("eventType", "")

        # ANPR: a plate/vehicle read. Same shape and confidence gate as the dedicated
        # ANPR engine — the plate lives in the nested <ANPR> block.
        if event_type == "ANPR":
            anpr = ANPREvent.from_alert(event)
            min_conf = self.config.anpr.min_confidence.value
            if anpr.confidence is not None and min_conf and anpr.confidence < min_conf:
                log.info(
                    f"Ignoring low-confidence plate {anpr.plate} "
                    f"({anpr.confidence} < {min_conf})"
                )
                return
            log.info(f"ANPR read: plate={anpr.plate} vehicle={anpr.vehicle_type}")
            await self._invoke(self.on_anpr_event_callback, anpr)
            return

        # PPE: a person without a hard hat. The rule only fires on a violation, so an
        # active event is itself the alert.
        if event_type in HARD_HAT_EVENT_TYPES:
            if event.get("eventState", "active") != "active":
                return
            ppe = PPEEvent.from_alert(event)
            log.info(f"PPE violation: hard hat missing (count={ppe.no_hardhat}).")
            await self._invoke(self.on_ppe_event_callback, ppe)
            return

        # Basic motion detection: the daytime trigger. Unclassified by design — the
        # frame is what matters, and the classifying happens in the cloud. Only the
        # leading edge is a trigger; "inactive" is the clear.
        if event_type == "VMD":
            if event.get("eventState", "active") != "active":
                return
            if self.config.is_night():
                # Night belongs to the classified intrusion rule, which is what should
                # be deciding whether to wake a siren.
                return
            log.info("Daytime motion (VMD) detected.")
            await self._invoke(
                self.on_motion_event_callback,
                MotionDetectEvent(MotionDetectEventType.motion, event),
            )
            return

        # Perimeter analytics are the night trigger only. During the day VMD has already
        # captured this target, so passing the smart event on as well would double every
        # daytime trigger — a snapshot, an upload and a cloud inference run each.
        if not self.config.is_night():
            if event_type in SMART_EVENT_TYPES:
                log.debug(f"Ignoring daytime {event_type}; VMD drives daytime capture.")
            return

        # Everything else (fielddetection / linedetection perimeter events, the
        # intruder alarm) is the AcuSense base's job.
        await super().on_cam_event(event)
