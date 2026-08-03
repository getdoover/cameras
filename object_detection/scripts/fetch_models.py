#!/usr/bin/env python3
"""Fetch and prepare the ONNX weights this app ships.

Run on a dev machine, not the device -- exporting a YOLO .pt needs ultralytics and
torch, which never get installed into the device image. The resulting .onnx files in
models/ are committed so the image build (and a device with no internet) doesn't
depend on Hugging Face being reachable.

    uv run --group dev scripts/fetch_models.py

Weights and licences
--------------------
* PPE (models/ppe.onnx) -- Hansung-Cho/yolov8-ppe-detection, MIT. 10 classes; we use
  person / hardhat / no-hardhat / safety vest / no-safety vest. Published as a .pt,
  so it gets exported to ONNX here.

  Picked over the two obvious alternatives by measuring all three, at conf=0.3, on
  four images from keremberke/construction-safety-object-detection plus one real
  frame from a site camera:

    model                          ds0  ds1  ds2  ds3   real frame
    Hansung-Cho (MIT)               4    3    1    6    person found
    leeyunjai/yolo11-ppe            4    0    0    3    nothing
    baskarmother (MIT)              1    1    1    1    nothing

  Only Hansung-Cho reliably emits `Person` boxes at all -- baskarmother returned a
  single box per image and never found a person, which makes per-person compliance
  impossible. Don't swap the weights without re-running that comparison: this app's
  whole output depends on people being detected, not just equipment.
* Plate detection (models/plate.onnx) -- morsetechlab/yolov11-license-plate-detection,
  AGPL-3.0. Ships ONNX directly, so it's a straight download.
* Plate OCR -- ankandrew's fast-plate-ocr, pulled at image build time by the
  Dockerfile (it manages its own weight cache).

NOTE the plate detector is AGPL-3.0 while this repo is Apache-2.0. That is a
deliberate, reviewable choice mirroring cattle-cam (which ships AGPL YOLO weights) --
but if this app is ever distributed as a binary to a third party rather than run as
a service, swap it for a permissively-licensed plate detector.
"""

import shutil
import sys
import urllib.request
from pathlib import Path

MODELS_DIR = Path(__file__).parents[1] / "models"

PPE_REPO = "Hansung-Cho/yolov8-ppe-detection"
PPE_WEIGHTS = "best.pt"
PPE_OUTPUT = "ppe.onnx"

PLATE_URL = (
    "https://huggingface.co/morsetechlab/yolov11-license-plate-detection/"
    "resolve/main/license-plate-finetune-v1n.onnx"
)
PLATE_OUTPUT = "plate.onnx"

# Must match ObjectDetectionConfig.inference_size's default. The export bakes a
# fixed input size into the graph, so a mismatch at runtime fails the session.
IMAGE_SIZE = 640


def download(url: str, dest: Path):
    print(f"  downloading {url}")
    with urllib.request.urlopen(url) as response, dest.open("wb") as f:
        shutil.copyfileobj(response, f)
    print(f"  wrote {dest} ({dest.stat().st_size / 1e6:.1f}MB)")


def fetch_plate_model():
    dest = MODELS_DIR / PLATE_OUTPUT
    if dest.exists():
        print(f"{PLATE_OUTPUT} already present, skipping.")
        return
    print(f"Fetching plate detector -> {PLATE_OUTPUT}")
    download(PLATE_URL, dest)


def fetch_ppe_model():
    dest = MODELS_DIR / PPE_OUTPUT
    if dest.exists():
        print(f"{PPE_OUTPUT} already present, skipping.")
        return

    print(f"Fetching PPE model -> {PPE_OUTPUT}")
    try:
        from huggingface_hub import hf_hub_download
        from ultralytics import YOLO
    except ImportError:
        sys.exit(
            "ultralytics + huggingface_hub are needed to export the PPE model.\n"
            "Run: uv run --group dev scripts/fetch_models.py"
        )

    checkpoint = hf_hub_download(repo_id=PPE_REPO, filename=PPE_WEIGHTS)
    print(f"  got checkpoint {checkpoint}")

    model = YOLO(checkpoint)
    print(f"  classes: {model.names}")
    # opset 12 is what onnxruntime 1.19+ handles without warnings on aarch64.
    # simplify=False keeps onnxsim (another heavy dep) out of the picture.
    #
    # dynamic=True matters: without it ultralytics pins H/W in the graph and
    # onnxruntime *rejects* any other input size ("Got invalid dimensions for input")
    # rather than resizing. The cloud processor's whole advantage is being able to run a
    # larger inference size than a Doovit can afford, so a fixed 640 graph would silently
    # cap it at the device's limit. `imgsz` remains the export-time reference shape.
    exported = model.export(
        format="onnx", imgsz=IMAGE_SIZE, opset=12, simplify=False, dynamic=True
    )
    shutil.move(str(exported), dest)
    print(f"  wrote {dest} ({dest.stat().st_size / 1e6:.1f}MB)")


def main():
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    fetch_ppe_model()
    fetch_plate_model()
    print("\nDone. Commit the .onnx files in models/ so the image build is offline.")


if __name__ == "__main__":
    main()
