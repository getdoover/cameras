from pydoover import ui

from .app_config import CameraConfig, CameraType
from .app_tags import CameraTags


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
    #             hidden=not (
    #                 Q("$config.app().human_detect_enabled:boolean")
    #                 | Q("$config.app().human_detect_enabled:boolean")
    #             ),
    #             children=[
    #                 ui.Switch(
    #                     "Alert me for Vehicle Motion",
    #                     name="alert_me_on_vehicle_motion",
    #                     icon="fa-car",
    #                     # hidden=not config.vehicle_detect_enabled,
    #                     hidden=not Q("$config.app().vehicle_detect_enabled:boolean"),
    #                 ),
    #                 ui.Switch(
    #                     "Alert me for Human Motion",
    #                     name="alert_me_on_human_motion",
    #                     icon="fa-user",
    #                     hidden=not Q("$config.app().human_detect_enabled:boolean"),
    #                     # hidden=not config.human_detect_enabled,
    #                 ),
    #             ],
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

        self.vehicle_detection = ui.Switch(
            "Alert me for Vehicle Motion",
            name="alert_me_on_vehicle_motion",
            icon="fa-car",
            hidden=not self.config.vehicle_detect_enabled,
        )
        self.human_detection = ui.Switch(
            "Alert me for Human Motion",
            name="alert_me_on_human_motion",
            icon="fa-user",
            hidden=not self.config.human_detect_enabled,
        )
        container = ui.Container(
            children=[self.vehicle_detection, self.human_detection],
            name="detection",
            display_name="Object Detection",
            hidden=not (
                self.config.vehicle_detect_enabled or self.config.human_detect_enabled
            ),
        )

        self.tab_container = ui.TabContainer(
            children=[self.history, *live_views, container],
            name="tabs",
            display_name="Tabs",
        )
        self.add_element(self.tab_container)
