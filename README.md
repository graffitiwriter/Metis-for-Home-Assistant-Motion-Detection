# Metis + Home Assistant CCTV Detection

Uses an [Axelera AI Metis](https://store.axelera.ai) device to run YOLO object detection on RTSP camera streams, with polygon zone filtering and webhook integration into [Home Assistant](https://www.home-assistant.io/) for notifications and snapshots.

The Metis hardware handles all the AI inference. Home Assistant handles notifications, snapshots, and automations. The two talk via webhooks over the local network.

**Full write-up with step-by-step instructions:**
[Home Assistant CCTV Object and Motion Detection Using Metis](https://community.axelera.ai/the-axelera-forum-52/home-assistant-cctv-object-and-motion-detection-using-metis-1226)

## What it does

- Runs YOLO26s on the Metis AIPU across multiple RTSP cameras simultaneously
- Filters detections through polygon zones (only alert when something crosses your property line, ignore the pavement)
- Sends webhook events to Home Assistant when a detection matches
- HA grabs a high-res snapshot from the camera and sends a notification to your phone
- Parked car detection in a secondary zone (car must be stationary for a configurable number of seconds before triggering)
- Automatic RTSP reconnection if a camera stream drops
- Heartbeat webhook so HA can monitor whether the detector is running
- Runs headless, auto-starts on boot, with a watchdog that recovers from hardware hangs

## What you need

- A Metis device (Compute Board, M.2, or PCIe card)
- [Voyager SDK](https://github.com/axelera-ai-hub/voyager-sdk) 1.5+ installed and working
- Home Assistant with the mobile app configured for notifications
- RTSP cameras on your local network
- SSH access to your Metis device

## Files

| File | What it is |
|------|-----------|
| `metis_ha_detector.py` | The detection script. Runs inside the Voyager SDK environment on the Metis device. |
| `config.json.example` | Example configuration. Copy to `config.json` and fill in your details. |
| `HA Configuration.yaml for Metis` | Shell commands to add to your Home Assistant `configuration.yaml` for high-res snapshots. |
| `HA-Automations-YAML` | Home Assistant webhook automations that receive detection events and send notifications. |

## Setup

### 1. Configure Home Assistant

Add the shell commands from `HA Configuration.yaml for Metis` to your Home Assistant `configuration.yaml`. These let HA grab high-res snapshots from your cameras via ffmpeg.

Create the webhook automations from `HA-Automations-YAML`. Each camera gets its own automation triggered by a webhook ID. The automations wait briefly (so the person is properly in frame), grab a snapshot, then send a notification.

Create toggle helpers in HA (Settings > Helpers > Toggle) for each camera/user combination so you can enable or disable notifications without restarting anything.

### 2. Configure the detection script

Copy `config.json.example` to `config.json` and update:

- **home_assistant.ip**: Your Home Assistant IP address
- **cameras**: Your RTSP URLs with credentials
- **zones**: Your zone polygon coordinates (see below)
- **alerts**: Cooldown timing, parking duration threshold

### 3. Set up your zones

Zones are polygons defined as lists of [x, y] pixel coordinates for a 1920x1080 frame.

To measure your zones:
1. Take a screenshot from your camera (open the RTSP stream in VLC and snapshot it)
2. Open it in an image editor
3. Note the pixel coordinates of your property boundary

The config has two zone types:
- **front_red**: Your property. Any detection here triggers an alert.
- **front_blue**: A road strip. Only triggers for cars that stay stationary (parked).

The back camera has no zones. It alerts on any detection, controlled by notification toggles in HA.

### 4. Download the precompiled model

Inside the Voyager SDK environment:

```bash
axdownloadmodel yolo26s-coco-onnx
```

### 5. Run it

```bash
python3 metis_ha_detector.py
```

Walk in front of a camera to confirm detections appear in the terminal and notifications arrive on your phone.

### 6. Make it permanent (Metis Compute Board)

On the MCB host (not inside Docker), create a startup script and systemd service so the detector starts automatically on boot. A watchdog timer checks every 2 minutes and recovers if the detector crashes. See the [full write-up](https://community.axelera.ai/the-axelera-forum-52/home-assistant-cctv-object-and-motion-detection-using-metis-1226) for details.

## How it works

1. The Voyager SDK pulls frames from RTSP cameras via GStreamer
2. YOLO26s runs on the Metis AIPU, returning bounding boxes, class labels, and confidence scores
3. The script checks whether any part of each bounding box overlaps a configured zone polygon (using OpenCV's `pointPolygonTest`)
4. If a detection passes the zone check and the cooldown has expired for that camera/zone/class combination, a webhook is POSTed to Home Assistant
5. HA waits briefly, grabs a high-res snapshot via ffmpeg, and sends a notification with the image

## Performance

On a Metis Compute Board with two 1080p RTSP cameras:

- ~30 FPS combined across both streams
- ~196ms average latency
- Metis temps: 31-32C
- Detection to phone notification: 2-3 seconds

## Configuration reference

All settings live in `config.json`. The script falls back to sensible defaults if any are missing.

| Setting | Default | What it does |
|---------|---------|-------------|
| `alerts.cooldown_seconds` | 15.0 | Minimum seconds between alerts for the same camera/zone/class |
| `alerts.parking_duration_seconds` | 4.0 | How long a car must be stationary in the blue zone before alerting |
| `alerts.parking_movement_threshold` | 30.0 | Pixel distance a car can jitter and still count as stationary |
| `model` | yolo26s-coco-onnx | Voyager SDK model name |
| `aipu_cores` | 4 | Number of Metis AIPU cores to use |
| `rtsp_reconnect_delay` | 5 | Seconds to wait before reconnecting after a stream failure |
| `rtsp_max_reconnects` | 50 | Maximum reconnection attempts before the script exits |
| `heartbeat_interval` | 60 | Seconds between heartbeat webhooks to HA |

## Contributing

If you build something similar or improve on this, I would love to see it. Fork away, open issues, or come share on the [Axelera AI community](https://community.axelera.ai).
