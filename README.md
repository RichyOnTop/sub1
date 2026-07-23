# Subway Motion Control

Use a webcam and MediaPipe pose tracking to send arrow-key taps to a focused browser game (such as Subway Surfers).

| Your pose | Sent key |
| --- | --- |
| Hold **both** wrists above your head | Up / jump |
| Move your body to the right or left | Right / left |
| Crouch (shorten your visible torso) | Down / crouch |

The program performs all pose processing locally. It does not upload camera frames.

## CachyOS / KDE Plasma setup

This is intended for a **KDE Wayland** desktop, which is commonly the default. `ydotool` injects keys through the Linux input subsystem and is the recommended backend. Install Python and system packages (package availability/names can vary by CachyOS configuration):

```bash
sudo pacman -S python python-pip ydotool
# Enable the ydotool daemon. Check its man page / package unit name if this differs:
systemctl --user enable --now ydotoold.service
```

Create an isolated environment and install the camera libraries:

```bash
cd /path/to/sub1
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

If `ydotoold.service` does not exist, start the installed `ydotoold` daemon according to your package's documentation. The daemon needs permission to access `/dev/uinput`; an input-injection command that fails generally indicates a daemon or permissions issue, not a gesture-tracking issue.

For an **X11** Plasma session, install `xdotool` instead and pass `--backend xdotool`:

```bash
sudo pacman -S xdotool
```

`wtype` is also supported (`--backend wtype`) where its Wayland virtual-keyboard protocol is enabled, but this is compositor/security-policy dependent.

## Run

1. Open Subway Surfers in Brave. Start the game and ensure it responds to the arrow keys.
2. Run the controller from a terminal:

   ```bash
   source .venv/bin/activate
   python subway_motion_control.py --backend ydotool
   ```

3. Stand upright and centered until its three-second calibration completes. The preview says `Gesture: neutral` when ready.
4. Click the Brave game window so it has keyboard focus, while leaving the camera preview visible. Perform gestures. Press **q** in the preview to stop.

The webcam preview is mirrored, like a mirror, so moving your body to your right should produce `right`. If the camera setup gives the opposite result, add `--swap-horizontal`.

### First-run and tuning commands

Test only recognition (no keys are sent):

```bash
python subway_motion_control.py --backend dry-run
```

Choose another camera (for example, an external webcam):

```bash
python subway_motion_control.py --camera 1 --backend ydotool
```

Useful sensitivity adjustments:

```bash
# A smaller threshold reacts to smaller sideways moves (default 0.12)
python subway_motion_control.py --side-threshold 0.08 --backend ydotool

# Larger ratio makes crouch easier to trigger; default is 0.72
python subway_motion_control.py --crouch-ratio 0.80 --backend ydotool
```

The program requires a full upper body in view: face, shoulders, hips, and both wrists. It emits a short key tap only after a gesture is stable over several frames and then enforces a 0.55-second cooldown, avoiding repeated actions while a pose is held. Increase `--cooldown` if the game still gets duplicate moves.

## Troubleshooting

- **No preview / camera cannot open:** close other apps using the webcam; test `--camera 1`; verify your user can access the camera device.
- **`ModuleNotFoundError`:** activate `.venv` and run `pip install -r requirements.txt`.
- **Pose says `Body not fully visible`:** step back, improve front lighting, and ensure hands, face, shoulders and hips are in frame.
- **Preview detects gestures but the game does nothing:** click the game to focus it. Then verify injection independently: `ydotool key 103:1 103:0` should send an Up tap. Fix/start `ydotoold` and its uinput permission if it does not.
- **Wrong direction:** use `--swap-horizontal`.
- **Browser intercepts a key:** keep the game focused, not the address bar or camera preview.

Only run this while you intend it to send keys. Use `q` to exit before doing unrelated work.
