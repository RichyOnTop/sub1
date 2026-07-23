#!/usr/bin/env python3
"""Camera gesture controls for a focused browser game.

Run from a terminal, give focus to the game window, then stand still during the
short calibration. Press q in the preview window to quit.
"""
from __future__ import annotations

import argparse
import collections
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from typing import Optional

import cv2
import mediapipe as mp
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision

# Pose landmark indexes (kept as integers so this also works across MediaPipe APIs).
NOSE, LEFT_SHOULDER, RIGHT_SHOULDER = 0, 11, 12
LEFT_WRIST, RIGHT_WRIST = 15, 16
LEFT_HIP, RIGHT_HIP = 23, 24


class KeySender:
    """Send a short arrow-key tap using a Linux input backend.

    ydotool is the recommended choice for KDE Wayland; it requires the ydotoold
    service. xdotool is provided for X11 sessions. `--backend dry-run` is useful
    for checking gesture recognition without controlling anything.
    """
    KEY_CODES = {"up": 103, "down": 108, "left": 105, "right": 106}
    WTYPE_NAMES = {"up": "Up", "down": "Down", "left": "Left", "right": "Right"}

    def __init__(self, backend: str, hold_ms: int) -> None:
        if backend == "auto":
            backend = "ydotool" if shutil.which("ydotool") else "xdotool" if shutil.which("xdotool") else "wtype" if shutil.which("wtype") else "dry-run"
        if backend != "dry-run" and not shutil.which(backend):
            raise RuntimeError(f"Backend '{backend}' is not installed. See README.md.")
        self.backend, self.hold_ms = backend, hold_ms
        logging.info("Keyboard backend: %s", backend)

    def tap(self, key: str) -> None:
        logging.info("gesture -> %s", key.upper())
        if self.backend == "dry-run":
            return
        try:
            if self.backend == "ydotool":
                # Linux evdev key code: 1 is press, 0 is release.
                subprocess.run(["ydotool", "key", f"{self.KEY_CODES[key]}:1", f"{self.KEY_CODES[key]}:0"], check=True, timeout=2)
            elif self.backend == "xdotool":
                subprocess.run(["xdotool", "key", "--delay", str(self.hold_ms), self.WTYPE_NAMES[key]], check=True, timeout=2)
            elif self.backend == "wtype":
                subprocess.run(["wtype", "-k", self.WTYPE_NAMES[key]], check=True, timeout=2)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            logging.error("Could not send %s with %s: %s", key, self.backend, exc)


@dataclass
class Calibration:
    torso_sizes: list[float]
    center_xs: list[float]
    started: float
    seconds: float
    torso: Optional[float] = None
    center_x: Optional[float] = None

    def add(self, torso: float, center_x: float) -> bool:
        if self.torso is not None:
            return True
        self.torso_sizes.append(torso)
        self.center_xs.append(center_x)
        if time.monotonic() - self.started >= self.seconds and len(self.torso_sizes) >= 10:
            self.torso = sorted(self.torso_sizes)[len(self.torso_sizes) // 2]
            self.center_x = sorted(self.center_xs)[len(self.center_xs) // 2]
            logging.info("Calibrated: torso=%.3f, center=%.3f", self.torso, self.center_x)
            return True
        return False

    @property
    def remaining(self) -> float:
        return max(0, self.seconds - (time.monotonic() - self.started))


def midpoint(a, b):
    return ((a.x + b.x) / 2, (a.y + b.y) / 2)


def read_body(landmarks):
    """Return normalized torso height, body center and whether both hands are raised."""
    try:
        ls, rs = landmarks[LEFT_SHOULDER], landmarks[RIGHT_SHOULDER]
        lh, rh = landmarks[LEFT_HIP], landmarks[RIGHT_HIP]
        lw, rw = landmarks[LEFT_WRIST], landmarks[RIGHT_WRIST]
        nose = landmarks[NOSE]
    except IndexError:
        return None
    required = (ls, rs, lh, rh, lw, rw, nose)
    if any(getattr(p, "visibility", 1.0) < 0.55 for p in required):
        return None
    sx, sy = midpoint(ls, rs)
    hx, hy = midpoint(lh, rh)
    # y increases down the captured image. Both hands above the face avoids a
    # one-handed wave being mistaken for a jump.
    arms_up = lw.y < nose.y and rw.y < nose.y
    return abs(hy - sy), (sx + hx) / 2, arms_up


MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
             "pose_landmarker_full/float16/latest/pose_landmarker_full.task")


def ensure_model(model_path: str) -> str:
    """Download the official Pose Landmarker model once, if it is absent."""
    path = os.path.expanduser(model_path)
    if os.path.isfile(path):
        return path
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    temporary = path + ".download"
    logging.info("Downloading MediaPipe pose model (first run only)…")
    try:
        urllib.request.urlretrieve(MODEL_URL, temporary)
        os.replace(temporary, path)
    except OSError as exc:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise RuntimeError(
            f"Could not download the pose model: {exc}. Download it from {MODEL_URL} "
            f"and pass its path with --model."
        ) from exc
    return path


def draw_landmarks(frame, landmarks) -> None:
    """Lightweight landmark overlay; Tasks API has no legacy drawing_utils."""
    height, width = frame.shape[:2]
    for point in landmarks:
        if getattr(point, "visibility", 1.0) >= 0.55:
            cv2.circle(frame, (int(point.x * width), int(point.y * height)), 3, (0, 220, 0), -1)


def main() -> int:
    parser = argparse.ArgumentParser(description="Control a browser game with camera gestures")
    parser.add_argument("--camera", type=int, default=0, help="OpenCV camera index (default: 0)")
    parser.add_argument("--backend", choices=("auto", "ydotool", "xdotool", "wtype", "dry-run"), default="auto")
    parser.add_argument("--calibrate-seconds", type=float, default=3.0)
    parser.add_argument("--side-threshold", type=float, default=0.12, help="Horizontal body movement, as fraction of frame width")
    parser.add_argument("--crouch-ratio", type=float, default=0.72, help="Torso height / calibration below this is crouch")
    parser.add_argument("--cooldown", type=float, default=0.55, help="Minimum seconds between key presses")
    parser.add_argument("--hold-ms", type=int, default=40, help="Key hold duration for xdotool")
    parser.add_argument("--swap-horizontal", action="store_true", help="Swap left and right if your camera direction is reversed")
    parser.add_argument("--no-preview", action="store_true", help="Do not show the camera window")
    default_model = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "subway-motion-control", "pose_landmarker_full.task")
    parser.add_argument("--model", default=default_model, help="Path to Pose Landmarker .task model (downloaded automatically if absent)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    try:
        sender = KeySender(args.backend, args.hold_ms)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    cap = cv2.VideoCapture(args.camera)
    if not cap.isOpened():
        print(f"error: cannot open camera {args.camera}", file=sys.stderr)
        return 2

    try:
        model_path = ensure_model(args.model)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        cap.release()
        return 2
    options = vision.PoseLandmarkerOptions(
        base_options=mp_python.BaseOptions(model_asset_path=model_path),
        running_mode=vision.RunningMode.VIDEO,
        num_poses=1,
        min_pose_detection_confidence=0.65,
        min_tracking_confidence=0.65,
    )
    cal = Calibration([], [], time.monotonic(), args.calibrate_seconds)
    history: collections.deque[str] = collections.deque(maxlen=4)
    last_key_time = 0.0
    timestamp_ms = 0
    logging.info("Stand normally for %.1f seconds to calibrate. Focus the game after calibration.", args.calibrate_seconds)

    with vision.PoseLandmarker.create_from_options(options) as pose:
        while True:
            ok, frame = cap.read()
            if not ok:
                logging.error("Camera frame could not be read")
                break
            frame = cv2.flip(frame, 1)  # intuitive preview; see --swap-horizontal if needed
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            timestamp_ms += 33  # strictly increasing timestamp for Tasks VIDEO mode
            image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            result = pose.detect_for_video(image, timestamp_ms)
            action = None
            landmarks = result.pose_landmarks[0] if result.pose_landmarks else None
            body = read_body(landmarks) if landmarks else None
            if body:
                torso, center_x, arms_up = body
                if not cal.add(torso, center_x):
                    label = f"Calibrating: stand straight ({cal.remaining:.1f}s)"
                else:
                    assert cal.torso is not None and cal.center_x is not None
                    # Priority ensures hands overhead is never interpreted as crouch/side-step.
                    if arms_up:
                        action = "up"
                    elif torso / cal.torso < args.crouch_ratio:
                        action = "down"
                    elif center_x - cal.center_x > args.side_threshold:
                        action = "right"
                    elif center_x - cal.center_x < -args.side_threshold:
                        action = "left"
                    if args.swap_horizontal and action in ("left", "right"):
                        action = "left" if action == "right" else "right"
                    label = f"Gesture: {action or 'neutral'}"
                if landmarks:
                    draw_landmarks(frame, landmarks)
            else:
                label = "Body not fully visible"

            # Require the same gesture on 3 of the last 4 camera frames, then
            # use cooldown so holding a pose is one game action rather than spam.
            history.append(action or "")
            stable = action and history.count(action) >= 3
            if stable and time.monotonic() - last_key_time >= args.cooldown:
                sender.tap(action)
                last_key_time = time.monotonic()
                history.clear()

            if not args.no_preview:
                cv2.putText(frame, label, (18, 36), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
                cv2.putText(frame, "q: quit", (18, 68), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
                cv2.imshow("Subway Motion Control", frame)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break
    cap.release()
    cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
