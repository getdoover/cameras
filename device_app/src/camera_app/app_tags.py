from pydoover.tags import Tag, Tags

from .app_config import CameraConfig


class CameraTags(Tags):
    config: CameraConfig
    presets = Tag("array[string]", [])
    active_preset = Tag("string", "")
    last_cam_snapshot = Tag("number", 0)

    # async def setup(self):
    #     self.add_tag(f"camera_power_{self.config.power.pin.value}", Tag("number", 0))
