import asyncio

from onvif import ONVIFCamera

from .clients.bosch import WSDL_DIR

TIMEOUT_SECS = 20


async def onvif_reboot(address: str, port: int, username: str, password: str) -> str:
    """Send ONVIF SystemReboot. Returns the camera's reply message."""
    cam = ONVIFCamera(
        address, port, username, password, wsdl_dir=WSDL_DIR, adjust_time=True
    )
    try:
        async with asyncio.timeout(TIMEOUT_SECS):
            await cam.update_xaddrs()
            devicemgmt = await cam.create_devicemgmt_service()
            return await devicemgmt.SystemReboot()
    finally:
        await cam.close()
