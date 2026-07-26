# Subway Motion Control

Use a webcam and MediaPipe pose tracking to send arrow-key taps to a focused browser game (such as Subway Surfers).

| Your pose | Sent key |
| --- | --- |
| Hold **both** wrists above your head | Up / jump |
| Move your body to the right or left | Right / left |
| Crouch (shorten your visible torso) | Down / crouch |

The program performs all pose processing locally. It does not upload camera frames.

## Windows setup

Install Python 3.10+ from [python.org](https://www.python.org/downloads/) or the Microsoft Store, making sure "Add python.exe to PATH" is checked during install.

Open **PowerShell** and set up an isolated environment:

```powershell
cd C:\path\to\sub1
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

> If `Activate.ps1` is blocked by execution policy, run PowerShell as your normal user and allow local scripts once with:
> `Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned`

> **MediaPipe version:** this controller uses MediaPipe's current **Tasks API** and supports MediaPipe `0.10.35`. On the first run it automatically downloads the official Pose Landmarker model (about 6 MB) into `%LOCALAPPDATA%\subway-motion-control\`. Use `--model C:\path\to\pose_landmarker_full.task` if you need to provide the model yourself.

Key presses are sent with the Windows **`SendInput`** API (via `ctypes`), which is built into Python on Windows — no extra tool or driver install is required. If a particular game doesn't respond to `SendInput` (some games only accept input from specific libraries), you can install `pyautogui` instead (`pip install pyautogui`, `--backend pyautogui`).

## Run

1. Open Subway Surfers in your browser. Start the game and ensure it responds to the arrow keys.
2. Run the controller from PowerShell:

   ```powershell
   .venv\Scripts\Activate.ps1
   python subway_motion_control.py --backend sendinput
   ```

3. Stand upright and centered until its three-second calibration completes. The preview says `Gesture: neutral` when ready.
4. Click the browser game window so it has keyboard focus, while leaving the camera preview visible. Perform gestures. Press **q** in the preview to stop.

The webcam preview is mirrored, like a mirror, so moving your body to your right should produce `right`. If the camera setup gives the opposite result, add `--swap-horizontal`.

### First-run and tuning commands

Test only recognition (no keys are sent):

```powershell
python subway_motion_control.py --backend dry-run
```

Choose another camera (for example, an external webcam):

```powershell
python subway_motion_control.py --camera 1 --backend sendinput
```

If your webcam is slow to open or the preview looks wrong, try a different capture API:

```powershell
python subway_motion_control.py --camera-backend msmf --backend sendinput
```

Useful sensitivity adjustments:

```powershell
# A smaller threshold reacts to smaller sideways moves (default 0.12)
python subway_motion_control.py --side-threshold 0.08 --backend sendinput

# Larger ratio makes crouch easier to trigger; default is 0.72
python subway_motion_control.py --crouch-ratio 0.80 --backend sendinput
```

The program requires a full upper body in view: face, shoulders, hips, and both wrists. It emits a short key tap only after a gesture is stable over several frames and then enforces a 0.55-second cooldown, avoiding repeated actions while a pose is held. Increase `--cooldown` if the game still gets duplicate moves.

## Low-end hardware / laggy response

By default the controller is already tuned for weaker CPUs: it uses the fast `lite` pose model, captures at 640×480, downscales frames to 320px wide before running pose detection, and always reads the newest available camera frame from a background thread (rather than blocking the whole loop on each `cap.read()`). Together these stop the input lag from compounding over time, which is the usual cause of a delay that keeps growing the longer you play.

If you still see rising lag or a very "behind" response, try, in order:

```powershell
# Shrink the pose-detection frame further (default 320; try 240 or 160)
python subway_motion_control.py --process-width 240 --backend sendinput

# Capture at an even lower camera resolution (needs your webcam to support it)
python subway_motion_control.py --camera-width 480 --camera-height 360 --backend sendinput

# Skip the preview window entirely, which also costs CPU/GPU time to draw
python subway_motion_control.py --no-preview --backend sendinput
```

Also worth doing on the Windows side:

- Close other apps that use the camera or CPU heavily (Teams, Zoom, browser tabs playing video, etc.) while running the controller.
- Lower your webcam's native capture resolution in the Windows Camera app or manufacturer software; a high-resolution webcam feed costs more per frame even before `--camera-width`/`--camera-height` are applied.
- Plug in a laptop that's on battery — Windows' power-saving mode can throttle the CPU significantly.
- If lag still grows over many minutes, it usually means your CPU can't keep up with pose detection at all; `--model-variant lite --process-width 160` is the fastest combination this script supports.

`--model-variant full` or `--model-variant heavy` are available if your hardware is capable and you want more accurate tracking, but they are slower and not recommended on low-end machines.

## Troubleshooting


- **No preview / camera cannot open:** close other apps using the webcam (Camera app, Teams, Zoom); check Windows Settings → Privacy & security → Camera → "Let desktop apps access your camera" is on; test `--camera 1`; try `--camera-backend dshow` or `--camera-backend msmf`.
- **`ModuleNotFoundError`:** activate `.venv` (`.venv\Scripts\Activate.ps1`) and run `pip install -r requirements.txt`.
- **Pose says `Body not fully visible`:** step back, improve front lighting, and ensure hands, face, shoulders and hips are in frame.
- **Preview detects gestures but the game does nothing:** click the game to focus it. If it still doesn't respond to `--backend sendinput`, try `--backend pyautogui` (after `pip install pyautogui`).
- **Wrong direction:** use `--swap-horizontal`.
- **Browser intercepts a key:** keep the game focused, not the address bar or camera preview.
- **`Activate.ps1` cannot be loaded because running scripts is disabled:** run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned` in that PowerShell window, then retry activation.

Only run this while you intend it to send keys. Use `q` to exit before doing unrelated work.
