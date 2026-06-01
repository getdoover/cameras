import logging

from camera_app.engines.base import CameraBase, ensure_ffmpeg

log = logging.getLogger(__name__)


class GenericRTSPCamera(CameraBase):
    async def setup(self):
        # Generic RTSP cameras always go through ffmpeg, so fail fast on the
        # slim image rather than later when the first snapshot is attempted.
        ensure_ffmpeg()
