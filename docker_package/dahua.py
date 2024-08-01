"""Dahua API Client.

Original Source Code: https://github.com/rroller/dahua/blob/main/custom_components/dahua/client.py
Original License: MIT License Copyright (c) 2020 Joakim Sørensen @ludeeus

Modified by Josh Bramley, Doover.
"""
import logging
import socket
import asyncio
from typing import Any

import aiohttp
import async_timeout

from dahua_digest_auth import DigestAuth
from hashlib import md5

_LOGGER: logging.Logger = logging.getLogger(__package__)

TIMEOUT_SECONDS = 20
SECURITY_LIGHT_TYPE = 1
SIREN_TYPE = 2


class DahuaClient:
    """
    DahuaClient is the client for accessing Dahua IP Cameras. The APIs were discovered from the "API of HTTP Protocol Specification" V2.76 2019-07-25 document
    and from inspecting the camera's HTTP UI request/responses.

    events is the list of events used to monitor on the camera (For example, motion detection)
    """

    def __init__(
        self,
        username: str,
        password: str,
        address: str,
        port: int,
        rtsp_port: int,
        session: aiohttp.ClientSession
    ) -> None:
        self._username = username
        self._password = password
        self._address = address
        self._session = session
        self._port = port
        self._rtsp_port = rtsp_port

        protocol = "https" if int(port) == 443 else "http"
        self._base = f"{protocol}://{address}:{port}"

    def get_rtsp_stream_url(self, channel: int, subtype: int) -> str:
        """
        Returns the RTSP url for the supplied subtype (subtype is 0=Main stream, 1=Sub stream)
        """
        return f"rtsp://{self._username}:{self._password}@{self._address}:{self._rtsp_port}/cam/realmonitor?channel={channel}&subtype={subtype}"

    async def get_snapshot(self, channel_number: int) -> bytes:
        """
        Takes a snapshot of the camera and returns the binary jpeg data
        NOTE: channel_number is not the channel_index. channel_number is the index + 1
        so channel index 0 is channel number 1. Except for some older firmwares where channel
        and channel number are the same!
        """
        return await self.get_bytes(f"/cgi-bin/snapshot.cgi?channel={channel_number}")

    async def get_system_info(self) -> dict:
        """
        Get system info data from the getSystemInfo API. Example response:

        appAutoStart=true
        deviceType=IPC-HDW5831R-ZE
        hardwareVersion=1.00
        processor=S3LM
        serialNumber=4X7C5A1ZAG21L3F
        updateSerial=IPC-HDW5830R-Z
        updateSerialCloudUpgrade=IPC-HDW5830R-Z:07:01:08:70:52:00:09:0E:03:00:04:8F0:00:00:00:00:00:02:00:00:600
        """
        try:
            return await self.get("/cgi-bin/magicBox.cgi?action=getSystemInfo")
        except aiohttp.ClientResponseError as e:
            not_hashed_id = f"{self._address}_{self._rtsp_port}_{self._username}_{self._password}"
            unique_cam_id = md5(not_hashed_id.encode('UTF-8')).hexdigest()
            return {"serialNumber": unique_cam_id}

    async def get_device_type(self) -> dict:
        """
        getDeviceType returns the device type. Example response:
        type=IPC-HDW5831R-ZE
        ...
        Some cams might return...
        type=IP Camera
        """
        try:
            return await self.get("/cgi-bin/magicBox.cgi?action=getDeviceType")
        except aiohttp.ClientResponseError as e:
            return {"type": "Generic RTSP"}

    async def get_software_version(self) -> dict:
        """
        get_software_version returns the device software version (also known as the firmware version). Example response:
        version=2.800.0000016.0.R,build:2020-06-05
        """
        try:
            return await self.get("/cgi-bin/magicBox.cgi?action=getSoftwareVersion")
        except aiohttp.ClientResponseError as e:
            return {"version": "1.0"}

    async def get_machine_name(self) -> dict:
        """ get_machine_name returns the device name. Example response: name=FrontDoorCam """
        try:
            return await self.get("/cgi-bin/magicBox.cgi?action=getMachineName")
        except aiohttp.ClientResponseError as e:
            not_hashed_id = "{0}_{1}_{2}_{3}".format(self._address, self._rtsp_port, self._username, self._password)
            unique_cam_id = md5(not_hashed_id.encode('UTF-8')).hexdigest()
            return {"name": unique_cam_id}

    async def get_vendor(self) -> dict:
        """ get_vendor returns the vendor. Example response: vendor=Dahua """
        try:
            return await self.get("/cgi-bin/magicBox.cgi?action=getVendor")
        except aiohttp.ClientResponseError as e:
            return {"vendor": "Generic RTSP"}

    async def reboot(self) -> dict:
        """ Reboots the device """
        return await self.get("/cgi-bin/magicBox.cgi?action=reboot")

    async def get_max_extra_streams(self) -> int:
        """ get_max_extra_streams returns the max number of sub streams supported by the camera """
        try:
            result = await self.get("/cgi-bin/magicBox.cgi?action=getProductDefinition&name=MaxExtraStream")
            return int(result.get("table.MaxExtraStream", "2"))
        except aiohttp.ClientResponseError as e:
            # If we can't fetch, just assume 2 since that's pretty standard
            return 3

    async def get_coaxial_control_io_status(self) -> dict:
        """
        async_get_coaxial_control_io_status returns the the current state of the speaker and white light.
        Note that the "white light" here seems to also work for cameras that have the red/blue flashing alarm light
        like the IPC-HDW3849HP-AS-PV.

        Example response:

        status.status.Speaker=Off
        status.status.WhiteLight=Off
        """
        # fixme: what is this return type?
        return await self.get("/cgi-bin/coaxialControlIO.cgi?action=getStatus&channel=1")

    async def get_lighting_v2(self) -> dict:
        """
        async_get_lighting_v2 will fetch the status of the camera light (also known as the illuminator)
        NOTE: this is not the same as the infrared (IR) light. This is the white visible light on the camera
        Not all cameras have this feature.

        Example response:
        table.Lighting_V2[0][2][0].Correction=50
        table.Lighting_V2[0][2][0].LightType=WhiteLight
        table.Lighting_V2[0][2][0].MiddleLight[0].Angle=50
        table.Lighting_V2[0][2][0].MiddleLight[0].Light=100
        table.Lighting_V2[0][2][0].Mode=Manual
        table.Lighting_V2[0][2][0].PercentOfMaxBrightness=100
        table.Lighting_V2[0][2][0].Sensitive=3
        """
        # fixme: return a namedtuple
        return await self.get("/cgi-bin/configManager.cgi?action=getConfig&name=Lighting_V2")

    async def get_config(self, name) -> dict:
        """ async_get_config gets a config by name """
        # example name=Lighting[0][0]
        try:
            return await self.get(f"/cgi-bin/configManager.cgi?action=getConfig&name={name}")
        except aiohttp.ClientResponseError as e:
            return {}

    async def set_config(self, name, value):
        """ set_config sets a config by name """
        # example name=Lighting[0][0]
        return await self.get(f"/cgi-bin/configManager.cgi?action=setConfig&{name}={value}", verify_ok=True)

    async def batch_set_config(self, *items: tuple[str, str]):
        return await self.get(
            "/cgi-bin/configManager.cgi?action=setConfig&" + "&".join([f"{name}={value}" for name, value in items]),
            verify_ok=True
        )

    async def get_config_lighting(self, channel: int, profile_mode) -> dict[str, Any]:
        """
        async_get_config_lighting will fetch the status of the IR light (InfraRed light)
        profile_mode: = 0=day, 1=night, 2=normal scene

        Example response:
        table.Lighting[0][0].Correction=50
        table.Lighting[0][0].MiddleLight[0].Angle=50
        table.Lighting[0][0].MiddleLight[0].Light=50
        table.Lighting[0][0].Mode=Auto
        table.Lighting[0][0].Sensitive=3
        """
        try:
            return await self.get_config(f"Lighting[{channel}][{profile_mode}]")
        except aiohttp.ClientResponseError as e:
            if e.status == 400:
                # Some cams/dvrs/nvrs might not support this option.
                # We'll just return an empty response to not break the integration.
                return {}
            raise e

    async def get_config_motion_detection(self) -> dict:
        """
        async_get_config_motion_detection will fetch the motion detection status (enabled or not)
        Example response:
        table.MotionDetect[0].DetectVersion=V3.0
        table.MotionDetect[0].Enable=true
        """
        try:
            return await self.get_config("MotionDetect")
        except aiohttp.ClientResponseError as e:
            return {"table.MotionDetect[0].Enable": "false"}

    async def get_video_analyse_rules_for_amcrest(self):
        """
        returns the VideoAnalyseRule and if they are enabled or not.
        Example output:
          table.VideoAnalyseRule[0][0].Enable=false
        """
        try:
            return await self.get_config("VideoAnalyseRule[0][0].Enable")
        except aiohttp.ClientResponseError as e:
            return {"table.VideoAnalyseRule[0][0].Enable": "false"}

    async def get_ivs_rules(self):
        """
        returns the IVS rules and if they are enabled or not. [0][1] means channel 0, rule 1
        table.VideoAnalyseRule[0][1].Enable=true
        table.VideoAnalyseRule[0][1].Name=IVS-1
        """
        return await self.get_config("VideoAnalyseRule")

    async def set_all_ivs_rules(self, channel: int, enabled: bool):
        """
        Sets all IVS rules to enabled or disabled
        """
        rules = await self.get_ivs_rules()
        # Supporting up to a max of 11 rules. Just because 11 seems like a high enough number
        to_modify = []
        for index in range(10):
            rule = f"table.VideoAnalyseRule[{channel}][{index}].Enable"
            if rule in rules:
                to_modify.append((rule.replace("table.", ""), str(enabled).lower()))

        if to_modify:
            return await self.batch_set_config(*to_modify)

    async def set_ivs_rule(self, channel: int, index: int, enabled: bool):
        """ Sets and IVS rules to enabled or disabled. This also works for Amcrest smart motion detection"""
        return await self.set_config(f"VideoAnalyseRule[{channel}][{index}].Enable", str(enabled).lower())

    async def set_smart_motion_detection(self, enabled: bool):
        """ Enables or disabled smart motion detection for Dahua devices (doesn't work for Amcrest)"""
        return await self.set_config("SmartMotionDetect[0].Enable", str(enabled).lower())

    async def set_light_global_enabled(self, enabled: bool):
        """ Turns the blue ring light on/off for Amcrest doorbells """
        return await self.set_config("LightGlobal[0].Enable", str(enabled).lower())

    async def get_smart_motion_detection(self) -> dict:
        """
        Gets the status of smart motion detection. Example output:
        table.SmartMotionDetect[0].Enable=true
        table.SmartMotionDetect[0].ObjectTypes.Human=true
        table.SmartMotionDetect[0].ObjectTypes.Vehicle=false
        table.SmartMotionDetect[0].Sensitivity=Middle
        """
        return await self.get_config("SmartMotionDetect")

    async def get_light_global_enabled(self) -> dict:
        """
        Returns the state of the Amcrest blue ring light (if it's on or off)
        Example output:
        table.LightGlobal[0].Enable=true
        """
        return await self.get_config("LightGlobal[0].Enable")

    async def get_floodlightmode(self) -> dict:
        """ async_get_config_floodlightmode gets floodlight mode """
        try:
            return await self.get_config("FloodLightMode.Mode")
        except aiohttp.ClientResponseError as e:
            return 2

    async def set_floodlightmode(self, mode: int) -> dict:
        """ async_set_floodlightmode will set the floodlight lighting control  """
        # 1 - Motion Activation
        # 2 - Manual (for manual switching)
        # 3 - Schedule
        # 4 - PIR
        return await self.set_config("FloodLightMode.Mode", mode)

    async def set_lighting_v1(self, channel: int, enabled: bool, brightness: int) -> dict:
        """ async_get_lighting_v1 will turn the IR light (InfraRed light) on or off """
        # on = Manual, off = Off
        mode = "Manual" if enabled else "Off"
        return await self.set_lighting_v1_mode(channel, mode, brightness)

    async def set_lighting_v1_mode(self, channel: int, mode: str, brightness: int) -> dict:
        """
        async_set_lighting_v1_mode will set IR light (InfraRed light) mode and brightness
        Mode should be one of: Manual, Off, or Auto
        Brightness should be between 0 and 100 inclusive. 100 being the brightest
        """

        if mode.lower() == "on":
            mode = "Manual"
        # Dahua api expects the first char to be capital
        mode = mode.capitalize()

        items = [
            (f"Lighting[{channel}][0].Mode", mode),
            (f"Lighting[{channel}][0].MiddleLight[0].Light", str(brightness)),
        ]
        return await self.batch_set_config(*items)

    async def set_video_profile_mode(self, channel: int, mode: str):
        """
        async_set_video_profile_mode will set camera's profile mode to day or night
        Mode should be one of: Day or Night
        """
        mode = "1" if mode.lower() == "night" else "0"

        return await self.set_config(f"VideoInMode[{channel}].Config[0]", mode)

    async def adjust_focus_v1(self, focus: str, zoom: str):
        """
        async_adjustfocus will set the zoom and focus
        """
        return await self.get(f"/cgi-bin/devVideoInput.cgi?action=adjustFocus&focus={focus}&zoom={zoom}", True)

    async def adjust_ptz(self, action: str, channel: int, speed: int):
        self.prev_ptz_action = (action, channel, speed)
        return await self.get(f"/cgi-bin/ptz.cgi?action=start&channel={channel}&code={action}&arg1=0&arg2={speed}&arg3=0", True)

    async def stop_ptz(self):
        try:
            action, channel, speed = self.prev_ptz_action
        except AttributeError:
            return

        return await self.get(f"/cgi-bin/ptz.cgi?action=stop&channel={channel}&code={action}&arg1=0&arg2={speed}&arg3=0", True)

    async def set_privacy_mask(self, index: int, enabled: bool):
        """
        async_setprivacymask will enable or disable the privacy mask
        """
        return await self.set_config(f"PrivacyMasking[0][{index}].Enable", str(enabled).lower())

    async def set_night_switch_mode(self, channel: int, mode: str):
        """
        async_set_night_switch_mode is the same as async_set_video_profile_mode when accessing the camera
        through a lorex NVR
        Mode should be one of: Day or Night
        """
        mode = "3" if mode.lower() == "night" else "0"

        return await self.set_config(f"VideoInOptions[{channel}].NightOptions.SwitchMode", mode)

    async def enable_channel_title(self, channel: int, enabled: bool):
        """ async_set_enable_channel_title will enable or disables the camera's channel title overlay """
        return await self.set_config(f"VideoWidget[{channel}].ChannelTitle.EncodeBlend", enabled)

    async def enable_time_overlay(self, channel: int, enabled: bool):
        """ async_set_enable_time_overlay will enable or disables the camera's time overlay """
        return await self.set_config(f"VideoWidget[{channel}].TimeTitle.EncodeBlend", enabled)

    async def enable_text_overlay(self, channel: int, group: int, enabled: bool):
        """ async_set_enable_text_overlay will enable or disables the camera's text overlay """
        return await self.set_config(f"VideoWidget[{channel}].CustomTitle[{group}].EncodeBlend", enabled)

    async def enable_custom_overlay(self, channel: int, group: int, enabled: bool):
        """ async_set_enable_custom_overlay will enable or disables the camera's custom overlay """
        return await self.set_config(f"VideoWidget[{channel}].UserDefinedTitle[{group}].EncodeBlend", enabled)

    async def set_service_set_channel_title(self, channel: int, text1: str, text2: str):
        """ async_set_service_set_channel_title sets the channel title """
        text = '|'.join(filter(None, [text1, text2]))
        return await self.set_config(f"ChannelTitle[{channel}].Name", text)

    async def set_service_set_text_overlay(self, channel: int, group: int, *text_parameters: str):
        """ async_set_service_set_text_overlay sets the video text overlay """
        text = '|'.join(text_parameters)
        return await self.set_config(f"VideoWidget[{channel}].CustomTitle[{group}].Text", text)

    async def set_service_set_custom_overlay(self, channel: int, group: int, *text_parameters: str):
        """ async_set_service_set_custom_overlay sets the customer overlay on the video"""
        text = "|".join(text_parameters)
        return await self.set_config(f"VideoWidget[{channel}].UserDefinedTitle[{group}].Text", text)

    async def set_lighting_v2(self, channel: int, enabled: bool, brightness: int, profile_mode: str) -> dict:
        """
        async_set_lighting_v2 will turn on or off the white light on the camera. If turning on, the brightness will be used.
        brightness is in the range of 0 to 100 inclusive where 100 is the brightest.
        NOTE: this is not the same as the infrared (IR) light. This is the white visible light on the camera

        profile_mode: 0=day, 1=night, 2=scene
        """

        # on = Manual, off = Off
        mode = "Manual"
        if not enabled:
            mode = "Off"
        url = "/cgi-bin/configManager.cgi?action=setConfig&Lighting_V2[{channel}][{profile_mode}][0].Mode={mode}&Lighting_V2[{channel}][{profile_mode}][0].MiddleLight[0].Light={brightness}".format(
            channel=channel, profile_mode=profile_mode, mode=mode, brightness=brightness
        )
        _LOGGER.debug("Turning light on: %s", url)
        return await self.get(url)

    # async def async_set_lighting_v2_for_flood_lights(self, channel: int, enabled: bool, brightness: int, profile_mode: str) -> dict:
    # async def async_set_lighting_v2_for_flood_lights(self, channel: int, enabled: bool, profile_mode: str) -> dict:
    #     """
    #     async_set_lighting_v2_for_floodlights will turn on or off the flood light on the camera. If turning on, the brightness will be used.
    #     brightness is in the range of 0 to 100 inclusive where 100 is the brightest.
    #     NOTE: While the flood lights do support an auto or "smart" mode, the api does not handle this change properly.
    #           If one wishes to make the change back to auto, it must be done in the 'Amcrest Smart Home' smartphone app.
    #
    #     profile_mode: 0=day, 1=night, 2=scene
    #     """
    #
    #     # on = Manual, off = Off
    #     mode = "Manual"
    #     if not enabled:
    #         mode = "Off"
    #     url_base = "/cgi-bin/configManager.cgi?action=setConfig"
    #     mode_cmnd = f'Lighting_V2[{channel}][{profile_mode}][1].Mode={mode}'
    #     # brightness_cmnd = f'Lighting_V2[{channel}][{profile_mode}][1].MiddleLight[0].Light={brightness}'
    #     # url = f'{url_base}&{mode_cmnd}&{brightness_cmnd}'
    #     url = f'{url_base}&{mode_cmnd}'
    #     _LOGGER.debug("Switching light: %s", url)
    #     return await self.get(url)
    #
    # async def async_set_lighting_v2_for_amcrest_doorbells(self, mode: str) -> dict:
    #     """
    #     async_set_lighting_v2_for_amcrest_doorbells will turn on or off the white light on Amcrest doorbells
    #     mode: On, Off, Flicker
    #     """
    #     mode = mode.lower()
    #     cmd = "Off"
    #     if mode == "on":
    #         cmd = "ForceOn&Lighting_V2[0][0][1].State=On"
    #     elif mode in ('strobe', 'flicker'):
    #         cmd = "ForceOn&Lighting_V2[0][0][1].State=Flicker"
    #
    #     url = "/cgi-bin/configManager.cgi?action=setConfig&Lighting_V2[0][0][1].Mode={cmd}".format(cmd=cmd)
    #     _LOGGER.debug("Turning doorbell light on: %s", url)
    #     return await self.get(url)
    #
    # async def async_set_video_in_day_night_mode(self, channel: int, config_type: str, mode: str):
    #     """
    #     async_set_video_in_day_night_mode will set the video dan/night config. For example to see it to Color or Black
    #     and white.
    #
    #     config_type is one of  "general", "day", or "night"
    #     mode is one of: "Color", "Brightness", or "BlackWhite". Note Brightness is also known as "Auto"
    #     """
    #
    #     # Map the input to the Dahua required integer: 0=day, 1=night, 2=general
    #     if config_type == "day":
    #         config_no = 0
    #     elif config_type == "night":
    #         config_no = 1
    #     else:
    #         # general
    #         config_no = 2
    #
    #     # Map the mode
    #     if mode is None or mode.lower() == "auto" or mode.lower() == "brightness":
    #         mode = "Brightness"
    #     elif mode.lower() == "color":
    #         mode = "Color"
    #     elif mode.lower() == "blackwhite":
    #         mode = "BlackWhite"
    #
    #     url = "/cgi-bin/configManager.cgi?action=setConfig&VideoInDayNight[{0}][{1}].Mode={2}".format(
    #         channel, str(config_no), mode
    #     )
    #     value = await self.get(url)
    #     if "OK" not in value and "ok" not in value:
    #         raise Exception("Could not set Day/Night mode")

    # async def async_get_video_in_mode(self) -> dict:
    #     """
    #     async_get_video_in_mode will return the profile mode (day/night)
    #     0 means config for day,
    #     1 means config for night, and
    #     2 means config for normal scene.
    #
    #     table.VideoInMode[0].Config[0]=2
    #     table.VideoInMode[0].Mode=0
    #     table.VideoInMode[0].TimeSection[0][0]=0 00:00:00-24:00:00
    #     """
    #
    #     url = "/cgi-bin/configManager.cgi?action=getConfig&name=VideoInMode"
    #     return await self.get(url)
    #
    # async def async_set_coaxial_control_state(self, channel: int, dahua_type: int, enabled: bool) -> dict:
    #     """
    #     async_set_lighting_v2 will turn on or off the white light on the camera.
    #
    #     Type=1 -> white light on the camera. this is not the same as the infrared (IR) light. This is the white visible light on the camera
    #     Type=2 -> siren. The siren will trigger for 10 seconds or so and then turn off. I don't know how to get the siren to play forever
    #     NOTE: this is not the same as the infrared (IR) light. This is the white visible light on the camera
    #     """
    #
    #     # on = 1, off = 0
    #     io = "1"
    #     if not enabled:
    #         io = "2"
    #
    #     url = "/cgi-bin/coaxialControlIO.cgi?action=control&channel={channel}&info[0].Type={dahua_type}&info[0].IO={io}".format(
    #         channel=channel, dahua_type=dahua_type, io=io)
    #     _LOGGER.debug("Setting coaxial control state to %s: %s", io, url)
    #     return await self.get(url)
    #
    # async def async_set_disarming_linkage(self, channel: int, enabled: bool) -> dict:
    #     """
    #     async_set_disarming_linkage will set the camera's disarming linkage (Event -> Disarming in the UI)
    #     """
    #
    #     value = "false"
    #     if enabled:
    #         value = "true"
    #
    #     url = "/cgi-bin/configManager.cgi?action=setConfig&DisableLinkage[{0}].Enable={1}".format(channel, value)
    #     return await self.get(url)

    async def set_record_mode(self, channel: int, mode: str) -> dict:
        """
        async_set_record_mode sets the record mode.
        mode should be one of: auto, manual, or off
        """
        if mode.lower() == "auto":
            mode = "0"
        elif mode.lower() == "manual" or mode.lower() == "on":
            mode = "1"
        elif mode.lower() == "off":
            mode = "2"
        return await self.set_config(f"RecordMode[{channel}].Mode", mode)

    # async def async_get_disarming_linkage(self) -> dict:
    #     """
    #     async_get_disarming_linkage will return true if the disarming linkage (Event -> Disarming in the UI) is enabled
    #
    #     returns
    #     table.DisableLinkage.Enable=false
    #     """
    #
    #     url = "/cgi-bin/configManager.cgi?action=getConfig&name=DisableLinkage"
    #     try:
    #         return await self.get(url)
    #     except aiohttp.ClientResponseError as e:
    #         return {"table.DisableLinkage.Enable": "false"}
    #
    # async def async_access_control_open_door(self, door_id: int = 1) -> dict:
    #     """
    #     async_access_control_open_door opens a door via a VTO
    #     """
    #     url = "/cgi-bin/accessControl.cgi?action=openDoor&UserID=101&Type=Remote&channel={0}".format(door_id)
    #     return await self.get(url)

    async def enable_smart_motion_detection(self, channel: int = 0, human: bool = True, vehicle: bool = True, sensitivity: str = "Middle"):
        items = [
            (f"SmartMotionDetect[{channel}].Enable", "true" if human or vehicle else "false"),
            (f"SmartMotionDetect[{channel}].Sensitivity", sensitivity),
            (f"SmartMotionDetect[{channel}].ObjectTypes.Human", str(human).lower()),
            (f"SmartMotionDetect[{channel}].ObjectTypes.Vehicle", str(vehicle).lower())
        ]
        if not (human or vehicle):
            await self.enable_motion_detection(channel, False)

        return await self.batch_set_config(*items)

    async def enable_motion_detection(self, channel: int, enabled: bool) -> dict:
        """
        enable_motion_detection will either enable/disable motion detection on the camera depending on the value
        """
        items = [
            (f"MotionDetect[{channel}].Enable", str(enabled).lower()),
            # (f"MotionDetect[{channel}].DetectVersion", "V3.0")
        ]
        try:
            return await self.batch_set_config(*items)
        except Exception as e:
            print(e)
            # Some older cameras do not support the above API, so try this one
            return await self.set_config(*items[0])

    async def invoke(self, callback, *args, **kwargs):
        if asyncio.iscoroutinefunction(callback):
            await asyncio.create_task(callback(*args, **kwargs))
        else:
            callback(*args, **kwargs)  # fixme: should this be in a different loop?

    async def stream_snapshots(self, callback, events: list, channel: int, heartbeat: int = 5):
        # http://192.168.1.108/cgi-bin/snapManager.cgi?action=attachFileProc&channel=1&heartbeat=5&Flags[0]
        # =Event&Events=[VideoMotion%2CVideoLoss]

        # Use codes=[All] for all codes
        codes = ",".join(events)
        url = f"{self._base}/cgi-bin/snapManager.cgi?action=attachFileProc&channel={channel}&heartbeat={heartbeat}&Flags[0]=Event&Events=[{codes}]"
        if not (self._username or self._password):
            return

        response = None
        try:
            auth = DigestAuth(self._username, self._password, self._session)
            response = await auth.request("GET", url)
            response.raise_for_status()
            # https://docs.aiohttp.org/en/stable/streams.html
            buffer = b""
            async for data, _ in response.content.iter_chunks():
                if b"--myboundary" in data:
                    split = data.split(b"--myboundary")
                    buffer += split[0].strip()
                    await self.invoke(callback, buffer, channel)

                    if len(split) > 2:
                        for elem in split[1:-1]:
                            await self.invoke(callback, elem, channel)

                    buffer = split[-1]

                else:
                    buffer += data

        except Exception:
            pass
        finally:
            if response is not None:
                response.close()

        # fixme: add some conditional restarting...
        return await self.stream_events(callback, events, channel, heartbeat)


    async def stream_events(self, callback, events: list, channel: int, heartbeat: int = 5):
        """
        enable_motion_detection will either enable or disable motion detection on the camera depending on the supplied value

        All: Use the literal word "All" to get back all events.. or pick and choose from the ones below
        VideoMotion: motion detection event
        VideoMotionInfo: fires when there's motion. Not really sure what it is for
        NewFile:
        SmartMotionHuman: human smart motion detection
        SmartMotionVehicle：Vehicle smart motion detection
        IntelliFrame: I don't know what this is
        VideoLoss: video loss detection event
        VideoBlind: video blind detection event.
        AlarmLocal: alarm detection event.
        CrossLineDetection: tripwire event
        CrossRegionDetection: intrusion event
        LeftDetection: abandoned object detection
        TakenAwayDetection: missing object detection
        VideoAbnormalDetection: scene change event
        FaceDetection: face detect event
        AudioMutation: intensity change
        AudioAnomaly: input abnormal
        VideoUnFocus: defocus detect event
        WanderDetection: loitering detection event
        RioterDetection: People Gathering event
        ParkingDetection: parking detection event
        MoveDetection: fast moving event
        StorageNotExist: storage not exist event.
        StorageFailure: storage failure event.
        StorageLowSpace: storage low space event.
        AlarmOutput: alarm output event.
        InterVideoAccess: I don't know what this is
        NTPAdjustTime: NTP time updates?
        TimeChange: Some event for time changes, related to NTPAdjustTime
        MDResult: motion detection data reporting event. The motion detect window contains 18 rows and 22 columns. The event info contains motion detect data with mask of every row.
        HeatImagingTemper: temperature alarm event
        CrowdDetection: crowd density overrun event
        FireWarning: fire warning event
        FireWarningInfo: fire warning specific data info

        In the example, you can see most event info is like "Code=eventcode; action=Start;
        index=0", but for some specific events, they will contain an another parameter named
        "data", the event info is like "Code=eventcode; action=Start; index=0; data=datainfo",
        the datainfo's fomat is JSON(JavaScript Object Notation). The detail information about
        the specific events and datainfo are listed in the appendix below this table.

        Heartbeat: integer, range is [1,60],unit is second.If the URL contains this parameter,
        and the value is 5, it means every 5 seconds the device should send the heartbeat
        message to the client,the heartbeat message are "Heartbeat".
        Note: Heartbeat message must be sent before heartbeat timeout
        """
        # Use codes=[All] for all codes
        codes = ",".join(events)
        url = f"{self._base}/cgi-bin/eventManager.cgi?action=attach&codes=[{codes}]&heartbeat={heartbeat}"
        if not (self._username or self._password):
            return

        response = None
        try:
            auth = DigestAuth(self._username, self._password, self._session)
            response = await auth.request("GET", url)
            response.raise_for_status()

            # https://docs.aiohttp.org/en/stable/streams.html
            async for data, _ in response.content.iter_chunks():
                if asyncio.iscoroutinefunction(callback):
                    await asyncio.create_task(callback(data, channel))
                else:
                    callback(data, channel)  # fixme: should this be in a different loop?

        except Exception:
            pass
        finally:
            if response is not None:
                response.close()

        # fixme: add some conditional restarting...
        return await self.stream_events(callback, events, channel, heartbeat)

    @staticmethod
    async def parse_dahua_api_response(data: str) -> dict:
        """
        Dahua APIs return back text that looks like this:

        key1=value1
        key2=value2

        We'll convert that to a dictionary like {"key1":"value1", "key2":"value2"}
        """
        lines = data.splitlines()
        data_dict = {}
        for line in lines:
            parts = line.split("=", 1)
            if len(parts) == 2:
                data_dict[parts[0]] = parts[1]
            else:
                # We didn't get a key=value. We just got a key. Just stick it in the dictionary and move on
                data_dict[parts[0]] = line
        return data_dict

    async def get_bytes(self, url: str) -> bytes:
        """Get information from the API. This will return the raw response and not process it"""
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

    async def get(self, url: str, verify_ok=False) -> dict:
        """Get information from the API."""
        url = self._base + url
        try:
            async with async_timeout.timeout(TIMEOUT_SECONDS):
                response = None
                try:
                    auth = DigestAuth(self._username, self._password, self._session)
                    response = await auth.request("GET", url)
                    response.raise_for_status()
                    data = await response.text()
                    if verify_ok:
                        if data.lower().strip() != "ok":
                            raise Exception(data)
                    return await self.parse_dahua_api_response(data)
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
        except Exception as exception:  # pylint: disable=broad-except
            _LOGGER.warning("Exception fetching information from %s", url)
            raise exception

    @staticmethod
    def to_stream_name(subtype: int) -> str:
        """ Given the subtype (aka, stream index), returns the stream name (Main or Sub) """
        if subtype == 0:
            return "Main"
        elif subtype == 1:
            # We originally didn't support more than 1 sub-stream and it we just called it "Sub". To keep backwards
            # compatibility we'll keep the name "Sub" for the first sub-stream. Others will follow the pattern below
            return "Sub"
        else:
            return "Sub_{0}".format(subtype)