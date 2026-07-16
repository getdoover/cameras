# IP Camera

<img src="https://doover.com/wp-content/uploads/Doover-Logo-Landscape-Navy-padded-small.png" alt="Doover Logo" style="max-width: 300px;">

**View and manage IP cameras with support for Dahua PTZ, Dahua Fixed, UniFi, and generic IP cameras.**


[![Version](https://img.shields.io/badge/version-1.0.0-blue.svg)](https://github.com/getdoover/cameras)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/getdoover/cameras/blob/main/LICENSE)

[Configuration](#configuration) | [Developer](https://github.com/getdoover/cameras/blob/main/DEVELOPMENT.md) | [Need Help?](#need-help)

<br/>

## Overview

App to view and manage IP cameras. Choose between Dahua PTZ or Fixed, UniFi and more. Features include live streaming via HLS, periodic snapshots, object detection, and optional power control for remote camera management.

<br/>

## Configuration

| Setting | Description | Default |
|---------|-------------|---------|
| **Camera Type** | Type of camera (Dahua PTZ/Fixed/Generic, UniFi Generic, Generic IP, Hikvision Thermal, Hikvision ANPR, Hikvision AcuSense, Bosch PTZ) | `Dahua (Generic)` |
| **Camera Username** | Username to login to camera control | `None` |
| **Camera Password** | Password to login to camera control | `None` |
| **IP Address** | IP address of camera (e.g. 192.168.50.100) | `Required` |
| **RTSP Port** | Port of RTSP feed on camera | `554` |
| **RTSP Channel** | RTSP channel name | `live` |
| **Control Port** | Port of control page on camera | `80` |
| **Power Control Enabled** | Whether power control is enabled for this camera | `false` |
| **Power Pin** | Digital Output pin that controls power to camera circuit | `0` |
| **Off After** | Seconds after which the camera will be powered off | `900` |
| **Wake Delay** | Seconds for camera to boot before requesting a snapshot | `5` |
| **Live View Enabled** | Whether remote component is enabled for this camera | `true` |
| **Live View URL** | URL for live view component | `https://getdoover.github.io/cameras/HLSLiveView.js` |
| **Snapshot Enabled** | Whether periodic snapshots are enabled | `true` |
| **Snapshot Period** | Seconds between snapshots | `14400` |
| **Snapshot Mode** | Video or Image format | `Image` |
| **Object Detection** | Objects to detect (Person, Vehicle) | `None` |
| **Control Enabled** | Allow control (movement) of PTZ cameras | `true` |

<br/>

### Hikvision ANPR (DeepinView `/P` models, e.g. iDS-2CD7A46G2/P-IZHSY)

These cameras dedicate their AI engine to **ANPR (license plate + vehicle detection)** over ISAPI —
they have **no** on-camera person/PPE/face classification. The driver surfaces plate reads and, for a
night-time intruder alarm, falls back to basic motion detection (which coexists with ANPR).

| Setting | Description | Default |
|---------|-------------|---------|
| **ANPR › Enabled** | Enable on-camera license-plate + vehicle detection | `false` |
| **ANPR › Minimum Confidence** | Drop plate reads below this confidence (0–100) | `0` |
| **Intruder Alarm › Enabled** | At night, pulse the alarm-output relay + notify on motion | `false` |
| **Intruder Alarm › Alarm Output Port** | Camera relay driving the external siren/strobe | `1` |
| **Intruder Alarm › Alarm Pulse Duration** | Seconds to hold the relay on per trigger | `10` |
| **Intruder Alarm › Camera Buzzer** | Also arm the camera's native buzzer at night (fires even if doover is offline) | `true` |
| **Intruder Alarm › Night Start / End Hour** | Hour window (site-local) the intruder alarm is armed | `18` / `6` |

> Note: this firmware does **not** support linking motion directly to the alarm-output relay, so the
> external siren/strobe is pulsed by the app on each night motion event; the native buzzer linkage is the
> only doover-independent local alarm. PPE/hard-hat and face access-control are not supported by this
> hardware and require server-side inference or different cameras.

<br/>

### Hikvision AcuSense ColorVu (e.g. DS-2CD2387G3-LIS2UY/SRB)

AcuSense cameras classify targets **on-camera as human / vehicle / animal** via intrusion (field)
detection over ISAPI — this is the driver for **person / intruder detection**. On setup the app
**creates the intrusion rule** if the camera doesn't have one, with a default full-frame zone detecting
**both human and vehicle** at the configured **Detection Sensitivity**. Events carry the classification
per-event and map onto the `person` / `vehicle` callbacks. (The **Object Detection** setting shapes which
events raise notifications downstream; it no longer limits what the camera looks for.)

The night intruder alarm uses the camera's built-in **active response** — on `/SRB` models the flash light
**and** audible siren fire together (`LightAudioAlarm` linkage) on a detection, natively. Where the
firmware accepts it, the app writes the **Night Start/End Hour** window into the camera's own **arming
schedule**, so the deterrent fires even while doover is offline; if not, the app falls back to arming and
disarming the linkage itself at the night boundary. The app also pulses the alarm-output relay and sends
a notification.

| Setting | Description | Default |
|---------|-------------|---------|
| **Object Detection** | Which classified events raise notifications (Person, Vehicle) | `Person` |
| **Detection Sensitivity** | On-camera intrusion detection sensitivity (0–100) | `50` |
| **Intruder Alarm › Enabled** | At night, trigger the flash+siren active response + pulse the relay + notify on a classified detection | `false` |
| **Intruder Alarm › Flash Light** | Flash the camera's built-in light on a smart event at night | `true` |
| **Intruder Alarm › Audible Alarm** | Sound the built-in siren on a smart event at night (with the flash light, uses the combined flash+siren active response) | `true` |
| **Intruder Alarm › Night Start / End Hour** | Hour window (site-local) the alarm is armed | `18` / `6` |
| **Intruder Alarm › Event Video Clips** | Upload video of the event instead of a single still | `false` |
| **Intruder Alarm › Event Clip Interval** | Seconds between clips (SD poll interval, or ffmpeg clip length) | `5` |
| **Intruder Alarm › Event Clip Cooldown** | Stop capturing clips this long after the last detection | `15` |

**Event video clips** upload video for the duration of an intruder event. At startup the app probes the
camera's storage (ISAPI `ContentMgmt/Storage`) and picks the best capture mode automatically:

| Camera storage | Mode | How it works |
|---|---|---|
| microSD fitted + formatted | **`sd`** | Adds a `record` linkage so the **camera** records the event to its card; the app fetches finalised segments over `ContentMgmt` and uploads each once. **No ffmpeg** (works on `slim`), and the camera records even if doover is offline. |
| No/unformatted card, ffmpeg present | **`ffmpeg`** | The app records the RTSP stream itself in `Event Clip Interval`-second clips. Needs the **`full`** image; nothing is recorded while doover is down. |
| No card, no ffmpeg (`slim`) | *off* | Falls back to the single-snapshot behaviour. |

The chosen mode is logged at startup. In `sd` mode expect the first clip to lag the detection — the
camera only indexes a segment once it has finished writing it, and segment length is set by the camera's
own recording config, not by `Event Clip Interval`.

> Note: face access-control is not supported by this model (no face engine); ANPR/plates stay with the
> `/P` DeepinView camera. PPE/hard-hat still needs server-side inference.

<br/>

### Camera events (for automations)

Every meaningful detection is published to the **`camera_event`** channel as a structured message, so
doover automations can trigger off it (publish onward, etc.) independently of user notifications:

```json
{"kind": "intruder", "app_key": "...", "display_name": "...", "timestamp": "...", "target": "person", "label": "a person"}
```

`kind` is one of `intruder` (night alarm), `person`, `vehicle`, or `anpr` (which also carries `plate`,
`vehicle_type` and `confidence`).

<br/>

## Integrations

This app works seamlessly with:

- **Platform Interface**: Core Doover platform component
- **RTSP to Web App**: Required for live HLS streaming

<br/>

## Need Help?

- Email: support@doover.com
- [Community Forum](https://doover.com/community)
- [Full Documentation](https://docs.doover.com)
- [Developer Documentation](https://github.com/getdoover/cameras/blob/main/DEVELOPMENT.md)

<br/>

## Version History

### v1.0.0 (Current)
- Initial release

<br/>

## License

This app is licensed under the [Apache License 2.0](https://github.com/getdoover/cameras/blob/main/LICENSE).
