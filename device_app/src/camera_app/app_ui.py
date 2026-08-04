from pydoover import ui

from .app_config import CameraConfig, CameraType
from .events import SET_ZONES_CMD


class ObjectDetections(ui.Container):
    type = "uiCameraDetections"

class CameraUI(ui.UI):
    config: CameraConfig

    # fixme: this is my dream static ui with references (not, and, etc.) to config, but it needs a bit of work
    # that's not a now job
    # tabs = ui.TabContainer(
    #     name="tabs",
    #     display_name="Tabs",
    #     children=[
    #         ui.CameraHistory("$config.app().APP_KEY"),
    #         ui.CameraLiveView(
    #             "$config.app().APP_KEY",
    #             "$config.app().APP_KEY",
    #             "$config.app().control_enabled:boolean",
    #             display_name="Live View",
    #             presets=CameraTags.presets,
    #             active_preset=CameraTags.active_preset,
    #         ),
    #         ui.CameraLiveView(
    #             "$config.app().APP_KEY",
    #             Q("$config.app().APP_KEY:string") + "_thermal",
    #             "$config.app().control_enabled:boolean",
    #             display_name="Live View (Thermal)",
    #             hidden=not (Q("$config.app().type:string") == "hikvision_thermal"),
    #             presets=CameraTags.presets,
    #             active_preset=CameraTags.active_preset,
    #         ),
    #         ui.Container(
    #             name="detection",
    #             display_name="Object Detection",
    #             children=[],
    #         ),
    #     ],
    # )
    async def setup(self):
        app_key = self.app_key
        self.history = ui.CameraHistory(app_key)

        self.live_view = ui.CameraLiveView(
            app_key,
            app_key,
            self.config.control_enabled.value,
            name=f"{app_key}_lv",
            display_name="Live View",
            presets=self.tags.presets,
            active_preset=self.tags.active_preset,
        )

        if CameraType(self.config.type.value) is CameraType.hikvision_thermal:
            self.thermal_live_view = ui.CameraLiveView(
                app_key,
                f"{app_key}_thermal",
                self.config.control_enabled.value,
                name=f"{app_key}_thermal_liveview",
                display_name="Live View (Thermal)",
                presets=self.tags.presets,
                active_preset=self.tags.active_preset,
            )
            live_views = [self.live_view, self.thermal_live_view]
        else:
            live_views = [self.live_view]

        # Carries JSON rather than a simple value, so it's a plain interaction the
        # zone editor drives. It has to be in the tree for the UI command manager to
        # resolve the handler (it looks the interaction up by name to build the
        # handler's context). Its *value* is the current zone state, the same way a
        # Switch's value is its current state — so there's no separate read path.
        # See CameraApplication.on_set_detection_zones.
        self.set_detection_zones = ui.Interaction(
            "Set Detection Zones",
            name=SET_ZONES_CMD,
            hidden=True,
        )

        self.tab_container = ui.TabContainer(
            children=[self.history, *live_views, ObjectDetections("Object Detections")],
            name="tabs",
            display_name="Tabs",
        )
        self.add_element(self.tab_container)
        self.add_element(self.set_detection_zones)


def export():
    pass
