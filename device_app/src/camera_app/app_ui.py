from pydoover import ui

from .app_config import CameraConfig, CameraType
from .app_tags import CameraTags
from .events import SET_ZONES_CMD


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

        # Deliberately empty, and deliberately still here: it holds the third tab's place
        # so the tab layout doesn't shift.
        #
        # It used to carry "Alert me for Vehicle/Human Motion" switches that gated the
        # notifications. Detections now always notify, so the switches are gone — and they
        # were worse than redundant: they were created with no value, and each was `hidden`
        # unless its target was in the Object Detection config, so reading one raised
        # `KeyError: alert_me_on_human_motion` out of the middle of the motion callback and
        # took the rest of the handler with it.
        container = ui.Container(
            children=[],
            name="detection",
            display_name="Object Detection",
        )

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
            children=[self.history, *live_views, container],
            name="tabs",
            display_name="Tabs",
        )
        self.add_element(self.tab_container)
        self.add_element(self.set_detection_zones)


def export():
    pass
