from pathlib import Path

from pydoover import config


class PPEConfig(config.Object):
    """Hard-hat / high-vis compliance checking.

    The model classifies *equipment*, not compliance: it emits `person`,
    `hardhat`/`no-hardhat` and `safety vest`/`no-safety vest` boxes independently.
    Turning that into "this person is missing a hard hat" is done in
    detectors/ppe.py by matching equipment boxes onto person boxes, which is why
    the two requirements below are separate switches rather than one "PPE" flag.
    """

    enabled = config.Boolean(
        "Enabled",
        description="Run hard-hat / high-vis detection on incoming snapshots.",
        default=False,
    )
    require_hard_hat = config.Boolean(
        "Require Hard Hat",
        description="Flag a person who isn't wearing a hard hat.",
        default=True,
    )
    require_high_vis = config.Boolean(
        "Require High-Vis",
        description="Flag a person who isn't wearing a high-vis vest.",
        default=True,
    )
    confidence = config.Integer(
        "Minimum Confidence",
        description="Drop detections below this confidence (0-100).",
        default=40,
        minimum=1,
        maximum=100,
        advanced=True,
    )
    notify_on_violation = config.Boolean(
        "Notify On Violation",
        description="Send a notification when someone is missing required PPE.",
        default=True,
    )


class ANPRConfig(config.Object):
    """Number-plate detection + OCR.

    Two models: a YOLO plate *detector* crops the plate out of the frame, then an
    OCR model reads the crop. A plate that's detected but unreadable still counts
    as a detection (and is drawn on the annotated image) with no text.
    """

    enabled = config.Boolean(
        "Enabled",
        description="Detect and read vehicle number plates on incoming snapshots.",
        default=False,
    )
    confidence = config.Integer(
        "Minimum Confidence",
        description="Drop plate detections below this confidence (0-100).",
        default=40,
        minimum=1,
        maximum=100,
        advanced=True,
    )
    min_plate_chars = config.Integer(
        "Minimum Plate Characters",
        description="Discard OCR results shorter than this many characters. Guards "
        "against a partial read being published as a real plate.",
        default=4,
        minimum=1,
        maximum=12,
        advanced=True,
    )
    notify_on_plate = config.Boolean(
        "Notify On Plate Read",
        description="Send a notification for every plate read. Off by default -- on a "
        "busy site this is a lot of notifications.",
        default=False,
    )


class ObjectDetectionConfig(config.Schema):
    camera_app_keys = config.Array(
        "Camera Apps",
        description="App keys of the camera apps whose snapshots to analyse, e.g. "
        "'doover_camera_1'. One instance can watch several cameras -- preferred over "
        "deploying one instance per camera, since each instance loads its own copy of "
        "the models into RAM.",
        element=config.String("App Key", description="Camera app key"),
    )

    ppe = PPEConfig("PPE Detection")
    anpr = ANPRConfig("Number Plate Recognition")

    analyse_reasons = config.Array(
        "Analyse Snapshots Because Of",
        description="Only analyse snapshots captured for these reasons. Matches the "
        "'reason' field the camera app publishes. Empty means analyse everything.",
        element=config.Enum(
            "Reason",
            description="Snapshot reason",
            choices=[
                "schedule",
                "manual",
                "intruder",
                "person",
                "vehicle",
                "anpr",
                "ppe",
            ],
            default="intruder",
        ),
    )

    annotate = config.Boolean(
        "Annotate Images",
        description="Draw labelled boxes on the frame and publish it back to the "
        "camera's channel so the timeline shows what was flagged.",
        default=True,
    )
    publish_clean_results = config.Boolean(
        "Publish Results With No Findings",
        description="Publish a result even when nothing was detected. Off by default so "
        "the camera timeline isn't filled with empty analyses.",
        default=False,
        advanced=True,
    )
    inference_size = config.Integer(
        "Inference Size",
        description="Square size (px) frames are letterboxed to before inference. "
        "Larger catches smaller/more distant subjects but costs CPU time and RAM.",
        default=640,
        minimum=320,
        maximum=1280,
        advanced=True,
    )

    @property
    def watched_app_keys(self) -> list[str]:
        return [e.value for e in self.camera_app_keys.elements if e.value]

    @property
    def wanted_reasons(self) -> set[str]:
        """Snapshot reasons to analyse. Empty set means "everything"."""
        return {e.value for e in self.analyse_reasons.elements if e.value}


def export():
    ObjectDetectionConfig().export(
        Path(__file__).parents[2] / "doover_config.json", "object_detection"
    )


if __name__ == "__main__":
    export()
