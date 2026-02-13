# Metis-for-Home-Assistant-Motion-Detection
As an experiment, I set up an Axelera AI Metis Compute Board to detect specific kinds of motion on my CCTV cameras, and send those detections to Home Assistant to trigger audomations and notifications.

# Metis Home Assistant Integration

Real-time object and motion detection with Axelera Metis hardware, triggering Home Assistant automations and notifications via webhooks. Detects people, vehicles, and animals in user-defined zones and sends instant notifications with snapshots to your phone.

## What This Does

- Monitors RTSP camera streams using Axelera Metis AIPU
- Detects objects in customisable zones (e.g., driveway vs road)
- Sends webhooks to Home Assistant when objects enter zones
- Home Assistant captures snapshots and sends phone notifications
- Runs completely headless (no monitor required)
- Auto-starts on boot and recovers from crashes

## Prerequisites

- Metis Compute Board (or Metis M.2/PCIe card) with Voyager SDK 1.5+ installed
- Home Assistant with mobile app configured
- RTSP cameras accessible on local network
- Basic familiarity with SSH and Python

## Quick Start

### 1. Home Assistant Setup

Create input_boolean helpers for notification toggles:
- Settings > Devices & Services > Helpers > Create Helper > Toggle
- Create one per user per camera (e.g., `user1_front_cam_notifications`)

Add shell commands to `configuration.yaml`:
- Copy contents from `configuration_snippet.yaml`
- Update RTSP URLs with your camera credentials
- Developer Tools > YAML > Restart > Shell Commands

Import automations:
- Settings > Automations & Scenes > ⋮ > Import
- Import `home_assistant_automations.yaml`
- Edit and replace all REPLACE: placeholders

### 2. Metis Setup

SSH into your Metis device and enter the Voyager SDK environment:

```bash
ssh root@YOUR_MCB_IP
cd /home/antelao  # Or your working directory
docker exec -it Voyager-SDK /bin/bash
cd /home/ubuntu/voyager-sdk
source venv/bin/activate
```

Download and configure the detection script:

```bash
wget https://raw.githubusercontent.com/YOUR_REPO/metis_ha_detector.py
nano metis_ha_detector.py
```

Update these values:
- `HA_IP` - Your Home Assistant IP address
- `FRONT_CAMERA` / `BACK_CAMERA` - Your RTSP URLs with credentials
- `FRONT_RED_ZONE` / `FRONT_BLUE_ZONE` - Zone coordinates for your camera angle

### 3. Test Run

```bash
python3 metis_ha_detector.py
```

Walk in front of cameras to test. You should see:
- Detection alerts printed to console
- Home Assistant notifications on your phone
- Snapshots saved to /config/www/snapshots/

Press Ctrl+C to stop.

### 4. Make It Permanent (Auto-start on Boot)

Exit Docker container and create systemd service:

```bash
exit  # Exit Docker container

cat > /etc/systemd/system/metis-ha-detector.service << 'EOF'
[Unit]
Description=Metis Home Assistant Detector
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
User=root
WorkingDirectory=/home/antelao
ExecStart=/usr/bin/docker exec -i Voyager-SDK /bin/bash -c "cd /home/ubuntu/voyager-sdk && source venv/bin/activate && AXELERA_LOW_LATENCY=1 AXELERA_STREAM_QUEUE_SIZE=1 python3 -u metis_ha_detector.py"
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
```

Make Docker container auto-start:

```bash
docker update --restart=unless-stopped Voyager-SDK
```

Enable and start the service:

```bash
systemctl daemon-reload
systemctl enable metis-ha-detector.service
systemctl start metis-ha-detector.service
```

Check it's running:

```bash
systemctl status metis-ha-detector.service
```

## Zone Configuration

Zones are defined as polygons using pixel coordinates. For a 1920x1080 camera:
- (0, 0) is top-left corner
- (1920, 1080) is bottom-right corner

To measure your zones:
1. Take a screenshot from your camera
2. Open in image editor (Photoshop, GIMP, etc)
3. Note pixel coordinates of zone boundaries
4. Update `FRONT_RED_ZONE` and `FRONT_BLUE_ZONE` in the script

Example zones:
- RED ZONE: Driveway/garden area - immediate alerts
- BLUE ZONE: Road/parking area - stationary vehicle detection only

## Adding More Cameras

To add a third camera:

1. Add RTSP URL to sources list:
```python
stream = create_inference_stream(
    sources=[FRONT_CAMERA, BACK_CAMERA, THIRD_CAMERA],  # Add here
    ...
)
```

2. Create new webhook in Home Assistant (copy existing automation, change webhook_id)

3. Add processing function:
```python
def process_third_camera(detections):
    # Your detection logic here
    ...

# In main() function:
elif frame_result.stream_id == 2:  # Third camera
    process_third_camera(frame_result.detections)
```

## Troubleshooting

**Script crashes with AttributeError**
- Check Voyager SDK version (requires 1.5+)
- Verify you're using correct attributes (`.score` not `.confidence`, `.box` not `.x1/.y1`)

**No notifications**
- Test webhook with curl: `curl -X POST -H "Content-Type: application/json" -d '{"object":"person","confidence":0.95}' http://YOUR_HA_IP:8123/api/webhook/metis_front_door`
- Check input_boolean toggles are ON in Home Assistant
- Verify MCB can reach Home Assistant IP

**Wrong zones triggering**
- Add debug print statements to see detection coordinates
- Adjust zone boundaries based on actual detections
- Test with `visible=True` in display.App() to see visual output

**Service won't start**
- Check Docker container is running: `docker ps | grep Voyager`
- View service logs: `journalctl -u metis-ha-detector -n 50`
- Verify script path in ExecStart matches your setup

## Useful Commands

```bash
# View live detection logs
journalctl -u metis-ha-detector -f

# Restart the service
systemctl restart metis-ha-detector.service

# Stop the service
systemctl stop metis-ha-detector.service

# Check service status
systemctl status metis-ha-detector.service

# Enter Docker container manually
docker exec -it Voyager-SDK /bin/bash
cd /home/ubuntu/voyager-sdk
source venv/bin/activate
```

## Performance

With this setup on Metis Compute Board (single Metis AIPU):
- 2 cameras @ 1080p
- YOLOv26s model
- ~30 FPS combined throughput
- ~196ms latency
- Low CPU usage (AI offloaded to Metis AIPU)

## Community & Support

For questions, issues, or to share your setup:
- Axelera AI Community: https://community.axelera.ai
- Voyager SDK Documentation: https://github.com/axelera-ai-hub/voyager-sdk

## License

Based on Axelera AI Voyager SDK examples. Check Voyager SDK license for usage terms.
