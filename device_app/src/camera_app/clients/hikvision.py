"""Hikvision ISAPI Client.

HTTP client for Hikvision IP cameras using the ISAPI (Intelligent Security API).
Hikvision uses digest authentication and returns XML responses.

Written by Josh Bramley, Doover.
"""

import logging
import re
import socket
import asyncio
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape

import aiohttp
import async_timeout

from .dahua import DigestAuth
from ..events import EVENT_IMAGE_KEY, TARGET_REGIONS_KEY

_LOGGER: logging.Logger = logging.getLogger(__package__)

TIMEOUT_SECONDS = 20

# Hikvision XML namespace
ISAPI_NS = "http://www.hikvision.com/ver20/XMLSchema"
NS = {"hik": ISAPI_NS}

ALERT_END = b"</EventNotificationAlert>"
ALERT_START = b"<EventNotificationAlert"

# The alertStream is multipart, and the parts that aren't XML are the camera's own JPEG of
# the event. Bounds on collecting one: how long to keep an alert waiting for its picture
# before giving up on it, and how much unparsed stream to hold before deciding we've lost
# sync and starting again. A picture measured 199KB (720p, AcuSense) to 416KB (1080p,
# DeepinView).
EVENT_IMAGE_WAIT_SECS = 3
MAX_STREAM_BUFFER = 8 * 1024 * 1024

# Hikvision expresses smart-detection coordinates against a virtual screen of this
# size rather than the real resolution (reported as <normalizedScreenSize>).
NORMALIZED_SCREEN = 1000

# How far the default region-entrance polygon sits in from the frame edge, in normalized
# units. Entrance needs the target seen outside the region before it crosses in, so
# unlike intrusion this cannot be full-frame -- see _default_region_entrance_body.
ENTRANCE_INSET = 250

# Default seconds a target must be inside an intrusion region before the rule fires. This
# is the "regardless of how fast they run in" knob: anything above zero silently misses
# anyone who crosses the region quicker than that, and the stock rule shipped **5**, which
# is enough to miss a vehicle entirely. Zero is the camera's own default and the bottom of
# its advertised range (`timeThreshold min="0" max="60" def="0"`), and means "report it as
# soon as you've classified it".
#
# Only a default: it is per-zone and user-editable through the zone editor, so nothing here
# overwrites a threshold somebody has set (see set_field_detection).
INTRUSION_DWELL_SECS = 0
INTRUSION_DWELL_MAX_SECS = 60

# A zone whose points come this close to the frame edge leaves too little room for a
# target to be tracked outside it, so entrance may never fire for that zone. Used to warn
# rather than to alter what the user drew.
ENTRANCE_EDGE_WARN = 100


def _strip_ns(tag: str) -> str:
    """Strip XML namespace from a tag name."""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _xml_to_dict(element: ET.Element) -> dict:
    """Recursively convert an XML element to a flat dictionary with dotted keys."""
    result = {}
    for child in element:
        key = _strip_ns(child.tag)
        if len(child):
            sub = _xml_to_dict(child)
            for sub_key, sub_val in sub.items():
                result[f"{key}.{sub_key}"] = sub_val
        else:
            result[key] = child.text or ""
    return result


class HikvisionClient:
    """
    HikvisionClient is the client for accessing Hikvision IP cameras via ISAPI.

    ISAPI documentation: Hikvision ISAPI protocol specification.
    """

    def __init__(
        self,
        username: str,
        password: str,
        address: str,
        port: int,
        rtsp_port: int,
        session: aiohttp.ClientSession,
    ) -> None:
        self._username = username
        self._password = password
        self._address = address
        self._session = session
        self._port = port
        self._rtsp_port = rtsp_port

        protocol = "https" if int(port) == 443 else "http"
        self._base = f"{protocol}://{address}:{port}"

    def get_rtsp_stream_url(self, channel: int = 1, subtype: int = 0) -> str:
        """
        Returns the RTSP url for the given channel.
        Hikvision channel IDs: 101=ch1 main, 102=ch1 sub, 201=ch2 main, etc.
        subtype: 0=main (01), 1=sub (02)
        """
        stream_id = channel * 100 + subtype + 1
        return f"rtsp://{self._username}:{self._password}@{self._address}:{self._rtsp_port}/Streaming/Channels/{stream_id}"

    async def get_snapshot(self, channel: int = 1, subtype: int = 0) -> bytes:
        """
        Takes a snapshot from the camera and returns binary JPEG data.

        ``subtype`` picks the stream, as for :meth:`get_rtsp_stream_url`: 0=main
        (101), 1=sub (102). The sub stream is a ready-made thumbnail — measured
        640x360 / ~18KB against 1920x1080 / ~117KB on the main stream — so it saves
        scaling one ourselves. Note this firmware ignores the
        ``videoResolutionWidth``/``Height`` query params, so picking the stream is
        the only way to ask for a smaller picture.
        """
        stream_id = channel * 100 + subtype + 1
        return await self.get_bytes(f"/ISAPI/Streaming/Channels/{stream_id}/picture")

    async def get_status(self) -> bool:
        """Check if the camera is reachable."""
        try:
            await self.get("/ISAPI/System/status")
        except Exception as e:
            _LOGGER.info(f"Failed to get camera status: {e}.")
            return False
        else:
            return True

    async def get_device_info(self) -> dict:
        """
        Get device information. Returns dict with keys like:
        deviceName, deviceID, model, serialNumber, firmwareVersion, etc.
        """
        try:
            return await self.get("/ISAPI/System/deviceInfo")
        except aiohttp.ClientResponseError:
            return {}

    async def get_system_status(self) -> dict:
        """
        Get system status including CPU, memory usage, uptime.
        """
        try:
            return await self.get("/ISAPI/System/status")
        except aiohttp.ClientResponseError:
            return {}

    async def get_time(self) -> dict:
        """Get the camera's current time settings."""
        try:
            return await self.get("/ISAPI/System/time")
        except aiohttp.ClientResponseError:
            return {}

    async def reboot(self) -> dict:
        """Reboots the device."""
        return await self.put("/ISAPI/System/reboot")

    @staticmethod
    def _hik_timezone(offset: timedelta) -> str:
        """Format a UTC offset the way Hikvision wants it.

        The sign is POSIX-style, i.e. inverted from the usual reading: UTC+10 is
        written ``CST-10:00:00``.
        """
        total = int(offset.total_seconds())
        sign = "-" if total >= 0 else "+"
        hours, remainder = divmod(abs(total), 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"CST{sign}{hours}:{minutes:02d}:{seconds:02d}"

    async def set_time(self, when: datetime) -> dict:
        """
        Push ``when`` (an aware, local datetime) onto the camera's clock.

        These cameras ship with ``timeMode=manual``, and with a flat RTC battery they
        come back from a power cut believing it's 2019 — which silently breaks
        anything time-based: the camera's arming schedule fires at the wrong hours and
        recording searches find nothing. Their configured NTP server is ignored while
        the mode is manual, and there's no NTP server reachable on the camera's LAN,
        so the app is the clock source and re-asserts it (see the engine's periodic
        drift check).
        """
        offset = when.utcoffset() or timedelta(0)
        body = (
            f'<Time version="2.0" xmlns="{ISAPI_NS}">'
            f"<timeMode>manual</timeMode>"
            f"<localTime>{when.strftime('%Y-%m-%dT%H:%M:%S')}</localTime>"
            f"<timeZone>{self._hik_timezone(offset)}</timeZone>"
            f"</Time>"
        )
        return await self.put("/ISAPI/System/time", body=body)

    # -- Thermal --

    async def get_thermal_capabilities(self) -> dict:
        """Get thermal channel capabilities."""
        try:
            return await self.get("/ISAPI/Thermal/channels/2/thermometry/capabilities")
        except aiohttp.ClientResponseError:
            return {}

    async def get_thermal_basic_param(self, channel: int = 2) -> dict:
        """
        Get basic thermal parameters (emissivity, distance, reflection temp, etc.)
        Channel 2 is typically the thermal channel on dual-sensor cameras.
        """
        try:
            return await self.get(f"/ISAPI/Thermal/channels/{channel}/thermometry/basicParam")
        except aiohttp.ClientResponseError:
            return {}

    async def set_thermal_basic_param(self, channel: int = 2, **params) -> dict:
        """
        Set basic thermal parameters.
        Common params: emissivity (0.0-1.0), distance (m), reflectiveTemperature (C).
        """
        # Build XML body
        root = ET.Element("ThermometryBasicParam")
        root.set("xmlns", ISAPI_NS)
        for key, value in params.items():
            child = ET.SubElement(root, key)
            child.text = str(value)

        body = ET.tostring(root, encoding="unicode")
        return await self.put(f"/ISAPI/Thermal/channels/{channel}/thermometry/basicParam", body=body)

    async def get_temperature_data(self, channel: int = 2) -> dict:
        """
        Get real-time temperature data from thermal channel.
        Returns temperature information including min/max/average temps.
        """
        try:
            return await self.get(f"/ISAPI/Thermal/channels/{channel}/thermometry/pixelToPixelParam")
        except aiohttp.ClientResponseError:
            return {}

    # -- Streaming --

    async def get_streaming_channels(self) -> dict:
        """Get a list of available streaming channels."""
        try:
            return await self.get("/ISAPI/Streaming/channels")
        except aiohttp.ClientResponseError:
            return {}

    async def get_streaming_channel(self, channel_id: int = 101) -> dict:
        """Get info for a specific streaming channel (101=ch1 main, 102=ch1 sub)."""
        try:
            return await self.get(f"/ISAPI/Streaming/channels/{channel_id}")
        except aiohttp.ClientResponseError:
            return {}

    # -- Image settings --

    async def get_image_settings(self, channel: int = 1) -> dict:
        """Get image settings (brightness, contrast, saturation, etc.)."""
        try:
            return await self.get(f"/ISAPI/Image/channels/{channel}")
        except aiohttp.ClientResponseError:
            return {}

    async def get_ir_cut_filter(self, channel: int = 1) -> dict:
        """Get IR cut filter (day/night mode) status."""
        try:
            return await self.get(f"/ISAPI/Image/channels/{channel}/ircutFilter")
        except aiohttp.ClientResponseError:
            return {}

    async def set_ir_cut_filter(self, channel: int = 1, mode: str = "auto") -> dict:
        """
        Set IR cut filter mode.
        mode: 'auto', 'day', 'night'
        Maps to Hikvision values: auto, day, night
        """
        root = ET.Element("IrcutFilter")
        root.set("xmlns", ISAPI_NS)
        mode_elem = ET.SubElement(root, "IrcutFilterType")
        mode_elem.text = mode
        body = ET.tostring(root, encoding="unicode")
        return await self.put(f"/ISAPI/Image/channels/{channel}/ircutFilter", body=body)

    # -- Event / Alarm --

    async def get_motion_detection(self, channel: int = 1) -> dict:
        """Get motion detection configuration."""
        try:
            return await self.get(f"/ISAPI/System/Video/inputs/channels/{channel}/motionDetection")
        except aiohttp.ClientResponseError:
            return {}

    async def set_motion_detection(self, channel: int = 1, enabled: bool = True) -> dict:
        """Enable or disable motion detection."""
        root = ET.Element("MotionDetection")
        root.set("xmlns", ISAPI_NS)
        enabled_elem = ET.SubElement(root, "enabled")
        enabled_elem.text = str(enabled).lower()
        body = ET.tostring(root, encoding="unicode")
        return await self.put(
            f"/ISAPI/System/Video/inputs/channels/{channel}/motionDetection",
            body=body,
        )

    async def get_event_triggers(self) -> dict:
        """Get event notification triggers."""
        try:
            return await self.get("/ISAPI/Event/triggers")
        except aiohttp.ClientResponseError:
            return {}

    # -- ANPR (Road Traffic / vehicle + license plate) --

    async def get_vehicle_detection(self, channel: int = 1) -> dict:
        """Get the road-traffic vehicle/ANPR detection config."""
        try:
            return await self.get(f"/ISAPI/Traffic/channels/{channel}/vehicleDetect")
        except aiohttp.ClientResponseError:
            return {}

    async def enable_vehicle_detection(self, enabled: bool, channel: int = 1) -> dict:
        """
        Enable/disable on-camera ANPR (vehicle + license plate).

        The lane/scene calibration is left untouched — we only flip the
        top-level <enabled> flag, so we GET the current config and PUT it back
        with just that field changed (the config is large and nested, and the
        camera rejects partial bodies).
        """
        raw = (await self.get_bytes(
            f"/ISAPI/Traffic/channels/{channel}/vehicleDetect"
        )).decode(errors="ignore")
        value = "true" if enabled else "false"
        # Only the first <enabled> is the top-level toggle; per-scene <enabled>
        # flags deeper in the body must be preserved.
        new = re.sub(r"<enabled>.*?</enabled>", f"<enabled>{value}</enabled>", raw, count=1)
        return await self.put(
            f"/ISAPI/Traffic/channels/{channel}/vehicleDetect", body=new
        )

    # -- PPE / hard-hat detection (DeepinView deep-learning analytics) --

    # Where the hard-hat rule lives. NOTE: taken from the DeepinView ISAPI analytics
    # convention (/ISAPI/Smart/<Feature>/<channel>); the exact segment casing for this
    # newer algorithm isn't in the public spec, so verify against a real
    # GET /ISAPI/Smart/capabilities (or the camera's web UI network trace) on the bench
    # and adjust if the camera 404s.
    HARD_HAT_ENDPOINT = "/ISAPI/Smart/HardHatDetection/{channel}"

    async def get_hard_hat_detection(self, channel: int = 1) -> dict:
        """Get the PPE / hard-hat detection config."""
        try:
            return await self.get(self.HARD_HAT_ENDPOINT.format(channel=channel))
        except aiohttp.ClientResponseError:
            return {}

    def _default_hard_hat_body(
        self, enabled: bool, sensitivity: int, channel: int
    ) -> str:
        """Build a full-frame hard-hat rule for a camera that has none configured yet.

        Mirrors :meth:`_default_field_detection_body` — a rectangle inset from the
        edges so it covers the whole scene, in Hikvision normalized (0-1000) units.
        """
        value = "true" if enabled else "false"
        corners = ((10, 10), (990, 10), (990, 990), (10, 990))
        coords = "".join(
            f"<RegionCoordinates><positionX>{x}</positionX>"
            f"<positionY>{y}</positionY></RegionCoordinates>"
            for x, y in corners
        )
        return (
            f'<HardHatDetection version="2.0" xmlns="{ISAPI_NS}">'
            f"<enabled>{value}</enabled>"
            f"<normalizedScreenSize>"
            f"<normalizedScreenWidth>{NORMALIZED_SCREEN}</normalizedScreenWidth>"
            f"<normalizedScreenHeight>{NORMALIZED_SCREEN}</normalizedScreenHeight>"
            f"</normalizedScreenSize>"
            f"<HardHatDetectionRegionList>"
            f"<HardHatDetectionRegion>"
            f"<id>1</id><enabled>true</enabled>"
            f"<sensitivityLevel>{sensitivity}</sensitivityLevel>"
            f"<RegionCoordinatesList>{coords}</RegionCoordinatesList>"
            f"</HardHatDetectionRegion>"
            f"</HardHatDetectionRegionList>"
            f"</HardHatDetection>"
        )

    async def set_hard_hat_detection(
        self, enabled: bool, sensitivity: int = 50, channel: int = 1
    ) -> dict:
        """
        Enable/disable on-camera PPE (hard-hat) detection and set its sensitivity.

        Like :meth:`set_field_detection`, this GET-modify-PUTs an existing rule so the
        camera's own region/calibration is preserved, and only PUTs a default
        full-frame rule when there's none configured yet.
        """
        endpoint = self.HARD_HAT_ENDPOINT.format(channel=channel)
        try:
            raw = (await self.get_bytes(endpoint)).decode(errors="ignore")
        except Exception:
            raw = ""

        if "<HardHatDetectionRegion" not in raw:
            body = self._default_hard_hat_body(enabled, sensitivity, channel)
            return await self.put(endpoint, body=body)

        value = "true" if enabled else "false"
        # First <enabled> is the rule; second (if any) is region 1.
        raw = re.sub(r"<enabled>.*?</enabled>", f"<enabled>{value}</enabled>", raw, count=1)
        if "<sensitivityLevel>" in raw:
            raw = re.sub(
                r"<sensitivityLevel>.*?</sensitivityLevel>",
                f"<sensitivityLevel>{sensitivity}</sensitivityLevel>",
                raw,
                count=1,
            )
        return await self.put(endpoint, body=raw)

    # -- Alarm output relay (external siren / strobe) --

    async def trigger_io_output(self, port: int = 1, active: bool = True) -> dict:
        """Manually drive an alarm-output relay high (active) or low."""
        state = "high" if active else "low"
        body = (
            f'<IOPortData version="2.0" xmlns="{ISAPI_NS}">'
            f"<outputState>{state}</outputState></IOPortData>"
        )
        return await self.put(f"/ISAPI/System/IO/outputs/{port}/trigger", body=body)

    async def pulse_io_output(self, port: int = 1, duration: float = 5.0) -> None:
        """
        Pulse an alarm-output relay for ``duration`` seconds.

        We drive it high, wait, then low, rather than relying on the relay's own
        pulse config so the duration is controlled by us and independent of how
        the camera's PowerOnState happens to be set.
        """
        await self.trigger_io_output(port, active=True)
        try:
            await asyncio.sleep(duration)
        finally:
            await self.trigger_io_output(port, active=False)

    # -- Motion -> beep linkage (doover-independent local alarm) --

    _BEEP_NOTIFICATION = (
        "<EventTriggerNotification><id>beep</id>"
        "<notificationMethod>beep</notificationMethod>"
        "<notificationRecurrence>beginning</notificationRecurrence>"
        "</EventTriggerNotification>"
    )

    async def set_motion_beep_linkage(self, enabled: bool, channel: int = 1) -> dict:
        """
        Arm/disarm the camera's own buzzer on motion (VMD).

        This firmware's VMD event trigger only accepts record/center/beep
        notification methods — alarm-output (relay) linkage is NOT supported via
        the API, so the external siren/strobe must be pulsed by the app instead
        (see :meth:`pulse_io_output`). ``beep`` is the one linkage that fires
        locally even if doover is offline. We GET-modify-PUT the raw trigger so
        any existing record/center notifications are preserved.
        """
        trigger_id = f"VMD-{channel}"
        raw = (await self.get_bytes(
            f"/ISAPI/Event/triggers/{trigger_id}"
        )).decode(errors="ignore")

        has_beep = "<notificationMethod>beep</notificationMethod>" in raw
        if enabled and not has_beep:
            raw = raw.replace(
                "</EventTriggerNotificationList>",
                f"{self._BEEP_NOTIFICATION}</EventTriggerNotificationList>",
                1,
            )
        elif not enabled and has_beep:
            raw = re.sub(
                r"<EventTriggerNotification>\s*<id>beep</id>.*?</EventTriggerNotification>",
                "",
                raw,
                count=1,
                flags=re.DOTALL,
            )
        else:
            return {"status": "unchanged"}

        return await self.put(f"/ISAPI/Event/triggers/{trigger_id}", body=raw)

    # -- AcuSense perimeter analytics (line / intrusion, human/vehicle) --

    async def get_field_detection(self, channel: int = 1) -> dict:
        """Get the intrusion (field) detection config."""
        try:
            return await self.get(f"/ISAPI/Smart/FieldDetection/{channel}")
        except aiohttp.ClientResponseError:
            return {}

    def _default_field_detection_body(
        self, enabled: bool, targets: list, sensitivity: int, channel: int
    ) -> str:
        """Build a full FieldDetection config with one full-frame region.

        Used when the camera has no intrusion rule configured yet. Coordinates are
        Hikvision normalized screen units (0-1000), and the region is a rectangle
        inset slightly from the edges so it covers the whole scene. NOTE: the exact
        element names (``sensitivityLevel``, ``timeThreshold``) and the normalized
        coordinate range vary by firmware — verify against a real GET of
        ``/ISAPI/Smart/FieldDetection/{channel}`` and adjust if the camera rejects
        this body.
        """
        value = "true" if enabled else "false"
        target = ",".join(targets) if targets else "human,vehicle"
        corners = ((10, 10), (990, 10), (990, 990), (10, 990))
        coords = "".join(
            f"<RegionCoordinates><positionX>{x}</positionX>"
            f"<positionY>{y}</positionY></RegionCoordinates>"
            for x, y in corners
        )
        return (
            f'<FieldDetection version="2.0" xmlns="{ISAPI_NS}">'
            f"<enabled>{value}</enabled>"
            f"<normalizedScreenSize>"
            f"<normalizedScreenWidth>1000</normalizedScreenWidth>"
            f"<normalizedScreenHeight>1000</normalizedScreenHeight>"
            f"</normalizedScreenSize>"
            f"<FieldDetectionRegionList>"
            f"<FieldDetectionRegion>"
            f"<id>1</id><enabled>true</enabled>"
            f"<sensitivityLevel>{sensitivity}</sensitivityLevel>"
            f"<timeThreshold>{INTRUSION_DWELL_SECS}</timeThreshold>"
            f"<RegionCoordinatesList>{coords}</RegionCoordinatesList>"
            f"<detectionTarget>{target}</detectionTarget>"
            f"</FieldDetectionRegion>"
            f"</FieldDetectionRegionList>"
            f"</FieldDetection>"
        )

    def _default_region_entrance_body(
        self, enabled: bool, targets: list, sensitivity: int
    ) -> str:
        """Build a RegionEntrance config with one full-frame region.

        Verified against a real GET of ``/ISAPI/Smart/regionEntrance/1``, which differs
        from FieldDetection in three ways that matter:

        * regions carry **no** ``<enabled>`` and no ``<timeThreshold>``
        * they ship with an **empty** ``RegionCoordinatesList``, so a polygon has to be
          written or the rule matches nothing
        * there is no ``contAlarmForStaticTargetEnabled`` -- which is the entire point
          of using it: entrance fires once when a target crosses into the region and
          does not re-alarm while it sits there.
        """
        value = "true" if enabled else "false"
        target = ",".join(targets) if targets else "human,vehicle"
        # NOT full-frame, unlike the intrusion default -- and this is the whole trick.
        #
        # Entrance fires when a target is tracked *outside* the region and then crosses
        # in, so the region needs real space around it. A near-full-frame region (the
        # intrusion default is inset by 1%) leaves nowhere to be outside, and the rule
        # then never fires at all: measured on a real camera, walking through frame
        # produced zero regionEntrance events at 10..990 and fired reliably at 250..750.
        #
        # The cost is that a target crossing only the outer quarter of frame is missed.
        # That is inherent to entrance semantics, not a tuning choice.
        corners = (
            (ENTRANCE_INSET, ENTRANCE_INSET),
            (NORMALIZED_SCREEN - ENTRANCE_INSET, ENTRANCE_INSET),
            (NORMALIZED_SCREEN - ENTRANCE_INSET, NORMALIZED_SCREEN - ENTRANCE_INSET),
            (ENTRANCE_INSET, NORMALIZED_SCREEN - ENTRANCE_INSET),
        )
        coords = "".join(
            f"<RegionCoordinates><positionX>{x}</positionX>"
            f"<positionY>{y}</positionY></RegionCoordinates>"
            for x, y in corners
        )
        return (
            f'<RegionEntrance version="2.0" xmlns="{ISAPI_NS}">'
            f"<enabled>{value}</enabled>"
            f"<normalizedScreenSize>"
            f"<normalizedScreenWidth>{NORMALIZED_SCREEN}</normalizedScreenWidth>"
            f"<normalizedScreenHeight>{NORMALIZED_SCREEN}</normalizedScreenHeight>"
            f"</normalizedScreenSize>"
            f"<RegionEntranceRegionList>"
            f"<RegionEntranceRegion>"
            f"<id>1</id>"
            f"<sensitivityLevel>{sensitivity}</sensitivityLevel>"
            f"<RegionCoordinatesList>{coords}</RegionCoordinatesList>"
            f"<detectionTarget>{target}</detectionTarget>"
            f"</RegionEntranceRegion>"
            f"</RegionEntranceRegionList>"
            f"</RegionEntrance>"
        )

    async def set_region_entrance(
        self,
        enabled: bool,
        targets: list | None = None,
        sensitivity: int = 50,
        channel: int = 1,
    ) -> dict:
        """Create/enable region-entrance detection: one event per target arriving.

        The daytime counterpart to :meth:`set_field_detection`. Intrusion
        (``fielddetection``) has ``contAlarmForStaticTargetEnabled`` on by default and
        re-alarms every ``targetAlarmInterval`` while a target stays inside the region,
        so a car that parks in frame keeps producing events -- one snapshot, upload and
        inference each. Entrance fires once on arrival instead.

        Note the path casing: ``/ISAPI/Smart/regionEntrance`` is camelCase where
        ``/ISAPI/Smart/FieldDetection`` is PascalCase. The firmware is strict about it.
        """
        body = self._default_region_entrance_body(enabled, targets, sensitivity)
        return await self.put(f"/ISAPI/Smart/regionEntrance/{channel}", body=body)

    async def set_field_detection(
        self,
        enabled: bool,
        targets: list = None,
        sensitivity: int = 50,
        channel: int = 1,
    ) -> dict:
        """
        Create/enable intrusion (field) detection and set its target + sensitivity.

        If the camera already has a rule (with its own region/calibration) we
        GET-modify-PUT it, preserving the region and only touching the enable flags,
        target filter, sensitivity and dwell time. If there is no rule yet we PUT a
        default full-frame region so the feature works out of the box. ``targets`` is a
        list of Hikvision target tokens (``human``, ``vehicle``, ...).

        The dwell time is deliberately **not** touched on an existing rule: it is per-zone
        and owned by whoever drew the zone (see ``set_field_detection_regions``), so
        asserting a default here would silently undo their setting on every app start. A
        rule this app creates from scratch gets :data:`INTRUSION_DWELL_SECS`.
        """
        try:
            raw = (await self.get_bytes(
                f"/ISAPI/Smart/FieldDetection/{channel}"
            )).decode(errors="ignore")
        except Exception:
            raw = ""

        if "<FieldDetectionRegion" not in raw:
            # No rule configured on the camera — create a default full-frame one.
            body = self._default_field_detection_body(
                enabled, targets, sensitivity, channel
            )
            return await self.put(
                f"/ISAPI/Smart/FieldDetection/{channel}", body=body
            )

        value = "true" if enabled else "false"
        # First <enabled> is the rule; second is region 1.
        raw = re.sub(r"<enabled>.*?</enabled>", f"<enabled>{value}</enabled>", raw, count=2)
        if targets:
            raw = re.sub(
                r"<detectionTarget>.*?</detectionTarget>",
                f"<detectionTarget>{','.join(targets)}</detectionTarget>",
                raw,
                count=1,
            )
        if "<sensitivityLevel>" in raw:
            raw = re.sub(
                r"<sensitivityLevel>.*?</sensitivityLevel>",
                f"<sensitivityLevel>{sensitivity}</sensitivityLevel>",
                raw,
                count=1,
            )
        return await self.put(f"/ISAPI/Smart/FieldDetection/{channel}", body=raw)

    @staticmethod
    def _rewrite_element(body: str, tag: str, value: str, count: int = 0) -> str:
        """Set an element's text wherever it already appears; leave an absent tag alone.

        Absent means this firmware doesn't have the feature, and inventing the element is
        worse than skipping it -- these cameras answer 400 "Invalid XML Content" on
        unexpected content rather than ignoring it, which would lose the whole write.
        Editing in place also keeps the schema's element order, which a rebuilt body can
        get wrong.

        ``count=0`` rewrites every occurrence: a rule carries one ``<timeThreshold>`` per
        region slot and they should all agree.
        """
        return re.sub(
            rf"<{tag}>.*?</{tag}>", f"<{tag}>{value}</{tag}>", body, count=count
        )

    async def set_static_target_alarm(self, enabled: bool, channel: int = 1) -> int:
        """Turn intrusion's re-alarm-on-a-static-target on or off.

        **The app wants this on**, which is the camera's own default, and asserts it rather
        than assuming it. With it on the rule re-fires every ``targetAlarmInterval`` for as
        long as a target stays in the region; with it off the camera reports a target once.

        That repeat is the *only* signal that an intruder is still present -- there is no
        "target still here" event and nothing to poll -- so the night alarm's whole
        "keep the siren going while they're there" behaviour is built on it, as is the
        camera's own light/buzzer linkage (``whiteLightDurationTime=0`` means "follow the
        event", and without re-alarms the event is a single instant). Switching it off to
        stop duplicate daytime snapshots looks tempting and silently breaks the night
        alarm; the snapshot cooldown in the app is the right place for that, because it can
        throttle the picture without throttling the alarm.

        Returns the camera's re-alarm interval in seconds, or ``None`` when it can't be
        read -- callers should treat None as "unknown", not as failure. The element is
        deliberately **not** created when absent, since these cameras answer 400 on
        unexpected content (and will also answer OK to a field and then ignore it).
        """
        endpoint = f"/ISAPI/Smart/FieldDetection/{channel}"
        tag = "contAlarmForStaticTargetEnabled"
        try:
            raw = (await self.get_bytes(endpoint)).decode(errors="ignore")
        except Exception as e:
            _LOGGER.info(f"Couldn't read intrusion config to set {tag}: {e}")
            return None

        interval = None
        match = re.search(r"<targetAlarmInterval>(\d+)</targetAlarmInterval>", raw)
        if match:
            interval = int(match.group(1))

        if f"<{tag}>" not in raw:
            _LOGGER.warning(
                f"Camera has no {tag}. If it does not re-alarm while a target stays in the "
                f"zone, the app cannot tell an intruder is still present and the night "
                f"alarm will stop one cooldown after the first detection."
            )
            return interval

        body = self._rewrite_element(raw, tag, "true" if enabled else "false")
        try:
            await self.put(endpoint, body=body)
        except Exception as e:
            _LOGGER.warning(f"Failed to set {tag}: {e}")
            return interval
        _LOGGER.info(
            f"Intrusion re-alarm on a static target {'on' if enabled else 'OFF'} "
            f"(interval={interval if interval is not None else 'unknown'}s)."
        )
        return interval

    async def get_field_detection_regions(self, channel: int = 1) -> list:
        """
        Read the intrusion rule's regions in native (0..1000) coordinates.

        Returns one dict per region with ``id``, ``enabled``, ``points``,
        ``targets`` and ``sensitivity``. The camera always reports its full set of
        region slots, so unconfigured ones come back with no points.
        """
        try:
            raw = await self.get_bytes(f"/ISAPI/Smart/FieldDetection/{channel}")
            root = ET.fromstring(raw.decode(errors="ignore"))
        except (ET.ParseError, Exception):
            return []

        regions = []
        for element in root.iter():
            if _strip_ns(element.tag) != "FieldDetectionRegion":
                continue

            region = {
                "points": [],
                "targets": [],
                "sensitivity": None,
                "time_threshold": None,
            }
            for child in element.iter():
                tag = _strip_ns(child.tag)
                text = (child.text or "").strip()
                if tag == "id" and "id" not in region:
                    region["id"] = int(text or 0)
                elif tag == "enabled":
                    region["enabled"] = text == "true"
                elif tag == "sensitivityLevel":
                    region["sensitivity"] = int(text or 0)
                elif tag == "timeThreshold":
                    region["time_threshold"] = int(text or 0)
                elif tag == "detectionTarget":
                    region["targets"] = [t for t in text.split(",") if t]
                elif tag == "RegionCoordinates":
                    coords = _xml_to_dict(child)
                    region["points"].append(
                        (int(coords.get("positionX", 0)), int(coords.get("positionY", 0)))
                    )
            regions.append(region)
        return regions

    @staticmethod
    def _field_detection_region(region: dict) -> str:
        coords = "".join(
            f"<RegionCoordinates><positionX>{x}</positionX>"
            f"<positionY>{y}</positionY></RegionCoordinates>"
            for x, y in region["points"]
        )
        return (
            f"<FieldDetectionRegion><id>{region['id']}</id>"
            f"<enabled>true</enabled>"
            f"<sensitivityLevel>{region['sensitivity']}</sensitivityLevel>"
            f"<timeThreshold>{region.get('time_threshold', INTRUSION_DWELL_SECS)}</timeThreshold>"
            f"<RegionCoordinatesList>{coords}</RegionCoordinatesList>"
            f"<detectionTarget>{','.join(region['targets'])}</detectionTarget>"
            f"</FieldDetectionRegion>"
        )

    async def set_field_detection_regions(self, regions: list, channel: int = 1) -> dict:
        """
        Replace the intrusion rule's regions.

        ``regions`` is a list of dicts with ``id``, ``points`` (native 0..1000 (x, y)
        tuples), ``targets`` (Hikvision tokens) and ``sensitivity``. A region with no
        points clears that slot — callers should pass every slot they want blanked,
        since one left out of the body keeps whatever it had.

        The rule-level ``<enabled>`` is preserved from the current config. Note the
        camera ignores each region's own ``<enabled>`` — it answers OK and leaves it
        false — so a region can't be individually switched off this way.
        """
        raw = (await self.get_bytes(
            f"/ISAPI/Smart/FieldDetection/{channel}"
        )).decode(errors="ignore")
        match = re.search(r"<enabled>(.*?)</enabled>", raw)
        rule_enabled = match.group(1) if match else "true"

        blocks = "".join(self._field_detection_region(r) for r in regions)
        body = (
            f'<FieldDetection version="2.0" xmlns="{ISAPI_NS}">'
            f"<id>{channel}</id><enabled>{rule_enabled}</enabled>"
            f"<normalizedScreenSize>"
            f"<normalizedScreenWidth>{NORMALIZED_SCREEN}</normalizedScreenWidth>"
            f"<normalizedScreenHeight>{NORMALIZED_SCREEN}</normalizedScreenHeight>"
            f"</normalizedScreenSize>"
            f"<FieldDetectionRegionList>{blocks}</FieldDetectionRegionList>"
            f"</FieldDetection>"
        )
        return await self.put(f"/ISAPI/Smart/FieldDetection/{channel}", body=body)

    @staticmethod
    def _region_entrance_region(region: dict) -> str:
        """One RegionEntrance region block.

        Differs from :meth:`_field_detection_region` in what the firmware accepts: no
        per-region ``<enabled>`` and no ``<timeThreshold>``. Verified against a real GET.
        """
        coords = "".join(
            f"<RegionCoordinates><positionX>{x}</positionX>"
            f"<positionY>{y}</positionY></RegionCoordinates>"
            for x, y in region["points"]
        )
        return (
            f"<RegionEntranceRegion><id>{region['id']}</id>"
            f"<sensitivityLevel>{region['sensitivity']}</sensitivityLevel>"
            f"<RegionCoordinatesList>{coords}</RegionCoordinatesList>"
            f"<detectionTarget>{','.join(region['targets'])}</detectionTarget>"
            f"</RegionEntranceRegion>"
        )

    async def set_region_entrance_regions(
        self, regions: list, channel: int = 1
    ) -> dict:
        """Replace the region-entrance rule's regions.

        Same ``regions`` shape as :meth:`set_field_detection_regions`, so one set of
        user-drawn zones can be written to both rules and mean the same thing.
        """
        raw = (
            await self.get_bytes(f"/ISAPI/Smart/regionEntrance/{channel}")
        ).decode(errors="ignore")
        match = re.search(r"<enabled>(.*?)</enabled>", raw)
        rule_enabled = match.group(1) if match else "true"

        blocks = "".join(self._region_entrance_region(r) for r in regions)
        body = (
            f'<RegionEntrance version="2.0" xmlns="{ISAPI_NS}">'
            f"<id>{channel}</id><enabled>{rule_enabled}</enabled>"
            f"<normalizedScreenSize>"
            f"<normalizedScreenWidth>{NORMALIZED_SCREEN}</normalizedScreenWidth>"
            f"<normalizedScreenHeight>{NORMALIZED_SCREEN}</normalizedScreenHeight>"
            f"</normalizedScreenSize>"
            f"<RegionEntranceRegionList>{blocks}</RegionEntranceRegionList>"
            f"</RegionEntrance>"
        )
        return await self.put(f"/ISAPI/Smart/regionEntrance/{channel}", body=body)

    async def disable_smart_rule(self, rule: str, channel: int = 1) -> bool:
        """Turn a smart-analytics rule off, leaving its regions untouched.

        GET-modify-PUT of the rule-level ``<enabled>`` only, so somebody's drawn
        polygons survive being switched off and come back if it's re-enabled. Only the
        first ``<enabled>`` is touched: the later ones belong to regions, which this
        firmware reports but ignores on write anyway.

        ``rule`` is the ISAPI path segment, and the casing matters --
        ``regionExiting`` / ``LineDetection`` / ``FieldDetection`` are not uniform.
        Returns whether the camera accepted it.
        """
        try:
            raw = (
                await self.get_bytes(f"/ISAPI/Smart/{rule}/{channel}")
            ).decode(errors="ignore")
        except Exception as e:
            _LOGGER.info(f"Couldn't read {rule} to disable it: {e}")
            return False

        if "<enabled>false</enabled>" in raw.replace(" ", "")[:400]:
            return True  # already off; don't spend a PUT on it

        body = re.sub(
            r"<enabled>.*?</enabled>", "<enabled>false</enabled>", raw, count=1
        )
        try:
            await self.put(f"/ISAPI/Smart/{rule}/{channel}", body=body)
        except Exception as e:
            _LOGGER.warning(f"Failed to disable {rule}: {e}")
            return False
        return True

    async def get_line_detection(self, channel: int = 1) -> dict:
        """Get the line-crossing detection config."""
        try:
            return await self.get(f"/ISAPI/Smart/LineDetection/{channel}")
        except aiohttp.ClientResponseError:
            return {}

    # -- ColorVu supplement light (built-in white-light deterrent) --

    async def set_supplement_light_mode(self, mode: str, channel: int = 1) -> dict:
        """
        Set the ColorVu supplement-light mode.

        ``eventIntelligence`` keeps the IR light on for normal night vision and
        flashes the white light on a smart event (the built-in deterrent);
        ``colorVuWhiteLight`` is always-on white; ``irLight`` is IR only;
        ``close`` is off.
        """
        raw = (await self.get_bytes(
            f"/ISAPI/Image/channels/{channel}/supplementLight"
        )).decode(errors="ignore")
        raw = re.sub(
            r"<supplementLightMode>.*?</supplementLightMode>",
            f"<supplementLightMode>{mode}</supplementLightMode>",
            raw,
            count=1,
        )
        return await self.put(f"/ISAPI/Image/channels/{channel}/supplementLight", body=raw)

    async def get_supplement_light_mode(self, channel: int = 1) -> str:
        cfg = {}
        try:
            cfg = await self.get(f"/ISAPI/Image/channels/{channel}/supplementLight")
        except aiohttp.ClientResponseError:
            pass
        return cfg.get("supplementLightMode", "")

    # Linkages this client owns on a smart-event trigger. Anything else already on
    # the trigger (supplementLight, customOverHTTP, center, ...) is preserved.
    # Verified against a DS-2CD2387G3-LIS2UY/SRB: these are the exact ids its own web
    # UI writes — there is no combined flash+siren token on this firmware.
    _MANAGED_LINKAGES = ("whiteLight", "audio", "record")

    # "Notify Surveillance Center" — the linkage that pushes an event onto the
    # alertStream. It is never optional and is written whether we're arming or
    # disarming: without it the camera handles the event entirely on its own (flash,
    # siren, recording all fire) and the app never hears that anything happened. The
    # stock fielddetection trigger ships without it, unlike VMD/IO.
    _STREAM_LINKAGE = "center"

    def _managed_linkage_ids(self, channel: int = 1) -> set:
        """Trigger ids we own, and therefore strip before writing.

        ``beep`` is in here despite not being one of ours to set: the camera adds it
        by itself alongside ``audio``, so unless it's stripped too it survives a
        disarm and the buzzer keeps sounding.
        """
        return {self._linkage_id(t, channel) for t in self._MANAGED_LINKAGES} | {
            "beep",
            self._STREAM_LINKAGE,
        }

    # The web UI writes 0 here, which the camera reads as "follow the event" rather
    # than a fixed number of seconds.
    _WHITE_LIGHT_DURATION = 0

    @staticmethod
    def _linkage_id(token: str, channel: int = 1) -> str:
        """The trigger's id for a linkage, which isn't always the method name.

        ``record`` is per video input, so the camera ids it ``record-<channel>``.
        """
        return f"record-{channel}" if token == "record" else token

    def _linkage_notification(self, token: str, channel: int = 1) -> str:
        body = (
            f"<EventTriggerNotification><id>{self._linkage_id(token, channel)}</id>"
            f"<notificationMethod>{token}</notificationMethod>"
            f"<notificationRecurrence>beginning</notificationRecurrence>"
        )
        if token == "whiteLight":
            # The camera rejects a whiteLight linkage that omits this child.
            body += (
                f"<WhiteLightAction><whiteLightDurationTime>"
                f"{self._WHITE_LIGHT_DURATION}"
                f"</whiteLightDurationTime></WhiteLightAction>"
            )
        elif token == "record":
            # ...and rejects a record linkage that doesn't say which input to record.
            body += f"<videoInputID>{channel}</videoInputID>"
        return body + "</EventTriggerNotification>"

    async def set_smart_alarm_linkage(
        self,
        enabled: bool,
        methods: list = None,
        event: str = "fielddetection",
        index: int = 1,
        channel: int = 1,
    ) -> dict:
        """
        Arm/disarm the built-in flash/siren "active response" on a smart-event
        trigger (e.g. ``fielddetection-1``).

        ``methods`` is a subset of :attr:`_MANAGED_LINKAGES` — ``whiteLight`` (the
        flash), ``audio`` (the siren) and ``record`` (write the event to the microSD,
        which is what makes clips fetchable later via ContentMgmt). Each is an
        independent notification: this firmware has **no** combined
        "LightAudioAlarm"/flash+siren token — the web UI adds ``whiteLight`` and
        ``audio`` side by side, so we do the same. These responses fire on-camera,
        independent of doover. We GET-modify-PUT the trigger so existing notifications
        (``supplementLight``, ``customOverHTTP``, ...) are preserved, first stripping
        the linkages we manage so re-arming is idempotent.

        We must send the *minimal* envelope the camera's own web UI sends — ``<id>``,
        ``<eventType>``, ``<videoInputChannelID>`` and the notification list, with no
        ``version``/``xmlns``/``eventDescription``/``dynVideoInputChannelID``.
        Echoing back the full GET body instead makes the camera answer OK and then
        silently ignore removals (the siren stays linked forever), so this rebuilds
        the body rather than editing the response in place. Notifications we don't
        own are carried across verbatim.
        """
        methods = set(methods or [])
        tokens = [t for t in self._MANAGED_LINKAGES if t in methods] if enabled else []
        # Kept on the trigger even when disarming — it's how we hear about the event
        # at all, not part of the deterrent.
        tokens.append(self._STREAM_LINKAGE)

        trigger_id = f"{event}-{index}"
        raw = (await self.get_bytes(
            f"/ISAPI/Event/triggers/{trigger_id}"
        )).decode(errors="ignore")

        managed = self._managed_linkage_ids(channel)
        preserved = [
            re.sub(r"\s+", " ", block).strip()
            for block in re.findall(
                r"<EventTriggerNotification>.*?</EventTriggerNotification>",
                raw,
                flags=re.DOTALL,
            )
            if not any(f"<id>{i}</id>" in block for i in managed)
        ]

        notifs = "".join(self._linkage_notification(t, channel) for t in tokens)
        body = (
            f"<EventTrigger><id>{trigger_id}</id>"
            f"<eventType>{event}</eventType>"
            f"<videoInputChannelID>{channel}</videoInputChannelID>"
            f"<EventTriggerNotificationList>"
            f"{notifs}{''.join(preserved)}"
            f"</EventTriggerNotificationList></EventTrigger>"
        )
        return await self.put(f"/ISAPI/Event/triggers/{trigger_id}", body=body)

    # -- Native arming schedule (so linkages fire on-camera, doover-independent) --

    # Where a smart event's arming schedule lives. Note the plural collection segment
    # and the "<eventType>_video<channel>" leaf — taken from what the camera's own
    # web UI PUTs, and not guessable from the trigger's own path.
    _SCHEDULE_ENDPOINT = "/ISAPI/Event/schedules/{collection}/{event}_video{channel}"

    # The collection segment isn't derivable from the event name -- it's the event's own
    # plural, with its own casing. Verified by GET against a real camera: anything else
    # returns 400 "Invalid XML Content" rather than a 404, so a wrong guess looks like a
    # malformed body instead of a bad path.
    _SCHEDULE_COLLECTIONS = {
        "fielddetection": "fieldDetections",
        "regionEntrance": "regionEntrances",
        "regionExiting": "regionExitings",
        "linedetection": "lineDetections",
    }

    @staticmethod
    def _night_time_segments(start_hour: int, end_hour: int) -> list:
        """Daily (begin, end) segments covering the night window.

        A window that wraps midnight (e.g. 18 -> 6) can't be expressed as one
        segment, so it becomes two on every day: 00:00-06:00 and 18:00-24:00.
        """
        if start_hour == end_hour:
            return []
        if start_hour < end_hour:
            return [(f"{start_hour:02d}:00:00", f"{end_hour:02d}:00:00")]
        segments = []
        if end_hour > 0:
            segments.append(("00:00:00", f"{end_hour:02d}:00:00"))
        if start_hour < 24:
            segments.append((f"{start_hour:02d}:00:00", "24:00:00"))
        return segments

    @classmethod
    def _merged_hour_segments(cls, windows: list) -> list:
        """Daily (begin, end) segments covering the union of several hour windows.

        This exists because the arming schedule gates **the event itself on this
        firmware, not just its linkages** — an hour outside the schedule produces no
        `fielddetection` at all, so the camera classifies nothing and the app hears
        nothing. A night-only schedule therefore makes person/vehicle detection blind
        for the rest of the day, which is why callers pass every window they need the
        camera awake for and get their union.

        Windows are ``(start_hour, end_hour)`` and may wrap midnight. Overlapping or
        touching windows are merged, so 6->18 plus 18->6 collapses to a single
        00:00-24:00 rather than two abutting blocks the camera might not join up.
        """
        # Work in hours-since-midnight, splitting wrapped windows first.
        ranges = []
        for start, end in windows:
            if start == end:
                continue
            if start < end:
                ranges.append((start, end))
            else:
                if end > 0:
                    ranges.append((0, end))
                if start < 24:
                    ranges.append((start, 24))
        if not ranges:
            return []

        merged = []
        for start, end in sorted(ranges):
            # `start <= last_end` (not `<`) so abutting blocks merge into one.
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])

        return [(f"{s:02d}:00:00", f"{e:02d}:00:00") for s, e in merged]

    def _arming_schedule_body(
        self,
        windows: list,
        event: str = "fielddetection",
        index: int = 1,
        channel: int = 1,
    ) -> str:
        """Build the weekly arming schedule; the same windows apply every day.

        ``dayOfWeek`` is 1-7. The camera has no notion of a window that wraps
        midnight, so :meth:`_merged_hour_segments` splits e.g. 18->6 into two blocks
        per day (00:00-06:00 and 18:00-24:00). Like the trigger, this body carries no
        version/xmlns.
        """
        segments = self._merged_hour_segments(windows)
        blocks = "".join(
            f"<TimeBlock><dayOfWeek>{day}</dayOfWeek>"
            f"<TimeRange><beginTime>{begin}</beginTime>"
            f"<endTime>{end}</endTime></TimeRange></TimeBlock>"
            for day in range(1, 8)
            for begin, end in segments
        )
        return (
            f"<Schedule><id>{event}-{index}</id>"
            f"<eventType>{event}</eventType>"
            f"<videoInputChannelID>{channel}</videoInputChannelID>"
            f"<TimeBlockList>{blocks}</TimeBlockList></Schedule>"
        )

    async def set_event_arming_schedule(
        self,
        windows: list,
        event: str = "fielddetection",
        index: int = 1,
        channel: int = 1,
    ) -> bool:
        """
        Write the camera's native arming schedule for a smart event.

        ``windows`` is a list of ``(start_hour, end_hour)`` pairs, merged into the
        union — pass every window the camera must be awake for.

        Outside the schedule this firmware emits **no event at all**, so the schedule
        is not merely a linkage gate: it decides when the camera classifies anything.
        Inside it, the event's linkages (flash / siren / record) fire *on-camera* with
        no doover involvement, so a deterrent scheduled here keeps working while the
        device is offline.

        This runs on the camera's own clock, so it's only as good as that clock: see
        the engine's ``sync_camera_clock``, without which the camera believes it's
        2019 and arms at the wrong hours. Returns whether the camera accepted it;
        callers should fall back to app-driven arming if not.
        """
        collection = self._SCHEDULE_COLLECTIONS.get(event)
        if collection is None:
            _LOGGER.warning(
                f"No arming-schedule collection known for '{event}'; skipping the "
                f"native schedule and leaving arming to the app."
            )
            return False
        endpoint = self._SCHEDULE_ENDPOINT.format(
            collection=collection, event=event, channel=channel
        )
        body = self._arming_schedule_body(windows, event, index, channel)
        try:
            await self.put(endpoint, body=body)
        except Exception as e:
            _LOGGER.warning(f"Arming schedule not accepted at {endpoint}: {e}")
            return False
        return True

    # -- On-camera storage (microSD) --

    # Storage the camera reports but can't record to yet (no card, or a card that
    # needs formatting) — treated as "no storage" so callers fall back.
    _USABLE_STORAGE_STATUS = ("ok", "idle")

    async def get_storage(self) -> list:
        """List the camera's storage volumes (microSD, NAS, ...).

        Returns a flattened dict per volume, with keys like ``status``,
        ``capacity`` (MB), ``freeSpace`` and ``hddType``.
        """
        try:
            raw = await self.get_bytes("/ISAPI/ContentMgmt/Storage")
        except Exception as e:
            _LOGGER.debug(f"Failed to read storage config: {e}")
            return []

        try:
            root = ET.fromstring(raw.decode(errors="ignore"))
        except ET.ParseError:
            return []

        return [
            _xml_to_dict(element)
            for element in root.iter()
            if _strip_ns(element.tag) == "hdd"
        ]

    async def has_recording_storage(self) -> bool:
        """
        Whether the camera has storage it can actually record events to.

        A card that is absent, unformatted or erroring reports a non-usable status
        (or zero capacity), in which case the ``record`` linkage would silently write
        nothing — so callers should fall back to pulling video another way.
        """
        for volume in await self.get_storage():
            status = (volume.get("status") or "").strip().lower()
            try:
                capacity = int(volume.get("capacity") or 0)
            except (TypeError, ValueError):
                capacity = 0

            if capacity > 0 and status in self._USABLE_STORAGE_STATUS:
                _LOGGER.info(
                    f"Camera storage available: type={volume.get('hddType')} "
                    f"status={status} capacity={capacity}MB "
                    f"free={volume.get('freeSpace')}MB"
                )
                return True
        return False

    # -- On-camera (microSD) recordings --

    @staticmethod
    def _isapi_time(value: datetime) -> str:
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    async def search_recordings(
        self,
        start: datetime,
        end: datetime,
        track_id: int = 101,
        max_results: int = 10,
    ) -> list:
        """
        Search the camera's own storage (microSD) for recordings in a time window.

        Returns a list of flattened match dicts, each carrying a
        ``mediaSegmentDescriptor.playbackURI`` that :meth:`download_recording` can
        fetch. ``track_id`` 101 is channel 1's main stream.
        """
        body = (
            f'<CMSearchDescription version="2.0" xmlns="{ISAPI_NS}">'
            f"<searchID>{uuid.uuid4()}</searchID>"
            f"<trackIDList><trackID>{track_id}</trackID></trackIDList>"
            f"<timeSpanList><timeSpan>"
            f"<startTime>{self._isapi_time(start)}</startTime>"
            f"<endTime>{self._isapi_time(end)}</endTime>"
            f"</timeSpan></timeSpanList>"
            f"<maxResults>{max_results}</maxResults>"
            # Hikvision's own schema misspells "Position" — do not "fix" this.
            f"<searchResultPostion>0</searchResultPostion>"
            f"<metadataList><metadataDescriptor>"
            f"//recordType.meta.std-cgi.com"
            f"</metadataDescriptor></metadataList>"
            f"</CMSearchDescription>"
        )
        raw = await self.post_bytes("/ISAPI/ContentMgmt/search", body)
        try:
            root = ET.fromstring(raw.decode(errors="ignore"))
        except ET.ParseError:
            return []

        matches = []
        for element in root.iter():
            if _strip_ns(element.tag) == "searchMatchItem":
                matches.append(_xml_to_dict(element))
        return matches

    async def download_recording(self, playback_uri: str) -> bytes:
        """
        Download a recording segment by its ``playbackURI``.

        You get the whole segment, and there's no trimming it: the URI a search hands
        back carries ``name=``/``size=``, which puts the camera in download-by-filename
        mode, so the ``starttime``/``endtime`` in it are ignored (verified — narrowing
        the range returns a byte-identical file). Dropping ``name``/``size`` to force
        download-by-time just makes the camera hang up. Since the recording is event
        triggered, the segment is roughly the event anyway, plus the camera's pre/post
        roll.

        The bytes are **not** mp4 despite what the URI implies — see
        ``CameraBase.remux_to_mp4``.
        """
        body = (
            f'<downloadRequest version="1.0" xmlns="{ISAPI_NS}">'
            f"<playbackURI>{escape(playback_uri)}</playbackURI>"
            f"</downloadRequest>"
        )
        return await self.post_bytes("/ISAPI/ContentMgmt/download", body)

    # Tags a target's bounding box arrives under. ``TargetRect`` is what the AcuSense /
    # DeepinView perimeter events use (inside <DetectionRegionEntry>, beside
    # <detectionTarget>); the others cover ANPR and the newer DeepinView analytics, whose
    # element names aren't in the public spec. Harmless to look for a tag a camera never
    # sends — an alert with none simply carries no boxes.
    _RECT_TAGS = ("TargetRect", "boundingBox", "Rect")

    @classmethod
    def _target_regions(cls, root: ET.Element) -> list:
        """Every target's bounding box in one alert, in the order it reported them.

        Read off the XML tree rather than the flattened dict on purpose:
        :func:`_xml_to_dict` writes repeated siblings to the same dotted key, so on a
        two-person event the second ``DetectionRegionEntry`` overwrites the first and only
        the last box would survive. Boxes are exactly the thing there can be several of.
        """
        # ElementTree nodes don't know their parent, and a rect's classification is its
        # sibling, so the link has to be built up front.
        parents = {child: parent for parent in root.iter() for child in parent}

        regions = []
        for element in root.iter():
            if _strip_ns(element.tag) not in cls._RECT_TAGS:
                continue
            box = cls._parse_rect(element)
            if box is None:
                continue
            region = {"box": box}
            target = cls._rect_target(element, parents)
            if target:
                region["target"] = target
            regions.append(region)
        return regions

    @staticmethod
    def _rect_target(element: ET.Element, parents: dict, depth: int = 2) -> str:
        """The classification belonging to a rect (``human``, ``vehicle``, ...).

        It sits beside the rect — ``<DetectionRegionEntry>`` holds both — so this checks
        the rect's siblings, then walks up a couple of levels for firmware that nests the
        rect one deeper. Returns ``""`` when the alert didn't classify the target, which
        is normal for an ANPR plate rect.
        """
        node = element
        for _ in range(depth + 1):
            parent = parents.get(node)
            if parent is None:
                break
            for sibling in parent:
                if _strip_ns(sibling.tag).lower().endswith("detectiontarget"):
                    text = (sibling.text or "").strip()
                    if text:
                        # Multiple entries could be comma-joined; take the first.
                        return text.split(",")[0].strip().lower()
            node = parent
        return ""

    @staticmethod
    def _parse_rect(element: ET.Element) -> list:
        """One rect as ``[x1, y1, x2, y2]`` fractions of the frame, or None if unusable.

        Firmware disagrees on units: some report fractions of the frame (0..1), others
        the normalized 0..1000 screen the zone endpoints use. Anything over 1 is
        therefore read as normalized and scaled down, which lands both conventions in the
        0..1 top-left space the rest of the app speaks (see ``events.DetectionZone``).
        A camera reporting a genuine 0..1 rect that happens to touch the far edge is
        unaffected: 1.0 is not > 1.

        NOTE: the y axis is passed through as the camera gave it. Hikvision's *zone*
        coordinates are y-up (hence ``_flip_y`` in the AcuSense engine) but the alert
        rect is documented top-left, and this hasn't been checked against a real capture.
        If boxes come back vertically mirrored, flipping is a one-line change here.
        """
        values = {}
        for child in element:
            try:
                values[_strip_ns(child.tag).lower()] = float((child.text or "").strip())
            except ValueError:
                return None

        try:
            x, y = values["x"], values["y"]
            width, height = values["width"], values["height"]
        except KeyError:
            return None

        if max(x, y, width, height) > 1:
            x, y, width, height = (
                v / NORMALIZED_SCREEN for v in (x, y, width, height)
            )

        return [
            round(min(1.0, max(0.0, v)), 4)
            for v in (x, y, x + width, y + height)
        ]

    async def stream_events(self, callback, heartbeat: int = 5):
        """
        Subscribe to the ISAPI event notification stream (alertStream).

        A long-lived HTTP connection returning ``multipart/mixed`` parts (the boundary is
        the literal string ``boundary``). Each part is either an ``application/xml``
        ``<EventNotificationAlert>`` or an ``image/jpeg`` — **the camera's own frame of the
        event**, captured at the instant it classified the target rather than whenever we
        next get round to asking for a picture. That frame used to be discarded; it is now
        paired with the alert it belongs to and handed over with it (see
        :attr:`EVENT_IMAGE_KEY`), which is the only way to get a picture of something moving
        through frame at the moment it was actually there.

        Pairing is by position — the JPEG is the part after the XML — so an alert that
        advertises a picture is held back until it arrives, bounded by
        :data:`EVENT_IMAGE_WAIT_SECS` and released early if the next alert beats it. The
        alarm path depends on events arriving promptly, so nothing here may wait
        indefinitely on a picture that may not come.
        """
        url = f"{self._base}/ISAPI/Event/notification/alertStream"
        if not (self._username or self._password):
            return

        response = None
        try:
            auth = DigestAuth(self._username, self._password, self._session)
            response = await auth.request("GET", url)
            response.raise_for_status()

            buffer = b""
            # An alert that says a picture is coming, held until it arrives so the event
            # and its frame reach the app together.
            pending: dict | None = None
            chunks = response.content.iter_chunks().__aiter__()

            while True:
                try:
                    # Only ever wait a bounded time when something is pending: an alert
                    # must not sit here because a promised picture never turned up.
                    data, _ = await asyncio.wait_for(
                        chunks.__anext__(),
                        EVENT_IMAGE_WAIT_SECS if pending is not None else None,
                    )
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    _LOGGER.debug("Picture didn't arrive in time; forwarding the event.")
                    await self._dispatch(callback, pending)
                    pending = None
                    continue

                buffer += data
                buffer, pending = await self._drain(callback, buffer, pending)

                if len(buffer) > MAX_STREAM_BUFFER:
                    # Lost sync (a part we can't parse, or a picture bigger than we'll
                    # hold). Start clean rather than grow forever; the next alert is a
                    # fresh start because each is self-delimiting.
                    _LOGGER.warning(
                        f"Discarding {len(buffer)} bytes of unparsed event stream."
                    )
                    buffer = b""
                    if pending is not None:
                        await self._dispatch(callback, pending)
                        pending = None

        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.debug("Event stream disconnected, reconnecting...")
            if response is not None:
                response.close()
            await asyncio.sleep(1)
            return await self.stream_events(callback, heartbeat)
        finally:
            if response is not None:
                response.close()

    async def _drain(self, callback, buffer: bytes, pending: dict | None):
        """Consume every complete part in ``buffer``, dispatching what's finished.

        Returns the leftover buffer and the alert (if any) still waiting for its picture.
        """
        while True:
            if pending is not None:
                # The picture belongs to `pending` only if it arrives before the next
                # alert does — otherwise the camera has moved on and we'd be stapling
                # one event's frame onto another's.
                next_alert = buffer.find(ALERT_START)
                marker = buffer.lower().find(b"content-type: image/")

                if marker != -1 and (next_alert == -1 or marker < next_alert):
                    image, rest = self._read_image_part(buffer, marker)
                    if image is None:
                        return buffer, pending  # still arriving
                    await self._dispatch(callback, pending, image)
                    buffer, pending = rest, None
                    continue

                if next_alert != -1:
                    # The next event beat the picture, so it isn't coming.
                    await self._dispatch(callback, pending)
                    pending = None
                    continue

                return buffer, pending

            end = buffer.find(ALERT_END)
            if end == -1:
                return buffer, pending
            end += len(ALERT_END)
            block, buffer = buffer[:end], buffer[end:]

            alert = self._parse_alert(block)
            if alert is None:
                continue
            if self._alert_has_picture(alert):
                pending = alert
                continue
            await self._dispatch(callback, alert)

    @staticmethod
    def _read_image_part(buffer: bytes, marker: int):
        """The JPEG starting at ``marker``, and what's left. ``(None, None)`` if partial."""
        header_end = buffer.find(b"\r\n\r\n", marker)
        if header_end == -1:
            return None, None
        match = re.search(
            rb"Content-Length:\s*(\d+)", buffer[marker:header_end], re.IGNORECASE
        )
        if match is None:
            return None, None

        body = header_end + 4
        end = body + int(match.group(1))
        if len(buffer) < end:
            return None, None
        return buffer[body:end], buffer[end:]

    @staticmethod
    def _alert_has_picture(alert: dict) -> bool:
        """Whether the camera says it's attaching a JPEG of this event.

        Verified on both firmware lines: a classified smart event carries
        ``detectionPictureTransType=binary`` with ``picturesNumber=1``, and the JPEG follows
        as the next multipart part. Heartbeat (``duration``) and ``videoloss`` alerts don't.
        """
        if (alert.get("detectionPictureTransType") or "").strip().lower() != "binary":
            return False
        for key in ("detectionPicturesNumber", "picturesNumber"):
            try:
                if int(alert.get(key) or 0) > 0:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def _parse_alert(self, data: bytes) -> dict | None:
        """Parse one <EventNotificationAlert> block into a flattened dict."""
        try:
            text = data.decode(errors="ignore")
            start = text.find("<EventNotificationAlert")
            if start == -1:
                return None
            root = ET.fromstring(text[start:])
        except ET.ParseError:
            return None
        except Exception as e:
            _LOGGER.debug(f"Failed to parse event: {e}")
            return None

        event = _xml_to_dict(root)

        # Bounding boxes come off the tree, not the flattened dict, so several
        # targets in one alert all survive (see :meth:`_target_regions`).
        regions = self._target_regions(root)
        if regions:
            event[TARGET_REGIONS_KEY] = regions
        return event

    @staticmethod
    async def _dispatch(callback, alert: dict, image: bytes = None) -> None:
        """Hand an alert (and the camera's own frame of it, if any) to the engine."""
        if alert is None:
            return
        if image:
            alert[EVENT_IMAGE_KEY] = image
        try:
            if asyncio.iscoroutinefunction(callback):
                await callback(alert)
            else:
                callback(alert)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _LOGGER.warning(f"Event callback failed: {e}", exc_info=e)

    # -- HTTP helpers --

    async def get_bytes(self, url: str) -> bytes:
        """GET request returning raw bytes (for snapshots, etc.)."""
        async with async_timeout.timeout(TIMEOUT_SECONDS):
            response = None
            try:
                auth = DigestAuth(self._username, self._password, self._session)
                response = await auth.request("GET", self._base + url)
                response.raise_for_status()
                return await response.read()
            finally:
                if response is not None:
                    response.close()

    async def post_bytes(self, url: str, body: str = None) -> bytes:
        """POST request with an XML body, returning raw bytes.

        Used by the ContentMgmt search/download endpoints, which take an XML request
        body and return either XML (search) or binary mp4 (download).
        """
        async with async_timeout.timeout(TIMEOUT_SECONDS):
            response = None
            try:
                auth = DigestAuth(self._username, self._password, self._session)
                kwargs = {}
                if body:
                    kwargs["data"] = body
                    kwargs["headers"] = {"Content-Type": "application/xml"}
                response = await auth.request("POST", self._base + url, **kwargs)
                response.raise_for_status()
                return await response.read()
            finally:
                if response is not None:
                    response.close()

    async def get(self, url: str) -> dict:
        """GET request, parses XML response into a dict."""
        url = self._base + url
        try:
            async with async_timeout.timeout(TIMEOUT_SECONDS):
                response = None
                try:
                    auth = DigestAuth(self._username, self._password, self._session)
                    response = await auth.request("GET", url)
                    response.raise_for_status()
                    data = await response.text()
                    return self._parse_xml_response(data)
                finally:
                    if response is not None:
                        response.close()
        except asyncio.TimeoutError as exception:
            _LOGGER.warning("TimeoutError fetching information from %s", url)
            raise exception
        except (KeyError, TypeError) as exception:
            _LOGGER.warning("TypeError fetching information from %s", url)
            raise exception
        except (aiohttp.ClientError, socket.gaierror) as exception:
            _LOGGER.debug("ClientError fetching information from %s", url)
            raise exception
        except Exception as exception:
            _LOGGER.warning("Exception fetching information from %s", url)
            raise exception

    async def put(self, url: str, body: str = None) -> dict:
        """PUT request with optional XML body, parses XML response into a dict."""
        url = self._base + url
        try:
            async with async_timeout.timeout(TIMEOUT_SECONDS):
                response = None
                try:
                    auth = DigestAuth(self._username, self._password, self._session)
                    kwargs = {}
                    if body:
                        kwargs["data"] = body
                        kwargs["headers"] = {"Content-Type": "application/xml"}
                    response = await auth.request("PUT", url, **kwargs)
                    response.raise_for_status()
                    data = await response.text()
                    return self._parse_xml_response(data)
                finally:
                    if response is not None:
                        response.close()
        except asyncio.TimeoutError as exception:
            _LOGGER.warning("TimeoutError sending to %s", url)
            raise exception
        except (aiohttp.ClientError, socket.gaierror) as exception:
            _LOGGER.debug("ClientError sending to %s", url)
            raise exception
        except Exception as exception:
            _LOGGER.warning("Exception sending to %s", url)
            raise exception

    @staticmethod
    def _parse_xml_response(data: str) -> dict:
        """
        Parse a Hikvision ISAPI XML response into a flat dictionary.

        Example input:
        <DeviceInfo xmlns="...">
          <deviceName>Camera</deviceName>
          <model>DS-2TD1228-2/QA</model>
        </DeviceInfo>

        Returns: {"deviceName": "Camera", "model": "DS-2TD1228-2/QA"}
        """
        try:
            root = ET.fromstring(data)
            return _xml_to_dict(root)
        except ET.ParseError:
            # Fall back to raw text if not valid XML
            return {"raw": data}
