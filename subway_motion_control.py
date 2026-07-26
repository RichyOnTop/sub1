#!/usr/bin/env python3
"""Camera gesture controls for a focused browser game (Windows).

Run from PowerShell, give focus to the game window, then stand still during
the short calibration. Press q in the preview window to quit.
"""
from __future__ import annotations

import argparse
import collections
import ctypes
import logging
import os
import sys
import threading
import time
import urllib.request
from ctypes import wintypes
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

IS_WINDOWS = sys.platform == "win32"


# --------------------------------------------------------------------------
# Windows key injection (SendInput, hardware scan codes)
# --------------------------------------------------------------------------
class _KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    )


class _INPUTUnion(ctypes.Union):
    _fields_ = (("ki", _KEYBDINPUT),)


class _INPUT(ctypes.Structure):
    _fields_ = (("type", wintypes.DWORD), ("union", _INPUTUnion))


INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008

# Hardware scan codes (Set 1) for the arrow keys. All arrow keys are
# "extended" keys on a standard keyboard.
SCAN_CODES = {"up": 0x48, "down": 0x50, "left": 0x4B, "right": 0x4D}


def _send_scan_code(scan_code: int, key_up: bool) -> None:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    flags = KEYEVENTF_SCANCODE | KEYEVENTF_EXTENDEDKEY
    if key_up:
        flags |= KEYEVENTF_KEYUP
    extra = ctypes.pointer(wintypes.ULONG(0))
    ki = _KEYBDINPUT(0, scan_code, flags, 0, extra)
    inp = _INPUT(INPUT_KEYBOARD, _INPUTUnion(ki=ki))
    if user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp)) != 1:
        raise OSError(ctypes.get_last_error())


class KeySender:
    """Send a short arrow-key tap to whatever window has focus.

    ``sendinput`` uses the Win32 ``SendInput`` API with hardware scan codes
    (no extra dependency, works with most browser games). ``pyautogui`` is an
    optional fallback if you have that package installed. ``dry-run`` is
    useful for checking gesture recognition without controlling anything.
    """

    def __init__(self, backend: str, hold_ms: int) -> None:
        if backend == "auto":
            backend = "sendinput" if IS_WINDOWS else "dry-run"
        if backend == "sendinput" and not IS_WINDOWS:
            raise RuntimeError("The 'sendinput' backend only works on Windows.")
        if backend == "pyautogui":
            try:
                import pyautogui  # noqa: F401
            except ImportError as exc:
                raise RuntimeError(
                    "The 'pyautogui' backend needs the pyautogui package: "
                    "pip install pyautogui"
                ) from exc
        self.backend, self.hold_ms = backend, hold_ms
        logging.info("Keyboard backend: %s", backend)

    def tap(self, key: str) -> None:
        logging.info("gesture -> %s", key.upper())
        if self.backend == "dry-run":
            return
        try:
            if self.backend == "sendinput":
                scan = SCAN_CODES[key]
                _send_scan_code(scan, key_up=False)
                time.sleep(self.hold_ms / 1000)
                _send_scan_code(scan, key_up=True)
            elif self.backend == "pyautogui":
                import pyautogui

                pyautogui.keyDown(key)
                time.sleep(self.hold_ms / 1000)
                pyautogui.keyUp(key)
        except OSError as exc:
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


MODEL_VARIANT_URLS = {
    "lite": (
        "https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
        "pose_landmarker_lite/float16/latest/pose_landmarker_lite.task"
    ),
    "full": (
        "https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
        "pose_landmarker_full/float16/latest/pose_landmarker_full.task"
    ),
    "heavy": (
        "https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
        "pose_landmarker_heavy/float16/latest/pose_landmarker_heavy.task"
    ),
}


def default_model_path(variant: str) -> str:
    """Pick a sensible per-user cache location for the downloaded model."""
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache"))
    return os.path.join(base, "subway-motion-control", f"pose_landmarker_{variant}.task")


def ensure_model(model_path: str, variant: str) -> str:
    """Download the official Pose Landmarker model once, if it is absent."""
    path = os.path.expanduser(model_path)
    if os.path.isfile(path):
        return path
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    temporary = path + ".download"
    logging.info("Downloading MediaPipe pose model (first run only)...")
    try:
        urllib.request.urlretrieve(MODEL_VARIANT_URLS[variant], temporary)
        os.replace(temporary, path)
    except OSError as exc:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise RuntimeError(
            f"Could not download the pose model: {exc}. Download it from "
            f"{MODEL_VARIANT_URLS[variant]} and pass its path with --model."
        ) from exc
    return path



def draw_landmarks(frame, landmarks) -> None:
    """Lightweight landmark overlay; Tasks API has no legacy drawing_utils."""
    height, width = frame.shape[:2]
    for point in landmarks:
        if getattr(point, "visibility", 1.0) >= 0.55:
            cv2.circle(frame, (int(point.x * width), int(point.y * height)), 3, (0, 220, 0), -1)


def open_camera(index: int, backend: str, width: int, height: int) -> cv2.VideoCapture:
    """Open the webcam, preferring DirectShow on Windows for faster/steadier init."""
    api_preference = {
        "any": cv2.CAP_ANY,
        "dshow": cv2.CAP_DSHOW,
        "msmf": cv2.CAP_MSMF,
    }[backend]
    cap = cv2.VideoCapture(index, api_preference)
    if width:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    if height:
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    # Ask the driver to keep only the newest frame internally. Most Windows
    # backends (DirectShow/MSMF) ignore this, which is why FreshFrameReader
    # below is the real fix for growing lag on slow hardware.
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap


class FreshFrameReader:
    """Continuously grab frames on a background thread and only ever hand out
    the newest one.

    Without this, a slow pose-detection step can't keep up with the camera's
    capture rate. OpenCV/the OS then queue up frames, and the main loop keeps
    processing older and older buffered frames -- the lag grows without
    bound over time (this is what produces a multi-second, ever-increasing
    delay on slower hardware). Reading in a dedicated thread and always
    discarding all but the latest frame keeps the controller reacting to
    "now", at the cost of only pose-detection speed, not the camera's frame
    rate.
    """

    def __init__(self, cap: cv2.VideoCapture) -> None:
        self._cap = cap
        self._lock = threading.Lock()
        self._frame = None
        self._ok = False
        self._frame_id = 0
        self._stopped = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stopped:
            ok, frame = self._cap.read()
            with self._lock:
                self._ok, self._frame = ok, frame
                if ok:
                    self._frame_id += 1
            if not ok:
                time.sleep(0.05)

    def read(self):
        """Return (ok, frame, frame_id). frame_id lets callers detect and skip
        a frame they've already processed, instead of burning CPU redoing
        pose detection on the same image while waiting for the next capture.
        """
        with self._lock:
            return self._ok, self._frame, self._frame_id

    def stop(self) -> None:
        self._stopped = True
        self._thread.join(timeout=1)



def main() -> int:
    parser = argparse.ArgumentParser(description="Control a browser game with camera gestures")
    parser.add_argument("--camera", type=int, default=0, help="OpenCV camera index (default: 0)")
    default_cam_backend = "dshow" if IS_WINDOWS else "any"
    parser.add_argument(
        "--camera-backend",
        choices=("any", "dshow", "msmf"),
        default=default_cam_backend,
        help="OpenCV capture API to use when opening the webcam",
    )
    parser.add_argument("--backend", choices=("auto", "sendinput", "pyautogui", "dry-run"), default="auto")
    parser.add_argument("--calibrate-seconds", type=float, default=3.0)
    parser.add_argument("--side-threshold", type=float, default=0.12, help="Horizontal body movement, as fraction of frame width")
    parser.add_argument("--crouch-ratio", type=float, default=0.72, help="Torso height / calibration below this is crouch")
    parser.add_argument("--cooldown", type=float, default=0.55, help="Minimum seconds between key presses")
    parser.add_argument("--hold-ms", type=int, default=40, help="Key hold duration in milliseconds")
    parser.add_argument("--swap-horizontal", action="store_true", help="Swap left and right if your camera direction is reversed")
    parser.add_argument("--no-preview", action="store_true", help="Do not show the camera window")
    parser.add_argument(
        "--model-variant",
        choices=("lite", "full", "heavy"),
        default="lite",
        help="Pose model to use. 'lite' is fastest and is the default because it "
        "runs well on low-end hardware; 'full'/'heavy' are more accurate but slower.",
    )
    parser.add_argument("--model", default=None, help="Path to a specific Pose Landmarker .task model (downloaded automatically if absent)")
    parser.add_argument("--camera-width", type=int, default=640, help="Requested camera capture width (lower = faster on weak hardware)")
    parser.add_argument("--camera-height", type=int, default=480, help="Requested camera capture height (lower = faster on weak hardware)")
    parser.add_argument(
        "--process-width",
        type=int,
        default=320,
        help="Frame is downscaled to this width before pose detection (preview stays full size). "
        "Lower is faster but less precise; use 0 to disable downscaling.",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    try:
        sender = KeySender(args.backend, args.hold_ms)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    cap = open_camera(args.camera, args.camera_backend, args.camera_width, args.camera_height)
    if not cap.isOpened():
        print(f"error: cannot open camera {args.camera}", file=sys.stderr)
        return 2
    reader = FreshFrameReader(cap)

    model_path = args.model or default_model_path(args.model_variant)
    try:
        model_path = ensure_model(model_path, args.model_variant)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        reader.stop()
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
    start_time = time.monotonic()
    last_processed_ok = False
    last_frame_id = -1
    logging.info("Stand normally for %.1f seconds to calibrate. Focus the game after calibration.", args.calibrate_seconds)

    with vision.PoseLandmarker.create_from_options(options) as pose:
        while True:
            ok, frame, frame_id = reader.read()
            if not ok or frame is None:
                if not last_processed_ok:
                    time.sleep(0.01)
                    continue
                logging.error("Camera frame could not be read")
                break
            last_processed_ok = True
            if frame_id == last_frame_id:
                # Already processed this exact camera frame; avoid burning
                # CPU redoing pose detection before a new frame arrives.
                time.sleep(0.002)
                continue
            last_frame_id = frame_id
            frame = cv2.flip(frame, 1)  # intuitive preview; see --swap-horizontal if needed


            # Detect on a downscaled copy so slow hardware keeps up; the
            # preview window still shows the full-resolution frame.
            if args.process_width and frame.shape[1] > args.process_width:
                scale = args.process_width / frame.shape[1]
                small = cv2.resize(frame, (0, 0), fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            else:
                small = frame
            rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
            # Real elapsed time, not a fixed per-frame increment, so the
            # VIDEO-mode timestamp reflects actual capture time even when
            # frames arrive irregularly on slower hardware.
            timestamp_ms = max(1, int((time.monotonic() - start_time) * 1000))
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
    reader.stop()
    cap.release()
    cv2.destroyAllWindows()
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
