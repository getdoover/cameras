import asyncio
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydoover.models import File

from camera_app.application import CameraApplication
from camera_app.app_config import Mode
from camera_app.engines import base
from camera_app.engines.hikvision_thermal import HikVisionThermal


@pytest.fixture
def video(tmp_path):
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip(
            "ffmpeg and ffprobe are required for video thumbnail integration tests"
        )
    path = tmp_path / "source.mp4"
    # Only the first frame is red; later frames are blue.
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=640x360:r=2:d=1",
            "-vf",
            "drawbox=color=red:t=fill:enable='eq(n,0)'",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    data = path.read_bytes()
    return File(
        filename="source.mp4", data=data, size=len(data), content_type="video/mp4"
    )


@pytest.fixture
def camera(tmp_path, monkeypatch):
    # Spaces also exercise quoting of paths passed to ffmpeg.
    monkeypatch.setattr(base, "OUTPUT_FILE_DIR", tmp_path / "camera output")
    config = SimpleNamespace(
        snapshot=SimpleNamespace(
            mode_as_filetype="mp4", mode=SimpleNamespace(value=Mode.video.value)
        ),
        rtsp_uri="visible",
        thermal_rtsp_uri="thermal",
    )
    camera = base.CameraBase(config)
    camera.get_thumbnail = AsyncMock(
        side_effect=AssertionError("must not open a live stream")
    )
    return camera


def application(camera):
    app = CameraApplication.__new__(CameraApplication)
    app.app_key = "cam"
    app.app_display_name = "Test camera"
    app.engine = camera
    app.device_agent = SimpleNamespace(create_message=AsyncMock())
    app.detector_zones = lambda: []
    return app


def assert_first_frame(thumbnail, tmp_path):
    assert thumbnail.content_type == "image/jpeg"
    assert thumbnail.data.startswith(b"\xff\xd8")
    path = tmp_path / thumbnail.filename
    path.write_bytes(thumbnail.data)
    dimensions = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert dimensions == "320,180"
    pixel = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-vf",
            "scale=1:1",
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
    ).stdout
    assert pixel[0] > 200 and pixel[1] < 40 and pixel[2] < 40


@pytest.mark.asyncio
async def test_capture_uploads_small_first_frame_alongside_unchanged_video(
    camera, video, tmp_path
):
    original = video.data
    capture = await camera.build_capture("Preset 1", video)
    assert capture.thumbnail is not None
    assert capture.thumbnail.filename == "Preset_1-thumbnail.jpg"
    assert capture.thumbnail.size < video.size
    assert_first_frame(capture.thumbnail, tmp_path)
    assert video.data == original
    camera.get_thumbnail.assert_not_awaited()
    assert list(base.OUTPUT_FILE_DIR.iterdir()) == []

    app = application(camera)
    await app.upload_media([capture], "schedule")
    app_key, payload, files = app.device_agent.create_message.call_args.args
    assert app_key == "cam"
    assert payload["media"] == [
        {
            "name": "Preset_1",
            "file": "Preset_1.mp4",
            "thumbnail": "Preset_1-thumbnail.jpg",
        }
    ]
    assert files == [video, capture.thumbnail]


@pytest.mark.asyncio
@pytest.mark.parametrize("thumbnail_fails", [False, True])
async def test_event_video_uses_recorded_first_frame(
    camera, video, tmp_path, thumbnail_fails
):
    app = application(camera)
    app.config = SimpleNamespace(
        alarm=SimpleNamespace(event_clip_max_secs=SimpleNamespace(value=30))
    )
    app.power_management = SimpleNamespace(acquire=AsyncMock())
    app.watch_for_event_end = AsyncMock()
    camera.record_event_video = AsyncMock(return_value=video)
    if thumbnail_fails:
        camera.run_ffmpeg_cmd = AsyncMock(side_effect=RuntimeError("ffmpeg failed"))
    await app.run_event_video()

    _, payload, files = app.device_agent.create_message.call_args.args
    if thumbnail_fails:
        assert payload["media"] == [{"name": "event", "file": "event.mp4"}]
        assert files == [video]
    else:
        assert payload["media"] == [
            {
                "name": "event",
                "file": "event.mp4",
                "thumbnail": "event-thumbnail.jpg",
            }
        ]
        assert files[0] is video
        assert_first_frame(files[1], tmp_path)
    camera.get_thumbnail.assert_not_awaited()
    assert list(base.OUTPUT_FILE_DIR.iterdir()) == []


@pytest.mark.asyncio
async def test_thermal_video_has_its_own_thumbnail(camera, video):
    thermal = File(
        filename="thermal.mp4",
        data=video.data,
        size=video.size,
        content_type="video/mp4",
    )
    camera.get_video_snapshot = AsyncMock(side_effect=[video, thermal])
    camera.get_video_thumbnail = AsyncMock(
        side_effect=[
            File(
                filename="thumb.jpg", data=b"visible", size=7, content_type="image/jpeg"
            ),
            File(
                filename="thumb.jpg", data=b"thermal", size=7, content_type="image/jpeg"
            ),
        ]
    )
    captures = await HikVisionThermal.get_snapshot(camera)
    assert [capture.thumbnail.data for capture in captures] == [b"visible", b"thermal"]
    assert [call.args[0] for call in camera.get_video_thumbnail.call_args_list] == [
        video,
        thermal,
    ]
    camera.get_thumbnail.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [RuntimeError("ffmpeg failed"), asyncio.CancelledError()]
)
async def test_thumbnail_temp_files_cleaned_on_failure(camera, video, failure):
    camera.run_ffmpeg_cmd = AsyncMock(side_effect=failure)
    if isinstance(failure, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await camera.build_capture("snapshot", video)
    else:
        capture = await camera.build_capture("snapshot", video)
        assert capture.files() == [video]
    camera.run_ffmpeg_cmd.assert_awaited_once()
    assert list(base.OUTPUT_FILE_DIR.iterdir()) == []


@pytest.mark.asyncio
async def test_invalid_video_does_not_upload_empty_thumbnail(camera, video):
    video.data = b"invalid mp4"
    video.size = len(video.data)
    capture = await camera.build_capture("snapshot", video)
    assert capture.files() == [video]
    assert list(base.OUTPUT_FILE_DIR.iterdir()) == []
