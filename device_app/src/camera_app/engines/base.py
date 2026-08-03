import asyncio
import base64
import re
import shutil
import signal
from datetime import datetime, timedelta
import logging
import uuid
from pathlib import Path

from pydoover.models import File
from camera_app.app_config import CameraConfig, Mode

OUTPUT_FILE_DIR = Path("/tmp/camera")
MAX_MESSAGE_SIZE = 125_000

# Preview image uploaded alongside each snapshot/video for gallery + timeline use.
# A capture's thumbnail sits beside its media, e.g. Preset1.jpg / Preset1-thumbnail.jpg.
THUMBNAIL_FILENAME = "thumbnail.jpg"
THUMBNAIL_SUFFIX = "-thumbnail"
THUMBNAIL_WIDTH = 640


class Capture:
    """One captured view: the media, and the thumbnail that belongs to it.

    They're produced together rather than thumbnailed later on purpose — a PTZ
    camera takes one of these per preset, and by the time the batch is uploaded it
    has moved on, so a thumbnail grabbed afterwards would show the wrong scene.
    """

    def __init__(self, name: str, media: File, thumbnail: File = None):
        self.name = name
        self.media = media
        self.thumbnail = thumbnail

    def files(self) -> list:
        return [f for f in (self.media, self.thumbnail) if f is not None]


log = logging.getLogger(__name__)


def ensure_ffmpeg() -> None:
    """Fail with a clear message when ffmpeg is needed but missing.

    ffmpeg is only bundled in the 'full' image variant. The default 'slim'
    image supports image (still) snapshots on Dahua/Hikvision/Bosch cameras
    via their HTTP API, but cannot do video snapshots or generic RTSP cameras.
    """
    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            "ffmpeg is not installed in this image. Video snapshots and generic "
            "RTSP cameras require the 'full' image variant (use the '-full' tag, "
            "e.g. ghcr.io/getdoover/cameras:main-full). Dahua/Hikvision/Bosch "
            "image snapshots work on the default 'slim' image."
        )


class CameraBase:
    # Whether this engine drives *daytime* capture off the camera's basic motion
    # detection (VMD) rather than its on-camera classification. The app needs to know
    # because unclassified motion is otherwise not actionable outside the night alarm
    # window — see CameraApplication.on_unclassified_motion.
    daytime_motion_capture = False

    # What this camera can do with detection zones, so the frontend can constrain
    # drawing (point limits, how many zones) instead of guessing and being silently
    # rejected. Engines that support zones override this; the default advertises
    # none, which is how the UI knows not to offer the editor.
    ZONE_CAPABILITIES = {
        "supported": False,
        "max_zones": 0,
        "min_points": 0,
        "max_points": 0,
        "targets": [],
        "supports_sensitivity": False,
        "supports_per_zone_targets": False,
        "supports_disable": False,
    }

    def __init__(self, config: "CameraConfig"):
        self.config = config

        self.ensure_output_dir()

    # -- Detection zones (device-agnostic; see events.DetectionZone) --

    async def get_detection_zones(self) -> list:
        """Return the camera's current zones as :class:`DetectionZone` objects."""
        return []

    async def set_detection_zones(self, zones: list) -> None:
        """Write ``zones`` to the camera. Overridden by engines that support it."""
        raise NotImplementedError(
            f"{type(self).__name__} does not support detection zones"
        )


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
        # Safety net: each snapshot deletes its own temp file, but a crash
        # between writing and unlinking could leave orphans that accumulate
        # forever (the filenames are random UUIDs). Sweep anything stale.
        cutoff = datetime.now() - timedelta(hours=1)
        for f in OUTPUT_FILE_DIR.iterdir():
            try:
                if f.is_file() and datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                    f.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _read_snapshot(fp: Path, filename: str, content_type: str) -> File:
        if not fp.exists() or fp.stat().st_size == 0:
            # ffmpeg can leave behind a 0-byte file when the camera is
            # unreachable; treat that as a failed snapshot rather than
            # returning an empty File.
            raise RuntimeError(f"ffmpeg produced no output at {fp}")
        return File(
            filename=filename,
            data=fp.read_bytes(),
            size=fp.stat().st_size,
            content_type=content_type,
        )

    async def on_control_message(self, message_id, data):
        pass

    async def fetch_presets(self) -> list[str]:
        return []

    def snapshot_func(self, still: bool = False) -> tuple:
        """The capture function to use, and the extension its output should carry.

        ``still`` forces an image even when the camera is configured for video. Motion
        capture passes it: that frame exists to be analysed in the cloud, which can't
        read an mp4, and at the rate motion fires video would be untenable to upload
        anyway.
        """
        if not still and Mode(self.config.snapshot.mode.value) is Mode.video:
            return self.get_video_snapshot, "mp4"
        return self.get_still_snapshot, "jpg"

    async def build_capture(
        self,
        name: str,
        media: File,
        with_thumbnail: bool = True,
        filetype: str = None,
    ) -> Capture:
        """Name a captured file and pair it with a thumbnail of the same view.

        Call this while the camera is still pointing where ``media`` was taken —
        the thumbnail is grabbed here and now, not later.
        """
        safe_name = re.sub(r"[^A-Za-z0-9_\-]", "_", name)
        filetype = filetype or self.config.snapshot.mode_as_filetype
        media.filename = f"{safe_name}.{filetype}"

        thumbnail = None
        if with_thumbnail:
            try:
                thumbnail = await self.get_thumbnail()
            except Exception as e:
                log.info(f"Couldn't build a thumbnail for {safe_name}: {e}")
            if thumbnail is not None:
                thumbnail.filename = f"{safe_name}{THUMBNAIL_SUFFIX}.jpg"

        return Capture(safe_name, media, thumbnail)

    async def get_snapshot(self, still: bool = False) -> list[Capture]:
        func, filetype = self.snapshot_func(still)

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

        return [await self.build_capture("snapshot", data, filetype=filetype)]

    async def get_thumbnail(self) -> File:
        """A small preview image for the gallery / timeline, or None if we can't.

        Grabs a single frame off the stream and scales it down. Cameras whose HTTP
        API can hand us a small image directly (e.g. Hikvision's sub-stream) should
        override this — it saves an ffmpeg pass and works on the slim image.
        """
        if shutil.which("ffmpeg") is None:
            # Not worth failing a snapshot over; the gallery can fall back to the
            # full-size media.
            return None

        fp = self.get_output_filepath(str(uuid.uuid4()), "jpg")
        cmd = (
            f"ffmpeg -y -rtsp_transport tcp -analyzeduration 10M -probesize 10M "
            f"-i {self.config.rtsp_uri} -frames:v 1 "
            f"-vf 'scale={THUMBNAIL_WIDTH}:-1' {fp}"
        )
        try:
            await self.run_ffmpeg_cmd(cmd)
            return self._read_snapshot(fp, THUMBNAIL_FILENAME, "image/jpeg")
        except Exception as e:
            log.info(f"Couldn't build a thumbnail: {e}")
            return None
        finally:
            fp.unlink(missing_ok=True)

    async def detect_night(self) -> bool:
        """Whether the camera is currently producing a night (IR) image.

        None means "can't tell from the camera" — the flag is then left off the
        payload and consumers work it out from the thumbnail instead. An IR frame is
        monochrome, so near-zero colour saturation gives it away; note it is NOT
        reliably dark (with the illuminator on, a night frame measured *brighter*
        than a colour test pattern), so brightness alone gets it backwards.
        """
        return None

    async def get_still_snapshot(self, rtsp_uri: str) -> File:
        fp = self.get_output_filepath(str(uuid.uuid4()), "jpg")
        cmd = f"ffmpeg -y -rtsp_transport tcp -analyzeduration 10M -probesize 10M -i {rtsp_uri} -vf 'scale={self.config.snapshot.scale.value.value}' -frames:v 1 {fp}"
        try:
            await self.run_ffmpeg_cmd(cmd)
            return self._read_snapshot(fp, "snapshot.jpg", "image/jpeg")
        finally:
            fp.unlink(missing_ok=True)

    async def get_video_snapshot(self, rtsp_uri: str, secs: int = None) -> File:
        # `secs` lets callers ask for a video of a specific length; snapshots use the
        # configured duration.
        secs = secs or self.config.snapshot.secs.value
        fp = self.get_output_filepath(str(uuid.uuid4()), "mp4")

        # possible alternative, allegedly h265 is the "new" best high-compression format.
        # ffmpeg -y -rtsp_transport tcp -i rtsp://10.144.239.221:554/s0 -vf
        # scale=420:-1 -r 10 -t 6 -vcodec libx265 -tag:v hvc1 -c:a aac output.mp4
        if self.config.snapshot.native_h264.value:
            # Stream-copy avoids decode/re-encode CPU cost; filters can't be applied to a copied stream.
            cmd = (
                f"ffmpeg -y -rtsp_transport tcp -analyzeduration 10M -probesize 10M -i {rtsp_uri} "
                f"-t {secs} -c:v copy -c:a aac {fp}"
            )
        else:
            cmd = (
                f"ffmpeg -y -rtsp_transport tcp -analyzeduration 10M -probesize 10M -i {rtsp_uri} -vf 'fps={self.config.snapshot.fps.value},scale={self.config.snapshot.scale.value.value},"
                f"format=yuv420p,pad=ceil(iw/2)*2:ceil(ih/2)*2' -t {secs} -c:v libx264 -c:a aac {fp}"
            )
        try:
            await self.run_ffmpeg_cmd(cmd)
            return self._read_snapshot(fp, "snapshot.mp4", "video/mp4")
        finally:
            fp.unlink(missing_ok=True)

    async def record_video_until(
        self, rtsp_uri: str, stop: asyncio.Event, max_secs: int
    ) -> File:
        """Record one continuous mp4 until ``stop`` is set, or ``max_secs`` elapses.

        Used for intruder event video, where the length isn't known up front — the
        recording runs for as long as the intruder keeps triggering detections. The
        ``-t`` cap is a backstop in case we never get told to stop.

        ffmpeg is interrupted rather than killed: an mp4 only gets its trailer (and
        so becomes playable) when ffmpeg shuts down cleanly, and SIGKILL would leave
        an unplayable file.
        """
        ensure_ffmpeg()
        self.ensure_output_dir()
        fp = self.get_output_filepath(str(uuid.uuid4()), "mp4")

        if self.config.snapshot.native_h264.value:
            encode = "-c:v copy -c:a aac"
        else:
            encode = (
                f"-vf 'fps={self.config.snapshot.fps.value},"
                f"scale={self.config.snapshot.scale.value.value},format=yuv420p,"
                f"pad=ceil(iw/2)*2:ceil(ih/2)*2' -c:v libx264 -c:a aac"
            )
        cmd = (
            f"ffmpeg -y -rtsp_transport tcp -analyzeduration 10M -probesize 10M "
            f"-i {rtsp_uri} -t {max_secs} {encode} {fp}"
        )
        log.info(f"running cmd: {cmd}")
        proc = await asyncio.create_subprocess_shell(cmd)

        try:
            try:
                await asyncio.wait_for(stop.wait(), timeout=max_secs)
            except asyncio.TimeoutError:
                log.info(f"Event video hit the {max_secs}s cap.")

            if proc.returncode is None:
                proc.send_signal(signal.SIGINT)
            await proc.wait()
            return self._read_snapshot(fp, "event.mp4", "video/mp4")
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            fp.unlink(missing_ok=True)

    async def remux_to_mp4(self, data: bytes, name: str) -> File:
        """Repackage a camera's recorded bytes into an mp4 that will actually play.

        Hikvision's ContentMgmt download doesn't hand back an mp4 — it's Hikvision's
        own ``IMKH`` container wrapping an MPEG program stream, which no player will
        touch. ffmpeg's PS demuxer reads it regardless (it skips the IMKH header), so
        the video is stream-copied straight across.

        The audio is the one thing that has to be re-encoded: these cameras record
        G.711 (``pcm_mulaw``), which mp4 cannot carry — a plain ``-c copy`` fails
        outright with "Could not find tag for codec pcm_mulaw".
        """
        ensure_ffmpeg()
        self.ensure_output_dir()
        src = self.get_output_filepath(str(uuid.uuid4()), "ps")
        dst = self.get_output_filepath(str(uuid.uuid4()), "mp4")
        src.write_bytes(data)

        cmd = (
            f"ffmpeg -y -i {src} -c:v copy -c:a aac -movflags +faststart {dst}"
        )
        try:
            await self.run_ffmpeg_cmd(cmd)
            return self._read_snapshot(dst, f"{name}.mp4", "video/mp4")
        finally:
            src.unlink(missing_ok=True)
            dst.unlink(missing_ok=True)

    async def run_ffmpeg_cmd(self, cmd):
        ensure_ffmpeg()
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
