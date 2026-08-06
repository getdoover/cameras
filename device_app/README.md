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
| **Always On** | Hold power on permanently; only power cycle an unresponsive camera | `false` |
| **Off After** | Seconds after which the camera will be powered off (ignored when *Always On*) | `900` |
| **Wake Delay** | Seconds for camera to boot before requesting a snapshot | `5` |
| **Power Cycle After Failed Pings** | Consecutive failed pings before a power cycle (*Always On* only) | `3` |
| **Power Cycle Off Duration** | Seconds to hold power off during a power cycle | `15` |
| **Live View Enabled** | Whether remote component is enabled for this camera | `true` |
| **Live View URL** | URL for live view component | `https://getdoover.github.io/cameras/HLSLiveView.js` |
| **Snapshot Enabled** | Whether periodic snapshots are enabled | `true` |
| **Snapshot Period** | Seconds between snapshots | `14400` |
| **Snapshot Mode** | Video or Image format | `Image` |
| **Object Detection** | Objects the *camera* is told to detect (Dahua/Bosch only; Hikvision always does both) | `None` |
| **Control Enabled** | Allow control (movement) of PTZ cameras | `true` |

<br/>

### Camera power: on demand, or always on with a watchdog

By default the power pin is **on demand**: raised when something needs the camera (a
snapshot, a live view, an event), then dropped once **Off After** lapses. Good for a
solar site where the camera is the biggest load and is wanted a few times a day.

**Always On** inverts that. The pin is held up for the life of the app and **Off After**
no longer applies, so the camera is never interrupted mid-event and never has to boot
before a snapshot. In exchange the app runs a watchdog: it pings the camera every 30s and
**power cycles it only when it stops answering** — which is the one repair that works on a
camera that has locked up, and one no amount of retrying over the network can perform.

- **Failures must be consecutive.** A single dropped ping means nothing — the camera is
  busy encoding, the network blipped — and cutting power on one is worse than having no
  watchdog at all, because the cycle itself costs a boot during which the camera really is
  unreachable. **Power Cycle After Failed Pings** is that threshold.
- **A snapshot arriving mid-cycle cannot abort it.** Raising the pin during the off period
  would cancel the reboot being performed *because* the camera stopped answering, leaving
  it locked up. Requests during a cycle are skipped, and the pin is restored by the cycle
  itself.
- **After power returns, the watchdog waits out Wake Delay** before counting failures
  again. Without that it would read the booting camera's silence as fresh failures and
  cycle it forever.
- **A deliberate release still works.** The periodic expiry check never drops power in this
  mode, but the device shutting down does — and that latches, so the always-on loop can't
  power the camera back up behind the shutdown's back.

> On a **shared power pin** (two camera apps, one circuit), the app with *Always On* keeps
> the circuit up, which is what you'd want. But note that its watchdog cuts power to the
> *circuit*, so the other camera reboots with it — unavoidable when they share a supply.

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
**both human and vehicle** at the configured **Detection Sensitivity**, a **0-second** dwell time, and
re-alarm-on-a-static-target **on** (see below — the night alarm depends on it). It then **disables `regionEntrance`, `regionExiting` and
`LineDetection`** (their polygons are preserved) and ignores their events: intrusion already reports what
they would, and every rule left on is a second event — and so a second snapshot, upload and inference
run — for the same person walking through.

Events carry the classification per-event and map onto the `person` / `vehicle` callbacks, and **every
classified detection notifies** — there is no per-camera switch for it. (**Object Detection** does nothing
on this engine: the intrusion rule always classifies both human and vehicle. It still selects the on-camera
targets on Dahua and Bosch.)

The night intruder alarm uses the camera's built-in **active response** — on `/SRB` models the flash light
(`whiteLight`) and audible siren (`audio`) are linked to the intrusion event, and the app writes the
**Night Start/End Hour** window into the camera's own **arming schedule**. Both then fire on-camera,
**even while doover is offline**. If the firmware won't accept a schedule the app falls back to arming and
disarming the linkage itself at the night boundary. The app also pulses the alarm-output relay and sends
a notification.

> The camera's arming schedule runs on **the camera's own clock**, which on these models is `manual` and
> resets to 2019 on a power cut (flat RTC), leaving it to arm at the wrong hours forever. The app
> therefore syncs the camera's clock (and timezone) from the device at setup and re-checks it each loop,
> correcting drift over **5 seconds** — `MAX_CLOCK_DRIFT_SECS`.
>
> Seconds, not minutes, because of event clips rather than the schedule. The clip search asks the camera
> for a ~25-second window around an event, and the camera both stamps segments and answers searches on its
> own clock — so a sub-minute offset means every search misses and the app reports "no recording" while the
> footage sits on the card the whole time. Measured on a DS-2CD2387G3 running 44s behind its doovit: not
> one clip was ever found. The search also **shifts the window by the measured offset** and widens it by
> `EVENT_CLIP_SEARCH_MARGIN`, then picks the segment overlapping the event most — so residual drift between
> clock checks can't cost a clip either.

| Setting | Description | Default |
|---------|-------------|---------|
| **Object Detection** | Not used by this engine — the camera always classifies human *and* vehicle | `Person` |
| **Detection Sensitivity** | On-camera intrusion detection sensitivity (0–100) | `50` |
| **Intruder Alarm › Enabled** | At night, trigger the flash+siren active response + pulse the relay + notify on a classified detection | `false` |
| **Intruder Alarm › Flash Light** | Flash the camera's built-in light on a smart event at night | `true` |
| **Intruder Alarm › Audible Alarm** | Sound the built-in siren on a smart event at night (with the flash light, uses the combined flash+siren active response) | `true` |
| **Intruder Alarm › Night Start / End Hour** | Hour window (site-local) the alarm is armed | `18` / `6` |
| **Intruder Alarm › Event Video** | Upload a video of the event instead of a single still | `false` |
| **Intruder Alarm › Event Video Cooldown** | Stop recording this long after the last detection | `15` |
| **Intruder Alarm › Event Video Max Length** | Hard cap on a single event video (seconds) | `120` |

**Event video** captures an intruder event as **one continuous video**, not a series of clips. Recording
starts on the first detection and keeps running for as long as the intruder keeps triggering the camera —
each detection pushes the cooldown out — then the whole thing uploads as a single file. **Event Video Max
Length** is the backstop so one persistent event can't record forever.

At startup the app probes the camera's storage (ISAPI `ContentMgmt/Storage`) and picks a capture mode:

| Camera storage | Mode | How it works |
|---|---|---|
| microSD fitted + formatted | **`sd`** | Adds a `record` linkage so the **camera** records the event to its card; once the event ends the app pulls that span back in one download. **No ffmpeg** (works on `slim`), and the camera records even if doover is offline. |
| No/unformatted card, ffmpeg present | **`ffmpeg`** | The app records the RTSP stream itself for the length of the event. Needs the **`full`** image (the deployment template selects it automatically); nothing is recorded while doover is down. |
| No card, no ffmpeg (`slim`) | *off* | Falls back to the single-snapshot behaviour. |

The chosen mode is logged at startup. A card that is present but unformatted/erroring counts as **no**
storage — the `record` linkage would arm and silently write nothing.

> Note: face access-control is not supported by this model (no face engine); ANPR/plates stay with the
> `/P` DeepinView camera. PPE/hard-hat still needs server-side inference.

<br/>

### Snapshot & video messages

Every snapshot/video is published to the app's own channel with a **thumbnail** attached alongside each
full-size file, plus a payload describing them — so a gallery or preview timeline doesn't have to
download the full image, or hardcode filenames.

One message carries **every view captured in one go**: a PTZ camera contributes one per preset, a thermal
camera a visible and a thermal view. So `media` is always a list, even when there's only one:

```json
{"reason": "schedule", "night": true, "media": [
  {"name": "Preset1", "file": "Preset1.jpg", "thumbnail": "Preset1-thumbnail.jpg"},
  {"name": "Preset2", "file": "Preset2.jpg", "thumbnail": "Preset2-thumbnail.jpg"}
]}
```

| Field | Meaning |
|---|---|
| `media[].name` | The view — a preset name, or `snapshot` / `visible` / `thermal` / `event` |
| `media[].file` | Filename of the full-size attachment |
| `media[].thumbnail` | Filename of its preview. Absent if one couldn't be made, or wouldn't represent the view (the thermal channel gets none — a visible-stream preview would show a different image) |
| `reason` | Why it was captured: `schedule`, `manual`, `intruder`, `person`, `vehicle`, `anpr` — matches the `camera_event` `kind` |
| `night` | `true`/`false` — **only present when the camera states it outright** (see below) |
| `detections` | Where the camera localised the targets that triggered the capture. **Only present when the event carried boxes** (see below) |
| `media[].name` `event-frame` | The camera's **own** JPEG of the event, uploaded beside the fetched snapshot (see below) |

Thumbnails sit beside their media (`Preset1.jpg` / `Preset1-thumbnail.jpg`) and are captured **at the same
moment as the media** — on a PTZ camera that has to happen while it's still pointed at the preset.

On Hikvision the thumbnail is free: the camera's **sub-stream** picture is already thumbnail-sized
(640×360, ~18KB vs 1920×1080/~117KB), so it's one extra HTTP GET with **no ffmpeg** — thumbnails work on
the `slim` image. Other camera types scale a frame with ffmpeg, and simply get no thumbnail if ffmpeg
isn't present. For an intruder event the thumbnail is grabbed **at the trigger**, while the video is
still recording, so it shows the intruder rather than an empty scene.

**`night`** comes from the camera's IR-cut filter state (`ircutFilter`), which is ground truth — a
grey/foggy *daylight* scene looks washed out too, so the image alone can mislead. The field is omitted
when the camera won't commit (mode `auto`/`schedule` describe how it decides, not what it decided, and
non-Hikvision cameras aren't asked). When it's absent, work it out from the thumbnail client-side:

> A night frame is **not** reliably a dark one — with the IR illuminator on, one measured *brighter*
> (avg luma 136) than a full-colour test pattern (125). What gives it away is that it carries no colour:
> average saturation sits at ~0. Key off **saturation**, not brightness.

**`detections`** is what the camera itself localised. AcuSense and DeepinView put a `<TargetRect>` beside
the `<detectionTarget>` in their smart events, so a person/vehicle/intruder snapshot can say *where* in
the frame the target was; an ANPR event's plate rect comes through the same way, labelled `plate`:

```json
{"reason": "person", "object_detection": true,
 "detections": [{"target": "person", "box": [0.31, 0.42, 0.49, 0.78]},
                {"target": "vehicle", "box": [0.05, 0.50, 0.28, 0.71]}],
 "media": [{"name": "snapshot", "file": "snapshot.jpg", "thumbnail": "snapshot-thumbnail.jpg"}]}
```

| | |
|---|---|
| `box` | `[x1, y1, x2, y2]` as **fractions of the frame, origin top-left**. Same ordering as the Object Detection app's own `findings` boxes, but normalised rather than pixels, so it stays valid whatever resolution the snapshot came back at — and the same space as detection-zone points |
| `target` | `person`, `vehicle`, `animal`, `other` or `plate`. **Omitted** when the camera reported a box without classifying it. Vendor tokens are normalised (Hikvision's `human` → `person`); an unknown token from newer firmware is passed through rather than dropped |

It's **advisory**: the whole key is absent when the event carried no boxes, so a consumer can tell "no
boxes this time" from "this camera doesn't report boxes", and analysis is free to ignore it and run over
the full frame. What it's *for* is cropping a plate read to the region the camera already found, and
having something concrete to deduplicate consecutive events on.

> Units differ by firmware — some report fractions of the frame, others the normalized `0–1000` screen the
> zone endpoints use — so anything over `1` is scaled down, which lands both in the same space. The **y axis
> is top-left origin** and passed through unflipped: confirmed against a real event frame, where a box at
> `y=0.076..0.348` sat around a person in the upper third of the picture. (Zone *polygons* are y-up and
> still get flipped — that asymmetry is the camera's, not ours.)

<br/>

### Motion snapshots (daytime pictures, no alarm)

The intruder alarm owns the **night** window — motion there means sirens, strobes and
event video. During the day none of that is wanted, but the picture often still is:
it's what the **Object Detection** app analyses for hard-hat / high-vis compliance and
number plates.

This section governs those pictures. A classified person/vehicle event captures a still;
these settings let you confine that to an hour window, rate-limit it, and mark the
results for analysis.

| Setting | Description | Default |
|---------|-------------|---------|
| **Only Capture During Hours** | Confine motion snapshots to the window below | `false` |
| **Start Hour / End Hour** | Hour window (0–23, site-local) snapshots are captured in | `6` / `18` |
| **Minimum Seconds Between Snapshots** | Shortest gap between two motion snapshots. `0` captures on every event | `15` |
| **Object Detection** | Offer these snapshots to the Object Detection app | `false` |

**The same rule (intrusion) drives day and night**, so the zone you draw is the zone that
detects, at any hour. Intrusion triggers on a target being *in* the region, which is what
catches someone already in frame, someone appearing inside it, and someone crossing only
the outer margin — none of which a boundary-crossing rule can see. Its dwell time defaults
to **0 seconds** — report it as soon as it's classified — so a target crossing quickly still
fires; the stock rule shipped `5`, which silently missed anyone faster than that. It's
**per-zone and editable** in the zone editor (`threshold_secs`), so a zone that flickers can
be given a second or two without slowing the rest down.

> The app writes that default only when it **creates** the rule. On a camera that already
> has one, the dwell is left exactly as configured — it belongs to whoever drew the zone, and
> re-asserting a default on every app start would silently undo their setting.

The catch intrusion brings is that it re-alarms while a target *stays* in the region, so a
parked car would otherwise keep costing a snapshot, an upload and an inference run. That is
throttled **in the app** — **Minimum Seconds Between Snapshots** — and deliberately not at
the camera:

> **Do not switch `contAlarmForStaticTargetEnabled` off.** That repeat is the only signal
> that an intruder is *still there* — there's no "target still present" event and nothing to
> poll — so the night alarm's "keep going while they're in frame" behaviour is built on it,
> as is the camera's own light/buzzer linkage (`whiteLightDurationTime=0` means "follow the
> event", and with no re-alarms the event is a single instant). Turning it off to stop
> duplicate daytime snapshots looks tempting and quietly caps every night alarm at one
> cooldown. The app asserts it **on** at setup, and re-asserts it after every zone write —
> writing regions rebuilds the rule body and drops it.

> **The camera does not repeat the smart event while a target stays put.** Verified on an
> `iDS-2CD5T87G2/V-XHSY` (V5.9.20) with someone standing in the zone for two minutes:
> `fielddetection` fired `active` **once**, and from then on the camera sent a *different*
> event every 5s (`targetAlarmInterval`) —
> `<eventType>duration</eventType>` with `<relationEvent>fielddetection</relationEvent>` —
> interleaved with `fielddetection`/`inactive`. That heartbeat is the only evidence an
> intruder hasn't left; there is nothing to poll. The app treats it as a **continuation**:
> it extends the strobe, horn and recording, and does nothing else — no snapshot, no
> notification, no `camera_event`, since one arrives every few seconds for the same person.

Only the picture is throttled. The strobe, horn, siren, recording and `camera_event` fire on
every detection. The **notification** is once per intruder event rather than once per
re-alarm, since a target standing in the zone would otherwise generate a message every few
seconds. If the camera's `targetAlarmInterval` is longer than **Event Video Cooldown**, a
motionless intruder ends the alarm early — the app logs a warning naming both numbers at
startup, because that combination looks like the alarm cutting out for no reason.

> **Off by default is the existing behaviour.** With *Only Capture During Hours* off, a
> classified person/vehicle event captures a still whatever the time, exactly as
> before — turning this section on is what narrows it.

The window is **independent of the night window**, not its complement, so a site can
keep pictures to working hours only and leave a gap where nothing is captured. It
accepts a wrapping range (`18` → `6`) the same way the night window does, and
`start == end` means never.

**Only the picture is gated, never the event.** A `camera_event` is still published
outside the window — an automation wants to know a person was seen at 3am even when the
site only keeps daytime images.

> **This window is written into the camera's arming schedule, and that changes how the
> night deterrent is armed.** On this firmware the arming schedule gates the *event*,
> not just its linkages: an hour outside it produces no `fielddetection` at all, so the
> camera classifies nothing and the app hears nothing. A night-only schedule therefore
> made daytime person/vehicle detection **completely blind** — measured on a real
> camera, walking in front of it at 12:47 produced only an unclassified `VMD` event.
>
> So the schedule is the **union** of the night window and this one. The cost is
> that the schedule no longer means "night", so the camera can't gate the flash/siren
> for us: the app arms the deterrent at dusk and disarms it at dawn instead.
> **That loses the offline guarantee at the boundary** — if doover is down when dusk
> passes, the deterrent stays disarmed until it comes back. Leave the motion-snapshot
> window off if native, doover-independent night deterrence matters more than daytime
> detection on that camera.

**Both the schedule and the linkage are re-asserted every 10 minutes**, and immediately
when something changes. They live on the camera, where a web-UI visit, a firmware quirk or
a factory reset can silently change them, and drift is invisible in the worst direction —
the camera stops reporting for part of the day and the app simply never hears anything. So:

- **Coming back from being offline**, the app writes the schedule and applies the correct
  armed state at startup, so a device that missed dusk and returns at midday arms nothing
  and disarms the siren immediately — it doesn't wait for the next boundary.
- **Editing the night or motion-snapshot hours** takes effect on the next main loop, with
  no restart and no waiting out the 10 minutes.
- The schedule is re-asserted **even with the intruder alarm off**. It isn't a deterrent
  setting: on this firmware it decides when the camera detects *at all*, so a site that
  only wants daytime snapshots depends on it just as much as one that wants a siren.

**Object Detection** adds `"object_detection": true|false` to the snapshot message. The
detection app treats that as authoritative in both directions, overriding its own
reason filter — so a camera can be watched for plates without every one of its motion
snapshots being run through the models. It only marks the frame as wanted; no inference
happens in this app.

<br/>

### The camera's own event frame

On a classified detection these cameras **attach a JPEG to the alert** — their own picture of the event,
taken at the instant they classified the target. It arrives as an `image/jpeg` part on the alertStream, on
the connection the app already holds open, and it used to be discarded. It is now uploaded **beside** the
snapshot the app fetches, as a `media` entry named `event-frame`:

```json
{"reason": "vehicle", "object_detection": true,
 "detections": [{"target": "vehicle", "box": [0.55, 0.08, 0.58, 0.43]}],
 "media": [{"name": "snapshot", "file": "snapshot.jpg", "thumbnail": "snapshot-thumbnail.jpg"},
           {"name": "event-frame", "file": "event-frame.jpg"}]}
```

Why both, rather than one or the other:

| | `event-frame` | `snapshot` |
|---|---|---|
| **when** | the instant of classification | ~0.3s later, when the app gets round to asking |
| **resolution** | 1280×720 (AcuSense) / 1920×1080 (DeepinView) | full main-stream, 1920×1080 |
| **overlays** | detection zone + target box **burnt in** | clean; timestamp and camera name only |
| **size** | 199–418KB measured | ~181KB |

For anything moving — a vehicle crossing a gate — the event frame is the one that actually has the target
in it, because it was taken while the target was still in the zone. The fetched snapshot is the clean,
full-resolution one. Neither is a substitute for the other, so both go up.

- **The frame is published even when the snapshot fails.** If the camera has stopped answering HTTP, the
  frame it already handed us is *more* valuable, not less — so a failed ping or a failed capture still
  publishes the event frame on its own.
- **Pairing is positional**: the JPEG is the multipart part after the XML. An alert that advertises a
  picture (`detectionPictureTransType=binary`, `picturesNumber>0`) is held back until it arrives — bounded
  by `EVENT_IMAGE_WAIT_SECS` (3s) and released early if the next alert beats it. Nothing waits
  indefinitely: the alarm path needs events promptly, and a heartbeat (`duration`) or `videoloss` alert
  carries no picture at all.
- **It has no thumbnail.** It sits beside a snapshot that has one, and at a few hundred KB a preview would
  cost more than it saves.

> **Two things to know before pointing the Object Detection app at these.** The overlays are burnt in, so
> the models see a red polygon and a red target box drawn over the scene — turn off the camera's
> rules/target-info display if that matters more than having the aid for human review. And the frame is a
> second image attachment, so the detection app will analyse **both** frames: twice the inference per
> event, with two chances at a plate.

<br/>

### Object detection zones

Zones live entirely on the **`set_detection_zones`** command, over `ui_cmds` — its value in the aggregate
is the current state (just as a switch's value is its current state), and the command writes it. There's
no separate read path. The shape is identical across Hikvision and Dahua; the frontend never sees a vendor
coordinate space.

**Read — the command's value (`$cmds.app().set_detection_zones`):**

```json
{
  "capabilities": {
    "supported": true, "max_zones": 4, "min_points": 3, "max_points": 10,
    "targets": ["person", "vehicle", "animal", "other"],
    "supports_sensitivity": true, "supports_per_zone_targets": true,
    "supports_threshold": true, "threshold_min": 0, "threshold_max": 60,
    "supports_disable": false, "supports_notify": true,
    "kinds": {
      "intrusion":     {"display_name": "Intrusion", "max_zones": 4,
                        "supports_threshold": true, "default_notify": false},
      "excluded_area": {"display_name": "Excluded Area", "max_zones": 4,
                        "supports_threshold": false, "default_notify": true,
                        "edge_warn": 100, "edge_warn_normalized": 0.1},
      "ppe":           {"display_name": "PPE", "max_zones": null,
                        "default_notify": false, "detected_by": "object_detection"},
      "anpr":          {"display_name": "Number Plates", "max_zones": null,
                        "default_notify": false, "detected_by": "object_detection"}
    }
  },
  "zones": [
    {"id": 1, "enabled": true, "points": [[0.1,0.1],[0.9,0.1],[0.9,0.9],[0.1,0.9]],
     "targets": ["person","vehicle"], "sensitivity": 70, "threshold_secs": 0,
     "kind": "intrusion", "notify": false, "name": "Driveway"}
  ]
}
```

**Write — the same command.** Send `{"zones": [...]}`; the reply is the shape above and becomes the
command's new value, so you read it back from where you wrote it. Going through `ui_cmds` means the
commands system records who changed the zones and when. The value is seeded at startup (without an audit
entry — starting up isn't somebody editing zones).

| Field | Meaning |
|---|---|
| `points` | `[x, y]` pairs, **normalised `0.0`–`1.0`, origin top-left, y down** — the same space as an overlay on the video element. Engines convert to native (Hikvision `0–1000`, Dahua `0–8191`) |
| `id` | Zone/rule slot, **numbered per kind**. Hikvision renumbers by position; on Dahua this must match an existing IVS rule |
| `kind` | What the zone is for: `intrusion`, `excluded_area`, `ppe`, `anpr`. Defaults to `intrusion`. See below |
| `notify` | Whether a detection in this zone raises a notification. Defaults per kind (`default_notify` in `capabilities.kinds`) |
| `targets` | Any of `person`, `vehicle`, `animal`, `other` — check `capabilities.targets` for what this camera accepts |
| `enabled` | Only meaningful when `capabilities.supports_disable` |
| `sensitivity` | `0`–`100`. Only when `capabilities.supports_sensitivity` |
| `threshold_secs` | How long a target must stay in the zone before it counts. Only when the *kind* supports it; range is `threshold_min`–`threshold_max` (`0`–`60` on Hikvision). **`0` means report it as soon as it's classified**, and is the default |

#### Zone kinds

Each kind maps to a different detector, and two of them aren't on the camera at all.

| Kind | Detected by | Notified by | Notes |
|---|---|---|---|
| `intrusion` | camera `fielddetection` | this app | Presence in the region. The original behaviour and the default |
| `excluded_area` | camera `regionEntrance` | this app | Crossing **into** somewhere nobody should be. Fires once on entry rather than re-alarming while they stand there. Notifies at `Warn` and publishes an `excluded_area` camera event |
| `ppe` | Object Detection app | Object Detection app | Filters hard-hat/high-vis findings to this polygon |
| `anpr` | Object Detection app | Object Detection app | Filters plate reads to this polygon |

The two camera kinds are written to **separate rules with separate slot budgets**, so excluded areas don't
consume intrusion's four. `ppe`/`anpr` zones are never written to the camera — they ride along on each
snapshot message as `detection_zones` for the Object Detection app to filter on, and are kept in the
`detection_zones` tag because the camera has nowhere to store them.

> [!WARNING]
> **An `excluded_area` must not hug the frame edge.** Region entrance only fires for a target it tracked
> *outside* the zone first, so a zone touching the edge leaves nowhere to be outside and never fires at
> all. Measured on a real camera: a region at `10..990` (native) produced **zero** events where `250..750`
> fired reliably. Warn when any point is within `edge_warn_normalized` of an edge; the app logs a warning
> but does not move what somebody drew.

> [!NOTE]
> An excluded area overlapping an intrusion zone genuinely reports **twice** — the two rules answer
> different questions. The app collapses that, preferring the excluded area as the more specific claim.

**Notification behaviour with no zones is unchanged from before zones had kinds**: a camera with no zones
notifies on every classified person/vehicle, exactly as it always did. Zones only ever narrow that. A newly
drawn `intrusion` zone defaults to `notify: false`, so drawing one to get pictures doesn't sign you up for
alerts; an `excluded_area` defaults to `notify: true`. The night intruder alarm is **not** gated on
`notify` — it has already sounded a siren by then, and an alarm nobody is told about is worse than useless.

**Always drive the editor off `capabilities`** rather than assuming:

- `kinds` — which kinds this camera offers, and **per-kind** limits. `max_zones` differs per kind and
  `excluded_area` has no dwell control at all (region entrance has no `timeThreshold`, so a dwell slider
  there is one the camera throws away). `max_zones: null` means no camera limit — nothing has to store it
- `max_zones` / `min_points` / `max_points` — Hikvision genuinely rejects a 2-point or 11-point zone
- `supports_disable: false` on Hikvision — it accepts a region `enabled` change, replies OK, and **ignores
  it**. Offer *delete*, not a toggle
- `threshold_min` / `threshold_max` — a dwell slider can't guess this the way it can assume `0`–`100` for
  sensitivity, and the useful end of the range is the bottom: **`0`** catches a vehicle crossing at speed,
  while even `1`–`2` starts missing them. The camera's own default is `0`
- `supported: false` — hide the editor entirely

Out-of-range points are clamped rather than rejected, so a drag past the frame edge won't fail the write.
**The response is a read-back from the camera, not an echo** — this firmware will answer OK and silently
drop a field, so trust what comes back, not what you sent. On failure the reply carries an `error` string
alongside the unchanged zones.

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
