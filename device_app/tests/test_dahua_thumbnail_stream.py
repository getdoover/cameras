"""Dahua thumbnails must never change the camera's stream codecs.

Requesting mjpg/video.cgi for a stream that isn't MJPEG makes a Dahua re-encode
that stream as MJPEG and save it, which breaks the H.264 WebRTC live view on the
sub stream (seen on Maxitool Porgera Stations 1 and 8, 2026-11).
"""

from unittest.mock import AsyncMock

import pytest

from camera_app.clients import dahua
from camera_app.clients.dahua import DahuaClient

JPEG = b"\xff\xd8" + b"x" * 100 + b"\xff\xd9"


def encode(*extras):
    """An Encode getConfig answer with the given (compression, enabled) extra streams."""
    config = {"table.Encode[0].MainFormat[0].Video.Compression": "H.264"}
    for i, (compression, enabled) in enumerate(extras):
        config[f"table.Encode[0].ExtraFormat[{i}].Video.Compression"] = compression
        config[f"table.Encode[0].ExtraFormat[{i}].VideoEnable"] = (
            "true" if enabled else "false"
        )
    return config


class FakeContent:
    async def iter_chunks(self):
        yield JPEG, True


class FakeResponse:
    content = FakeContent()

    def raise_for_status(self):
        pass

    def close(self):
        pass


@pytest.fixture
def client(monkeypatch):
    requested = []

    class FakeAuth:
        def __init__(self, *args, **kwargs):
            pass

        async def request(self, method, url, **kwargs):
            requested.append(url)
            return FakeResponse()

    monkeypatch.setattr(dahua, "DigestAuth", FakeAuth)
    c = DahuaClient("admin", "pw", "192.168.1.13", 80, 554, session=None)
    c.requested = requested
    return c


@pytest.mark.asyncio
async def test_h264_sub_stream_is_never_asked_for_mjpeg(client):
    client.get_config = AsyncMock(return_value=encode(("H.264", True), ("H.265", True)))
    assert await client.get_substream_frame() is None
    assert client.requested == []


@pytest.mark.asyncio
async def test_mjpeg_sub_stream_gives_a_frame(client):
    client.get_config = AsyncMock(return_value=encode(("MJPG", True)))
    assert await client.get_substream_frame() == JPEG
    assert client.requested == [
        "http://192.168.1.13:80/cgi-bin/mjpg/video.cgi?channel=1&subtype=1"
    ]


@pytest.mark.asyncio
async def test_third_stream_used_when_sub_stream_stays_h264(client):
    client.get_config = AsyncMock(return_value=encode(("H.264", True), ("MJPG", True)))
    assert await client.get_substream_frame() == JPEG
    assert client.requested == [
        "http://192.168.1.13:80/cgi-bin/mjpg/video.cgi?channel=1&subtype=2"
    ]


@pytest.mark.asyncio
async def test_disabled_mjpeg_stream_is_skipped(client):
    client.get_config = AsyncMock(return_value=encode(("H.264", True), ("MJPG", False)))
    assert await client.get_substream_frame() is None
    assert client.requested == []


@pytest.mark.asyncio
async def test_unreadable_encode_config_asks_for_nothing(client):
    client.get_config = AsyncMock(return_value={})
    assert await client.get_substream_frame() is None
    assert client.requested == []
