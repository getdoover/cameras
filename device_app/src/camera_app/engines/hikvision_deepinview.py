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

Everything else (fielddetection / linedetection smart events, the intruder alarm,
zones) is handled by the AcuSense base.

Because DeepinView subclasses AcuSense, the ``isinstance(engine, HikvisionAcuSense
Camera)`` checks in application.py (clock sync, native arming schedule, intruder
handling) automatically apply to this engine too.
"""

import logging

from ..events import ANPREvent, PPEEvent
from .hikvision_acusense import HikvisionAcuSenseCamera


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

        return True

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

        # Everything else (fielddetection / linedetection perimeter events, the
        # intruder alarm) is the AcuSense base's job.
        await super().on_cam_event(event)
