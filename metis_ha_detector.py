#!/usr/bin/env python3
"""
Metis Home Assistant Detector v2
Monitors RTSP cameras via Voyager SDK, checks detections against
polygon zones, and triggers Home Assistant automations via webhooks.

Requires:
  - Axelera Metis hardware (Compute Board, M.2, or PCIe)
  - Voyager SDK 1.5+ installed and working
  - Home Assistant with webhook automations configured
  - RTSP cameras on the local network

Configuration is loaded from config.json (see config.json.example).

Full project write-up:
  https://community.axelera.ai/the-axelera-forum-52/home-assistant-cctv-object-and-motion-detection-using-metis-1226
"""

import json
import logging
import os
import signal
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import cv2
import numpy as np
import requests

from axelera.app import display
from axelera.app.stream import create_inference_stream


# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "home_assistant": {
        "ip": "YOUR_HA_IP",
        "port": 8123,
        "webhooks": {"front": "metis_front_door", "back": "metis_back_door"}
    },
    "cameras": {
        "front": {
            "url": "rtsp://USER:PASS@CAMERA_IP:554/stream",
            "detect_classes": ["person", "cat", "dog", "car"],
            "confidence": 0.5
        },
        "back": {
            "url": "rtsp://USER:PASS@CAMERA_IP:554/stream",
            "detect_classes": ["person", "cat", "dog"],
            "confidence": 0.5
        }
    },
    "zones": {
        "front_red": [[0, 486], [385, 447], [1660, 447],
                      [1920, 465], [1920, 1080], [0, 1080]],
        "front_blue": [[282, 371], [1920, 371], [1920, 421], [282, 421]]
    },
    "alerts": {
        "cooldown_seconds": 15.0,
        "parking_duration_seconds": 4.0,
        "parking_movement_threshold": 30.0
    },
    "model": "yolo26s-coco-onnx",
    "aipu_cores": 4,
    "rtsp_reconnect_delay": 5,
    "rtsp_max_reconnects": 50,
    "heartbeat_interval": 60,
    "log_file": "/home/ubuntu/shared/detector.log",
    "log_max_bytes": 5242880,
    "log_backup_count": 3
}


def load_config(path="config.json"):
    """Load config from JSON, falling back to defaults."""
    if Path(path).exists():
        with open(path) as f:
            user = json.load(f)
        merged = {**DEFAULT_CONFIG, **user}
        for key in ("home_assistant", "cameras", "zones", "alerts"):
            if key in user and key in DEFAULT_CONFIG:
                merged[key] = {**DEFAULT_CONFIG[key], **user[key]}
        return merged
    return DEFAULT_CONFIG.copy()


# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------

def setup_logging(cfg):
    log = logging.getLogger("metis")
    log.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    log.addHandler(ch)
    try:
        fh = RotatingFileHandler(
            cfg.get("log_file", "/home/ubuntu/shared/detector.log"),
            maxBytes=cfg.get("log_max_bytes", 5 * 1024 * 1024),
            backupCount=cfg.get("log_backup_count", 3)
        )
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except (OSError, PermissionError) as e:
        log.warning(f"Can't open log file: {e}")
    return log


# ---------------------------------------------------------------------------
# ZONE CHECKER
# ---------------------------------------------------------------------------

class ZoneChecker:
    """Polygon zone detection using OpenCV pointPolygonTest."""

    def __init__(self, zones_cfg):
        self.zones = {}
        for name, coords in zones_cfg.items():
            self.zones[name] = np.array(coords, np.int32)

    def overlaps(self, bbox, zone_name):
        """True if any corner or centre of bbox falls inside the zone."""
        if zone_name not in self.zones:
            return False
        zone = self.zones[zone_name]
        x1, y1, x2, y2 = bbox
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        for px, py in [(x1, y1), (x2, y1), (x1, y2), (x2, y2), (cx, cy)]:
            if cv2.pointPolygonTest(zone, (float(px), float(py)), False) >= 0:
                return True
        return False


# ---------------------------------------------------------------------------
# ALERT MANAGER
# ---------------------------------------------------------------------------

class AlertManager:
    """Temporal cooldown per (camera, zone, class).

    Tracks when we last alerted for each combination and suppresses
    until the cooldown expires. Simpler and more predictable than
    grid-cell based deduplication.
    """

    def __init__(self, cfg, log):
        self.cooldown = cfg["alerts"]["cooldown_seconds"]
        self.park_duration = cfg["alerts"]["parking_duration_seconds"]
        self.park_threshold = cfg["alerts"]["parking_movement_threshold"]
        self.log = log

        self.last_alert = {}
        self.parked = {}

        ha = cfg["home_assistant"]
        base = f"http://{ha['ip']}:{ha['port']}/api/webhook"
        self.webhooks = {
            name: f"{base}/{hook}"
            for name, hook in ha["webhooks"].items()
        }

    def should_alert(self, camera, zone, class_name):
        """True if cooldown has expired for this combination."""
        key = (camera, zone, class_name)
        now = time.time()
        if now - self.last_alert.get(key, 0) >= self.cooldown:
            self.last_alert[key] = now
            return True
        return False

    def check_parked(self, camera, bbox, confidence):
        """Track a car's position. Returns True if stationary long enough."""
        now = time.time()
        cx = (bbox[0] + bbox[2]) / 2
        cy = (bbox[1] + bbox[3]) / 2
        key = f"{camera}_{int(cx/50)}_{int(cy/50)}"

        if key not in self.parked:
            self.parked[key] = {
                "first_seen": now, "last_pos": (cx, cy), "last_update": now
            }
            return False

        state = self.parked[key]
        old_cx, old_cy = state["last_pos"]
        dist = ((cx - old_cx)**2 + (cy - old_cy)**2)**0.5

        if dist > self.park_threshold:
            self.parked[key] = {
                "first_seen": now, "last_pos": (cx, cy), "last_update": now
            }
            return False

        state["last_pos"] = (cx, cy)
        state["last_update"] = now
        return (now - state["first_seen"]) >= self.park_duration

    def cleanup_parked(self, timeout=5.0):
        """Drop entries for cars no longer in frame."""
        now = time.time()
        stale = [k for k, v in self.parked.items()
                 if now - v["last_update"] > timeout]
        for k in stale:
            del self.parked[k]

    def send(self, camera, class_name, confidence):
        """POST detection event to Home Assistant."""
        url = self.webhooks.get(camera)
        if not url:
            self.log.warning(f"No webhook for camera: {camera}")
            return
        payload = {"object": class_name, "confidence": round(float(confidence), 2)}
        try:
            resp = requests.post(url, json=payload, timeout=2)
            if resp.status_code == 200:
                self.log.info(f"ALERT: {class_name} on {camera} ({confidence:.2f})")
            else:
                self.log.warning(
                    f"Webhook {resp.status_code}: {class_name} on {camera}")
        except requests.exceptions.RequestException as e:
            self.log.error(f"Webhook error ({camera}): {e}")


# ---------------------------------------------------------------------------
# HEARTBEAT
# ---------------------------------------------------------------------------

class Heartbeat:
    """Periodic ping so HA knows the detector is alive."""

    def __init__(self, cfg, log):
        ha = cfg["home_assistant"]
        self.url = f"http://{ha['ip']}:{ha['port']}/api/webhook/metis_detector_heartbeat"
        self.interval = cfg.get("heartbeat_interval", 60)
        self.last = 0
        self.log = log

    def tick(self):
        now = time.time()
        if now - self.last < self.interval:
            return
        self.last = now
        try:
            requests.post(self.url, json={"status": "online"}, timeout=2)
        except requests.exceptions.RequestException:
            pass


# ---------------------------------------------------------------------------
# FRAME PROCESSING
# ---------------------------------------------------------------------------

def process_front(detections, zones, alerts, valid_classes, min_conf, log):
    """Front camera: RED zone (all classes) + BLUE zone (parked cars only)."""
    for det in detections:
        cls = det.label.name
        conf = det.score
        if cls not in valid_classes or conf < min_conf:
            continue

        bbox = [float(det.box[0]), float(det.box[1]),
                float(det.box[2]), float(det.box[3])]

        if zones.overlaps(bbox, "front_red"):
            if alerts.should_alert("front", "red", cls):
                alerts.send("front", cls, conf)
            continue

        if cls == "car" and zones.overlaps(bbox, "front_blue"):
            if alerts.check_parked("front_blue", bbox, conf):
                if alerts.should_alert("front", "blue", "car_parked"):
                    alerts.send("front", "car (parked)", conf)


def process_back(detections, alerts, valid_classes, min_conf, log):
    """Back camera: no zones, just cooldown-based alerting."""
    for det in detections:
        cls = det.label.name
        conf = det.score
        if cls not in valid_classes or conf < min_conf:
            continue
        if alerts.should_alert("back", "all", cls):
            alerts.send("back", cls, conf)


# ---------------------------------------------------------------------------
# MAIN LOOP WITH RECONNECTION
# ---------------------------------------------------------------------------

def run(cfg, log):
    """Main detection loop. Reconnects on RTSP or stream failures."""
    zones = ZoneChecker(cfg["zones"])
    alerts = AlertManager(cfg, log)
    heartbeat = Heartbeat(cfg, log)

    cams = cfg["cameras"]
    front_cls = set(cams["front"]["detect_classes"])
    back_cls = set(cams["back"]["detect_classes"])
    front_conf = cams["front"]["confidence"]
    back_conf = cams["back"]["confidence"]

    delay = cfg.get("rtsp_reconnect_delay", 5)
    max_retries = cfg.get("rtsp_max_reconnects", 50)
    retries = 0
    stream = None

    while retries < max_retries:
        try:
            log.info("Connecting to cameras...")
            stream = create_inference_stream(
                network=cfg["model"],
                sources=[cams["front"]["url"], cams["back"]["url"]],
                pipe_type="gst",
                rtsp_latency=1,
                aipu_cores=cfg.get("aipu_cores", 4),
            )
            log.info("Stream connected. Processing frames.")
            retries = 0

            for frame in stream:
                heartbeat.tick()
                alerts.cleanup_parked()

                if frame.stream_id == 0:
                    process_front(frame.detections, zones, alerts,
                                  front_cls, front_conf, log)
                elif frame.stream_id == 1:
                    process_back(frame.detections, alerts,
                                 back_cls, back_conf, log)

        except KeyboardInterrupt:
            log.info("Keyboard interrupt. Stopping.")
            break

        except Exception as e:
            retries += 1
            log.error(f"Stream error ({retries}/{max_retries}): {e}")
            if retries < max_retries:
                log.info(f"Reconnecting in {delay}s...")
                time.sleep(delay)
            else:
                log.critical("Max reconnect attempts. Exiting.")

        finally:
            if stream:
                try:
                    stream.stop()
                except Exception:
                    pass
                stream = None

    log.info("Detector stopped.")


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    os.environ["AXELERA_LOW_LATENCY"] = "1"
    os.environ["AXELERA_STREAM_QUEUE_SIZE"] = "1"

    script_dir = Path(__file__).parent
    config_path = script_dir / "config.json"
    if not config_path.exists():
        config_path = Path("config.json")

    cfg = load_config(str(config_path))
    log = setup_logging(cfg)

    log.info("=" * 60)
    log.info("Metis Home Assistant Detector v2")
    log.info("=" * 60)
    log.info(f"Model: {cfg['model']}")
    log.info(f"Cameras: {len(cfg['cameras'])}")
    log.info(f"Zones: {list(cfg['zones'].keys())}")
    log.info(f"Cooldown: {cfg['alerts']['cooldown_seconds']}s")
    log.info(f"Parking trigger: {cfg['alerts']['parking_duration_seconds']}s")
    log.info(f"Max reconnects: {cfg.get('rtsp_max_reconnects', 50)}")
    log.info("=" * 60)

    def handle_signal(sig, frame):
        log.info(f"Signal {sig} received. Shutting down.")
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    with display.App(visible=False, opengl=False, buffering=False) as app:
        wnd = app.create_window("Metis HA Detector", (1280, 720))
        app.start_thread(
            lambda w: run(cfg, log), (wnd,), name="DetectorThread"
        )
        app.run()
