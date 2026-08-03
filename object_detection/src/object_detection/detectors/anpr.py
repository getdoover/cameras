"""Number-plate detection and recognition.

Two stages, because no single model does both well at this size: a YOLO detector
finds plate boxes in the full frame, then each box is cropped and passed to an OCR
model that only ever sees a tight plate image.

A plate that's detected but unreadable is still returned (with ``text=None``). It's
genuine information -- a vehicle was there and its plate was obscured/too small --
and dropping it would make the annotated image disagree with the payload.
"""

import logging
import re

from ..yolo import MODEL_DIR, Detection, ModelUnavailable, YoloOnnx

log = logging.getLogger(__name__)

PLATE_MODEL_PATH = MODEL_DIR / "plate.onnx"

# The OCR model is trained on tight crops, so a box that's clipped exactly to the
# detected plate edge tends to lose the outermost characters. Pad it slightly.
CROP_PADDING = 0.08

# Plates are alphanumeric; OCR models pad short reads with a fill character and
# occasionally emit separators. Strip anything that can't be part of a plate.
PLATE_CHARS = re.compile(r"[^A-Z0-9]")

# Below this many pixels wide a crop carries too few characters' worth of detail for
# OCR to do anything but hallucinate, so it's reported as an unread plate instead.
MIN_CROP_WIDTH = 32


class Plate:
    def __init__(
        self, detection: Detection, text: str | None = None, ocr_confidence=None
    ):
        self.detection = detection
        self.text = text
        self.ocr_confidence = ocr_confidence

    @property
    def label(self) -> str:
        return self.text or "plate"

    def to_dict(self) -> dict:
        d = {
            "box": list(self.detection.box),
            "confidence": round(self.detection.confidence, 3),
            "plate": self.text,
        }
        if self.ocr_confidence is not None:
            d["ocr_confidence"] = round(self.ocr_confidence, 3)
        return d


class ANPRResult:
    def __init__(self, plates: list[Plate]):
        self.plates = plates

    @property
    def has_findings(self) -> bool:
        return bool(self.plates)

    @property
    def read_plates(self) -> list[Plate]:
        return [p for p in self.plates if p.text]

    def to_dict(self) -> dict:
        return {"plates": [p.to_dict() for p in self.plates]}


class ANPRDetector:
    def __init__(self, config):
        self.config = config
        self.model = YoloOnnx(PLATE_MODEL_PATH)
        self.ocr = self._load_ocr()

    @staticmethod
    def _load_ocr():
        """Load the plate OCR model, degrading to detection-only if unavailable.

        fast-plate-ocr is an optional capability rather than a hard requirement: if
        it can't load (weights not pre-cached and the site is offline, or the API
        moved between versions) plate *detection* still works and still annotates the
        image, which is more useful than the app refusing to start.
        """
        try:
            from fast_plate_ocr import LicensePlateRecognizer

            return LicensePlateRecognizer("cct-xs-v1-global-model")
        except Exception as e:
            log.warning(
                f"Plate OCR unavailable ({e}); plates will be detected and boxed but "
                f"not read.",
                exc_info=e,
            )
            return None

    def analyse(self, image, size: int) -> ANPRResult:
        """Detect plates and read them. CPU-bound; call in a thread."""
        detections = self.model.detect(
            image,
            confidence=self.config.confidence.value / 100,
            size=size,
        )

        plates = []
        for detection in detections:
            text, conf = self._read(image, detection)
            plates.append(Plate(detection, text, conf))
        return ANPRResult(plates)

    def _read(self, image, detection: Detection):
        if self.ocr is None:
            return None, None

        crop = self._crop(image, detection)
        if crop is None:
            return None, None

        try:
            text, conf = self._run_ocr(crop)
        except Exception as e:
            log.warning(f"Plate OCR failed: {e}", exc_info=e)
            return None, None

        text = PLATE_CHARS.sub("", (text or "").upper())
        if len(text) < self.config.min_plate_chars.value:
            # Too short to trust -- almost always a partial read of a plate that's at
            # an angle or half out of frame.
            return None, None
        return text, conf

    def _run_ocr(self, crop):
        """Call fast-plate-ocr and normalise its return shape.

        1.x returns ``[PlatePrediction(plate=..., char_probs=...)]``; older versions
        returned a bare list of strings or a ``(texts, confidences)`` tuple. All three
        are handled rather than pinning one version and having plate reads silently
        vanish on the next bump.
        """
        result = self.ocr.run(crop)

        confidence = None
        texts = result
        if isinstance(result, tuple):
            texts = result[0]
            confidence = self._min_confidence(result[1] if len(result) > 1 else None)

        if isinstance(texts, str):
            return texts, confidence
        if not texts:
            return None, confidence

        first = texts[0]
        # 1.x PlatePrediction. Read by attribute rather than isinstance so we don't
        # import a class whose location has moved between versions.
        plate = getattr(first, "plate", None)
        if plate is not None:
            return plate, self._min_confidence(getattr(first, "char_probs", None))
        return first, confidence

    @staticmethod
    def _min_confidence(probs):
        """Collapse per-character probabilities into one number.

        A plate is only as trustworthy as its least certain character, so this is the
        minimum rather than the mean -- one badly-read digit is what makes a plate
        wrong, and averaging hides it.
        """
        if probs is None:
            return None
        try:
            values = [float(v) for v in _flatten(probs)]
        except (TypeError, ValueError):
            return None
        return min(values) if values else None

    @staticmethod
    def _crop(image, detection: Detection):
        h, w = image.shape[:2]
        x1, y1, x2, y2 = detection.box
        pad_x = int((x2 - x1) * CROP_PADDING)
        pad_y = int((y2 - y1) * CROP_PADDING)
        x1, y1 = max(0, x1 - pad_x), max(0, y1 - pad_y)
        x2, y2 = min(w, x2 + pad_x), min(h, y2 + pad_y)

        if x2 - x1 < MIN_CROP_WIDTH or y2 <= y1:
            return None

        # Handed over as 3-channel BGR. fast-plate-ocr's ONNX input is NHWC with 3
        # channels and the library does its own resize, so passing a grayscale crop
        # fails the session with an "invalid dimensions for input" error rather than
        # degrading -- every plate read would come back empty.
        return image[y1:y2, x1:x2]


def _flatten(values):
    for value in values:
        if hasattr(value, "__iter__") and not isinstance(value, (str, bytes)):
            yield from _flatten(value)
        else:
            yield value


def load(config) -> ANPRDetector | None:
    try:
        return ANPRDetector(config)
    except ModelUnavailable as e:
        log.error(
            f"Plate recognition is enabled but the model can't be loaded: {e}. Run "
            f"scripts/fetch_models.py and rebuild the image."
        )
        return None
