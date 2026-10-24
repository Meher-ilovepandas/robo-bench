# robo-bench — VEX AIM AI Benchmark

A benchmark harness that measures how well AI models can program a real robot.
Each model writes Python for a [VEX AIM](https://www.vex.com/aim) robot to play a game
(for example: grab a ball and knock down an AprilTag target). The code runs on the
robot while an overhead camera films the field; the model then sees its own code,
console logs, telemetry and camera frames, and gets another attempt to improve.

Models currently benchmarked (see `config.yaml`):

| Name  | Model             | API                         |
| ----- | ----------------- | --------------------------- |
| Opus  | `claude-opus-5-5` | Anthropic Messages API      |
| Astra | `gpt-6-astra`     | OpenAI Responses API        |
| Muse  | `muse-spark-1.3`  | Meta Model API (Responses)  |

All models run with reasoning on at the same effort (`high`), the same output-token
cap and the same image budget, so results are comparable.

## How a benchmark works

```
Game  ──►  Experiment (one model)  ──►  Attempt 1 ──► Trials ──► Attempt 2 ──► … (max 3 attempts)
```

- **Game** — a field setup plus a written objective, edited in the web UI.
- **Experiment** — one model playing one game, in one of two modes:
  - **Pure code**: the model writes a complete Python program per attempt.
  - **LLM drive**: the model controls the robot turn by turn (move / turn / kick / scan / photo).
- **Attempt** — one generated program. Each attempt can be run for up to 3 **trials**
  (60 s each); at least 1 completed trial is needed before the next attempt.

### What the model receives

| Attempt 1 | Attempt N (N > 1) |
| --- | --- |
| System preamble (`system_preamble.txt`) | same |
| Compact VEX AIM API reference (`vex_api_compact.txt`) | same |
| Game description (frozen when attempt 1 is generated) | same |
| Overhead photo of the field at the start | same photo |
| | Code from attempt N-1 |
| | Console logs, telemetry and camera frames from attempt N-1's trials |

Only the previous attempt's results are included, never earlier ones. Trials
stopped with the Stop button, or that crashed, are not sent to the model.

### During a trial

- The program runs on the host computer and drives the robot over Wi-Fi through the
  vendored VEX AIM WebSocket client in `vex/`.
- The overhead camera captures a frame every 2 s (plus 3 s after the program ends).
- Telemetry (heading, position, vision detections, …) is recorded from the program's
  own robot connection via `robot/launcher.py` — the harness never opens a second
  connection to the robot, which would disrupt the program.
- Console output (`print()`) is captured unbuffered, so nothing is lost at the time limit.

## Requirements

- macOS (camera handling uses AVFoundation via OpenCV)
- Python 3.10+
- A VEX AIM robot on the same Wi-Fi network as the computer
- An overhead USB camera delivering 1920×1080 (tested with the EMEET SmartCam C960)
- API keys for the models you want to benchmark

## Setup

```bash
git clone https://github.com/Meher-ilovepandas/robo-bench.git
cd robo-bench
pip3 install -r requirements.txt
cp .env.example .env        # then fill in your API keys
```

`.env`:

```
ANTHROPIC_API_KEY=...
OPENAI_API_KEY=...
META_API_KEY=...
```

On macOS, give your terminal app camera access:
**System Settings → Privacy & Security → Camera**.

## Running

```bash
PYTHONUNBUFFERED=1 python3 -m uvicorn web.server:app --host 0.0.0.0 --port 8000 --timeout-graceful-shutdown 3
```

Then open <http://localhost:8000>.

Both flags matter:
- `PYTHONUNBUFFERED=1` — otherwise harness log lines are buffered and don't appear.
- `--timeout-graceful-shutdown 3` — the live camera preview is an endless stream;
  without a timeout a stopped server never exits and keeps holding the camera.

### Workflow in the UI

1. **Config** — set the robot's IP and click *Connect Robot*; check the camera
   preview shows the field.
2. **Games** — create a game and write its objective; *Save*.
3. **Experiment** — open experiments for the game, *+ New*, pick a model and mode.
4. **New Attempt** — the model generates code (this can take a few minutes).
   The *Prompt*, *Code*, *Reasoning* and *Trace* tabs show exactly what was sent and returned.
5. **Start Run** — run a trial; repeat for up to 3 trials.
6. **New Attempt** again — the model gets the previous attempt's results and improves.

Messages that need attention (camera stopped, robot unreachable, a model API error)
pop up in the bottom-right corner.

## Configuration (`config.yaml`)

| Section | Key | Meaning |
| --- | --- | --- |
| `models` | `<Name>.api_type / model_id / max_tokens` | Models to benchmark (`anthropic`, `openai`, `meta`) |
| `retired_models` | `<Name>: model_id` | Old models whose experiments stay viewable but can't continue |
| `benchmark` | `code_attempts` | Attempts per experiment (3) |
| | `trials_per_code` | Max trials per attempt (3) |
| | `min_trials_before_next` | Trials needed before the next attempt (1) |
| | `run_duration_sec` | Trial length (60 s) |
| | `post_run_capture_sec` | Extra filming after the program ends (3 s) |
| `prompt` | `max_images` | Images per prompt, same for every model (50 — Muse's limit) |
| | `image_max_side_px` | Prompt images are downscaled to this size (originals kept on disk) |
| | `telemetry_interval_sec` | Telemetry rows sent to the model (one per second) |
| `robot` | `robot_ip` | Default robot IP (the last IP set in the UI is remembered) |
| `camera` | `width / height` | Resolution that identifies the overhead camera (1920×1080) |
| | `capture_fps` | Frames per second during a trial (0.5) |

## Where results are stored

Everything is saved under `sessions/` (git-ignored):

```
sessions/games/game_001/
├── meta.json, prompt.txt                     game name and description
└── experiments/exp_001/
    ├── config.json                           model and mode
    ├── start_image.jpg, game_prompt.txt      first photo and frozen description
    └── attempts/attempt_001/
        ├── input/prompt.txt, img_*.jpg       exactly what the model was sent
        ├── output.txt                        generated code
        ├── reasoning.txt                     the model's reasoning summary
        ├── trace.txt, harness.log
        └── runs/run_001/
            ├── console.log                   program output
            ├── telemetry.json, telemetry_series.json
            ├── frames/frame_*.jpg, frames.json   camera frames and capture times
            └── summary.json                  duration, exit code, gaps, …
```

## Troubleshooting

| Problem | Fix |
| --- | --- |
| Camera preview is black or frozen | Usually the Mac slept (lid closed / battery) or the USB cable dropped. Click **⟳ Restart server** on the Config page. To prevent it, keep the Mac awake (`caffeinate -d`) and on power. |
| "No camera delivers 1920×1080" | The overhead camera isn't connected, or was plugged in after the server started — plug it in and restart the server. The built-in camera is refused on purpose. |
| "Robot not reachable" | The robot is off, asleep, or got a new IP. Turn it on, enter its IP on the Config page, click *Set* then *Connect Robot*. |
| New Attempt is refused | Read the pop-up: usually the previous attempt has no completed trial yet, or the experiment already has 3 attempts. |
| More than one server running | `ps aux \| grep "uvicorn web.server"` should show one process; stop extras before starting a new one. |

## Repository layout

```
harness.py              orchestrates generation, trials and drive sessions
storage.py              on-disk layout of games / experiments / attempts / runs
config.py, config.yaml  configuration (+ API keys from .env)
web/server.py           FastAPI server and API
web/static/index.html   web UI
models/                 model adapters (Anthropic; OpenAI-compatible Responses API for OpenAI and Meta)
camera/capture.py       overhead camera capture
robot/runner.py         runs a trial: subprocess, console capture, telemetry
robot/launcher.py       wraps the generated program to record telemetry
robot/daemon.py         robot connection used by LLM drive mode
vex/                    VEX AIM WebSocket Python client (vendor code)
system_preamble.txt     system prompt given to every model
vex_api_compact.txt     API reference given to every model
vex_api.txt             full scraped VEX AIM API docs (download_api.py)
```
