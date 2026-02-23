"""Hikvision ISAPI Client.

HTTP client for Hikvision IP cameras using the ISAPI (Intelligent Security API).
Hikvision uses digest authentication and returns XML responses.

Written by Josh Bramley, Doover.
"""

import logging
import socket
import asyncio
import xml.etree.ElementTree as ET

from typing import Any

import aiohttp
import async_timeout

from .dahua import DigestAuth

_LOGGER: logging.Logger = logging.getLogger(__package__)

TIMEOUT_SECONDS = 20

# Hikvision XML namespace
ISAPI_NS = "http://www.hikvision.com/ver20/XMLSchema"
NS = {"hik": ISAPI_NS}


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

    async def get_snapshot(self, channel: int = 1) -> bytes:
        """
        Takes a snapshot from the camera and returns binary JPEG data.
        Channel 101 = ch1 main stream, 102 = ch1 sub stream, etc.
        """
        stream_id = channel * 100 + 1
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

    async def stream_events(self, callback, heartbeat: int = 5):
        """
        Subscribe to the ISAPI event notification stream (alertStream).
        This is a long-lived HTTP connection that returns multipart XML events.
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
            async for data, _ in response.content.iter_chunks():
                if b"--boundary" in data:
                    parts = data.split(b"--boundary")
                    buffer += parts[0]
                    if buffer.strip():
                        await self._process_event(callback, buffer)

                    for part in parts[1:-1]:
                        if part.strip():
                            await self._process_event(callback, part)

                    buffer = parts[-1]
                else:
                    buffer += data

        except asyncio.CancelledError:
            pass
        except Exception:
            _LOGGER.debug("Event stream disconnected, reconnecting...")
            await asyncio.sleep(1)
            return await self.stream_events(callback, heartbeat)
        finally:
            if response is not None:
                response.close()

    async def _process_event(self, callback, data: bytes):
        """Parse and forward an event from the alert stream."""
        try:
            # Find the XML portion of the multipart chunk
            text = data.decode(errors="ignore")
            xml_start = text.find("<")
            if xml_start == -1:
                return
            xml_text = text[xml_start:]
            root = ET.fromstring(xml_text)
            event = _xml_to_dict(root)

            if asyncio.iscoroutinefunction(callback):
                await callback(event)
            else:
                callback(event)
        except ET.ParseError:
            pass
        except Exception as e:
            _LOGGER.debug(f"Failed to process event: {e}")

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
