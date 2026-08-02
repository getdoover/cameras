# Object Detection

<img src="https://doover.com/wp-content/uploads/Doover-Logo-Landscape-Navy-padded-small.png" alt="Doover Logo" style="max-width: 300px;">

**On-device hard-hat / high-vis compliance checking and number-plate recognition for
camera snapshots.**

<br/>

## Overview

This app watches the camera apps on a Doovit. Every time a camera publishes a
snapshot, it fetches the image, runs the enabled detectors over it, and publishes the
result **back to that camera's own channel** — so the finding lands in the camera's
timeline next to the picture it came from, with an annotated copy attached.

Everything runs on the device. There is no cloud inference and no API key: the models
are ONNX files baked into the image, executed by onnxruntime on the CPU.

<br/>

## Configuration

| Setting | Description | Default |
|---------|-------------|---------|
| **Camera Apps** | App keys of the camera apps to watch, e.g. `doover_camera_1` | `Required` |
| **PPE › Enabled** | Run hard-hat / high-vis detection | `false` |
| **PPE › Require Hard Hat** | Flag a person not wearing a hard hat | `true` |
| **PPE › Require High-Vis** | Flag a person not wearing a high-vis vest | `true` |
| **PPE › Minimum Confidence** | Drop detections below this confidence (0–100) | `40` |
| **PPE › Notify On Violation** | Notify when someone is missing required PPE | `true` |
| **ANPR › Enabled** | Detect and read vehicle number plates | `false` |
| **ANPR › Minimum Confidence** | Drop plate detections below this confidence | `40` |
| **ANPR › Minimum Plate Characters** | Discard OCR reads shorter than this | `4` |
| **ANPR › Notify On Plate Read** | Notify on every plate read | `false` |
| **Analyse Snapshots Because Of** | Only analyse snapshots with these `reason`s. Empty = everything | *(all)* |

| **Annotate Images** | Draw labelled boxes and publish the annotated frame | `true` |
| **Publish Results With No Findings** | Publish even when nothing was detected | `false` |
| **Inference Size** | Square size (px) frames are letterboxed to | `640` |

### The camera app can opt frames in or out

The camera app's **Motion Snapshot Config › Object Detection** setting puts
`"object_detection": true|false` on its motion snapshots. That flag is **authoritative
in both directions and overrides the reason filter above** — the camera is the thing
that knows whether a given frame was captured to be analysed, and honouring only the
`true` case would leave no way to opt one camera out. Snapshots with no flag fall
through to the reason filter as normal.

<br/>

> **One instance can watch several cameras**, and that's the preferred setup — each
> instance loads its own copy of the models, and a Doovit has well under a gigabyte
> of RAM to spare. Add every camera to **Camera Apps** on a single install rather
> than deploying one install per camera.

<br/>

## What it publishes

### To the camera's own channel (the timeline)

A message shaped like the camera app's own snapshot messages, so an existing gallery
renders it without special-casing:

```json
{
  "reason": "object_detection",
  "analysed_by": "object_detection_1",
  "source": {"message_id": "…", "file": "Preset1.jpg", "name": "Preset1", "reason": "intruder"},
  "summary": "1 person missing hard hat, high vis",
  "media": [{"name": "Preset1", "file": "Preset1-detected.jpg"}],
  "findings": {
    "ppe": {
      "people": [{"box": [1509, 814, 1920, 1080], "confidence": 0.5,
                  "hard_hat": false, "high_vis": null, "implied": false}],
      "violations": [{"box": [1509, 814, 1920, 1080], "missing": ["hard_hat", "high_vis"]}]
    },
    "anpr": {"plates": [{"box": [], "confidence": 0.9, "plate": "ABC123"}]}
  }
}
```

`analysed_by` is not decoration — this app publishes into the very channel it
subscribes to, and that key is what stops each result coming back as a new snapshot
to analyse. Anything carrying it is skipped on ingest.

### To `camera_event` (automations)

The same channel the camera app uses, so automations hook off findings the same way:

```json
{"kind": "ppe_violation", "app_key": "doover_camera_1", "detected_by": "object_detection_1",
 "timestamp": "…", "count": 1, "missing": ["hard_hat"]}
```

`kind` is `ppe_violation` or `anpr` (which also carries `plate` and `confidence`).

<br/>

## How compliance is decided

The model detects **equipment, not compliance**. It emits independent boxes for
`person`, `hardhat`/`no-hardhat` and `safety vest`/`no-safety vest`, and the app turns
that into a per-person verdict. Two things there are easy to get wrong:

**Equipment is matched to people by containment, not IoU.** A hard hat is a tiny box
inside a large person box, so their IoU is near zero even when it's obviously being
worn. What matters is how much of the *equipment* box falls inside the person box.
There's a test pinning this (`test_iou_would_fail_here`) precisely because IoU looks
like the obvious choice.

**A positive detection always beats a negative one.** These models routinely emit both
`hardhat` and `no-hardhat` over the same head at similar confidence. So a person is
compliant if a *positive* box matches them, and is only flagged when none does. The
`no-*` classes are corroboration and a way to catch people the `person` class missed —
never the sole basis for a violation.

When the model sees a bare head or a vestless torso but no `person`, that equipment box
stands in for a person (`"implied": true`) so the violation isn't silently dropped.

> **Camera placement matters more than any setting here.** These models are trained on
> upright, roughly eye-level site footage. On a wide-angle camera mounted rotated, or
> looking steeply down, they miss people that a general-purpose COCO model finds
> easily. If detection seems poor, check the framing before touching confidence.

<br/>

## Models

| Purpose | Weights | Licence |
|---|---|---|
| PPE (`models/ppe.onnx`) | [Hansung-Cho/yolov8-ppe-detection](https://huggingface.co/Hansung-Cho/yolov8-ppe-detection) | MIT |
| Plate detection (`models/plate.onnx`) | [morsetechlab/yolov11-license-plate-detection](https://huggingface.co/morsetechlab/yolov11-license-plate-detection) (v1n) | **AGPL-3.0** |
| Plate OCR | [fast-plate-ocr](https://github.com/ankandrew/fast-plate-ocr) `cct-xs-v1-global-model`, cached at image build | MIT |

The `.onnx` files are committed so the image build — and a device with no internet —
doesn't depend on Hugging Face. Regenerate them with:

```bash
uv run --group dev scripts/fetch_models.py
```

The PPE model was chosen by measuring three candidates at `conf=0.3` on four images
from `keremberke/construction-safety-object-detection` plus a real site frame:

| Model | ds0 | ds1 | ds2 | ds3 | Real frame |
|---|---|---|---|---|---|
| **Hansung-Cho (MIT)** | 4 | 3 | 1 | 6 | person found |
| leeyunjai/yolo11-ppe | 4 | 0 | 0 | 3 | nothing |
| baskarmother (MIT) | 1 | 1 | 1 | 1 | nothing |

Only Hansung-Cho reliably emits `Person` boxes at all; baskarmother returned one box
per image and never found a person, which makes per-person compliance impossible.
**Don't swap the weights without re-running that comparison** — the app's entire
output depends on people being detected, not just equipment.

> The plate detector is AGPL-3.0 while this repo is Apache-2.0 — a deliberate,
> reviewable choice mirroring `cattle-cam`, which also ships AGPL YOLO weights. Fine
> for running as a service; if this app is ever distributed as a binary to a third
> party, swap it for a permissively-licensed plate detector first.

<br/>

## Why not ultralytics, and why not the standard base image

**No ultralytics / torch.** ultralytics imports torch unconditionally, and there's no
torch build that fits comfortably on a Pi CM4 with ~900MB of RAM free alongside the
other app containers. The YOLO pre/post-processing is ~150 lines in `yolo.py` instead,
and is verified byte-identical to ultralytics on the same model and image (same boxes,
confidences and coordinates).

**Not built on `spaneng/doover_device_base`.** That base is now Alpine/musl. Its Python
is 3.11 at `/usr/local`, while Alpine's `py3-onnxruntime` and `py3-opencv` packages
install for Alpine's *system* Python 3.12 under `/usr/lib/python3.12` — a different
ABI, so the app's interpreter can't import them. PyPI publishes no musllinux wheels
for onnxruntime or opencv, so getting an inference stack onto that base would mean
compiling onnxruntime for musl. This app uses `python:3.11-slim-bookworm`, which has
manylinux aarch64 wheels for everything, and re-declares the labels and healthcheck
the app controller looks for.

<br/>

## Performance

One 1920×1080 frame at `inference_size=640` takes roughly 130ms per model on a
development machine; expect several times that on a CM4. Snapshots are minutes to
hours apart, so this is comfortably fast enough — the design constraint is memory, not
latency, which is why inference is serialised behind a lock and the compose template
sets `mem_limit`.

<br/>

## Need Help?

- Email: support@doover.com
- [Community Forum](https://doover.com/community)
- [Full Documentation](https://docs.doover.com)

<br/>

## License

This app is licensed under the [Apache License 2.0](LICENSE). Note the model weight
licences in the table above.
