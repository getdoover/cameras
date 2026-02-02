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
| **Camera Type** | Type of camera (Dahua PTZ, Dahua Fixed, Dahua Generic, UniFi Generic, Generic IP) | `Dahua (Generic)` |
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
