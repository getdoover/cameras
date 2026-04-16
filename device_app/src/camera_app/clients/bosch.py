"""Bosch ONVIF Client.

Uses onvif-zeep-async to communicate with Bosch IP cameras via ONVIF protocol.
Designed for the Bosch AUTODOME IP starlight 5100i IR (NDP-5533-Z30L) PTZ camera.
"""

import asyncio
import logging

import aiohttp
from onvif import ONVIFCamera

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 20

# ONVIF event topics for Bosch VCA motion detection
MOTION_TOPICS = {
    "tns1:RuleEngine/CellMotionDetector/Motion",
    "tns1:VideoAnalytics",
}


class BoschClient:
    def __init__(self, username: str, password: str, address: str, port: int):
        self.username = username
        self.password = password
        self.address = address
        self.port = port

        self.cam: ONVIFCamera = None
        self.ptz_service = None
        self.media_service = None
        self.events_service = None
        self.profile_token = None

        self._presets_cache: dict[str, str] = None  # {name: token}
        self._subscription = None
        self._event_task: asyncio.Task = None

    async def connect(self):
        self.cam = ONVIFCamera(
            self.address,
            self.port,
            self.username,
            self.password,
        )
        await self.cam.update_xaddrs()

        self.media_service = await self.cam.create_media_service()
        self.ptz_service = await self.cam.create_ptz_service()

        # Get the first media profile token for PTZ operations
        profiles = await self.media_service.GetProfiles()
        if not profiles:
            raise RuntimeError("No media profiles found on camera")
        self.profile_token = profiles[0].token
        log.info(f"Connected to Bosch camera at {self.address}, profile: {self.profile_token}")

    # --- PTZ Methods ---

    async def get_ptz_status(self):
        return await self.ptz_service.GetStatus({"ProfileToken": self.profile_token})

    async def get_ptz_position(self) -> tuple[float, float, float]:
        status = await self.get_ptz_status()
        pos = status.Position
        pan = pos.PanTilt.x if pos.PanTilt else 0.0
        tilt = pos.PanTilt.y if pos.PanTilt else 0.0
        zoom = pos.Zoom.x if pos.Zoom else 0.0
        return (pan, tilt, zoom)

    async def absolute_move(self, pan: float, tilt: float, zoom: float, speed: float = 1.0):
        request = self.ptz_service.create_type("AbsoluteMove")
        request.ProfileToken = self.profile_token
        request.Position = {
            "PanTilt": {"x": pan, "y": tilt},
            "Zoom": {"x": zoom},
        }
        request.Speed = {
            "PanTilt": {"x": speed, "y": speed},
            "Zoom": {"x": speed},
        }
        await self.ptz_service.AbsoluteMove(request)

    async def relative_move(self, pan: float, tilt: float, zoom: float):
        request = self.ptz_service.create_type("RelativeMove")
        request.ProfileToken = self.profile_token
        request.Translation = {
            "PanTilt": {"x": pan, "y": tilt},
            "Zoom": {"x": zoom},
        }
        await self.ptz_service.RelativeMove(request)

    async def continuous_move(self, pan: float, tilt: float, zoom: float):
        request = self.ptz_service.create_type("ContinuousMove")
        request.ProfileToken = self.profile_token
        request.Velocity = {
            "PanTilt": {"x": pan, "y": tilt},
            "Zoom": {"x": zoom},
        }
        await self.ptz_service.ContinuousMove(request)

    async def stop(self, pan_tilt: bool = True, zoom: bool = True):
        request = self.ptz_service.create_type("Stop")
        request.ProfileToken = self.profile_token
        request.PanTilt = pan_tilt
        request.Zoom = zoom
        await self.ptz_service.Stop(request)

    # --- Preset Methods ---

    async def get_presets(self, fetch: bool = True) -> dict[str, str]:
        if not fetch and self._presets_cache is not None:
            return self._presets_cache

        presets = await self.ptz_service.GetPresets({"ProfileToken": self.profile_token})
        self._presets_cache = {}
        for preset in presets:
            name = preset.Name
            if name:
                self._presets_cache[name] = preset.token
        return self._presets_cache

    async def goto_preset(self, name: str):
        presets = await self.get_presets(fetch=False)
        token = presets.get(name)
        if token is None:
            raise ValueError(f"Preset '{name}' not found")

        request = self.ptz_service.create_type("GotoPreset")
        request.ProfileToken = self.profile_token
        request.PresetToken = token
        await self.ptz_service.GotoPreset(request)

    async def create_preset(self, name: str):
        request = self.ptz_service.create_type("SetPreset")
        request.ProfileToken = self.profile_token
        request.PresetName = name
        await self.ptz_service.SetPreset(request)
        # Invalidate cache
        self._presets_cache = None

    async def delete_preset(self, name: str):
        presets = await self.get_presets(fetch=False)
        token = presets.get(name)
        if token is None:
            raise ValueError(f"Preset '{name}' not found")

        request = self.ptz_service.create_type("RemovePreset")
        request.ProfileToken = self.profile_token
        request.PresetToken = token
        await self.ptz_service.RemovePreset(request)
        # Invalidate cache
        self._presets_cache = None

    # --- Snapshot ---

    async def get_snapshot(self) -> bytes:
        resp = await self.media_service.GetSnapshotUri({"ProfileToken": self.profile_token})
        snapshot_uri = resp.Uri

        auth = aiohttp.BasicAuth(self.username, self.password)
        async with aiohttp.ClientSession(auth=auth) as session:
            async with session.get(snapshot_uri, timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)) as response:
                response.raise_for_status()
                return await response.read()

    # --- Status ---

    async def get_status(self) -> bool:
        try:
            sources = await self.media_service.GetVideoSources()
            return len(sources) > 0
        except Exception:
            return False

    # --- Events (VCA Motion Detection) ---

    async def subscribe_events(self, callback):
        try:
            events_service = await self.cam.create_events_service()
            pullpoint = await events_service.CreatePullPointSubscription()
            subscription = self.cam.create_subscription_service("PullPointSubscription")

            while True:
                try:
                    messages = await subscription.PullMessages({
                        "Timeout": "PT5S",
                        "MessageLimit": 10,
                    })
                    for msg in messages.NotificationMessage or []:
                        topic = str(msg.Topic._value_1) if msg.Topic else ""
                        if any(t in topic for t in MOTION_TOPICS):
                            await self._parse_motion_event(msg, callback)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.warning(f"Error polling ONVIF events: {e}")
                    await asyncio.sleep(5)
        except asyncio.CancelledError:
            log.info("ONVIF event subscription cancelled")
        except Exception as e:
            log.error(f"Failed to create ONVIF event subscription: {e}")

    @staticmethod
    async def _parse_motion_event(msg, callback):
        topic = str(msg.Topic._value_1) if msg.Topic else ""
        event_type = "unknown"

        # Try to extract object type from event data
        if hasattr(msg, "Message") and hasattr(msg.Message, "Data"):
            data = msg.Message.Data
            if hasattr(data, "SimpleItem"):
                for item in data.SimpleItem or []:
                    name = getattr(item, "Name", "").lower()
                    value = getattr(item, "Value", "").lower()
                    if "object" in name or "type" in name:
                        if "person" in value or "human" in value:
                            event_type = "person"
                        elif "vehicle" in value or "car" in value:
                            event_type = "vehicle"
                    elif name == "ismotion" and value == "true":
                        event_type = "unknown"

        await callback(event_type, topic)
