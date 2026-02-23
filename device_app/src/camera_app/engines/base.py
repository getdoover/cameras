import asyncio
import base64
from datetime import datetime, timedelta
import logging
import uuid
from pathlib import Path

from pydoover.docker.device_agent.models import File
from camera_app.app_config import CameraConfig, Mode

OUTPUT_FILE_DIR = Path("/tmp/camera")
MAX_MESSAGE_SIZE = 125_000


log = logging.getLogger(__name__)


class CameraBase:
    def __init__(self, config: "CameraConfig"):
        self.config = config

        self.ensure_output_dir()

    async def setup(self):
        pass

    async def close(self):
        pass

    @staticmethod
    def get_output_filepath(task_id, snapshot_type):
        return OUTPUT_FILE_DIR / f"{task_id}.{snapshot_type}"

    @staticmethod
    def ensure_output_dir() -> None:
        OUTPUT_FILE_DIR.mkdir(parents=True, exist_ok=True)

    async def on_control_message(self, message_id, data):
        pass

    async def fetch_presets(self) -> list[str]:
        return []

    async def get_snapshot(self) -> list[File]:
        # returns base64 encoded bytes
        mode = self.config.snapshot.mode.value

        if Mode(mode) is Mode.video:
            func = self.get_video_snapshot
        else:
            func = self.get_still_snapshot

        try:
            data = await func(self.config.rtsp_uri)
        except Exception as e:
            log.exception(f"get_snapshot: {str(e)}", exc_info=e)
            return None

        # if data and len(data) > MAX_MESSAGE_SIZE:
        #     log.info(
        #         f"Reducing snapshot length from {self.config.snapshot.secs.value} to {self.config.snapshot.secs.value * 0.7}"
        #     )
        #     self.config.snapshot.secs.value = self.config.snapshot.secs.value * 0.7
        #     # None signifies an error, so use the parent retry handler which will run this a few times
        #     return None

        return [data]

    async def get_still_snapshot(self, rtsp_uri: str) -> File:
        fp = self.get_output_filepath(str(uuid.uuid4()), "jpg")
        cmd = f"ffmpeg -y -rtsp_transport tcp -i {rtsp_uri} -vf 'scale={self.config.snapshot.scale.value}' -frames:v 1 {fp}"
        await self.run_ffmpeg_cmd(cmd)
        return File(
            filename="snapshot.jpg",
            data=fp.read_bytes(),
            size=fp.stat().st_size,
            content_type="image/jpeg",
        )

    async def get_video_snapshot(self, rtsp_uri: str) -> File:
        fp = self.get_output_filepath(str(uuid.uuid4()), "mp4")

        # possible alternative, allegedly h265 is the "new" best high-compression format.
        # ffmpeg -y -rtsp_transport tcp -i rtsp://10.144.239.221:554/s0 -vf
        # scale=420:-1 -r 10 -t 6 -vcodec libx265 -tag:v hvc1 -c:a aac output.mp4
        cmd = (
            f"ffmpeg -y -rtsp_transport tcp -i {rtsp_uri} -vf 'fps={self.config.snapshot.fps.value},scale={self.config.snapshot.scale.value},"
            f"format=yuv420p,pad=ceil(iw/2)*2:ceil(ih/2)*2' -t {self.config.snapshot.secs.value} -c:v libx264 -c:a aac {fp}"
        )
        await self.run_ffmpeg_cmd(cmd)
        return File(
            filename="snapshot.mp4",
            data=fp.read_bytes(),
            size=fp.stat().st_size,
            content_type="video/mp4",
        )

    async def run_ffmpeg_cmd(self, cmd):
        self.ensure_output_dir()
        log.info(f"running cmd: {cmd}")
        proc = await asyncio.create_subprocess_shell(cmd)
        await proc.communicate()

    async def ping(self, timeout: int):
        hostname = self.config.connection.address.value
        start = datetime.now()

        while datetime.now() - start < timedelta(seconds=timeout):
            try:
                process = await asyncio.create_subprocess_exec(
                    *["ping", "-c", "1", "-W", str(timeout), hostname],
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await process.wait()
            except Exception as e:
                log.exception(f"Failed to ping camera: {str(e)}", exc_info=e)
                return False
            else:
                log.info(f"Ping command successful, exit code: {process.returncode}")
                if process.returncode == 0:
                    return True

                await asyncio.sleep(1)
