"""
VEX AIM Benchmark Harness — disk-backed, stateless orchestrator.

All persistent state lives on disk (via Storage). Ephemeral state:
  - robot connection status
  - active run subprocess
  - camera handle
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Callable, Awaitable

BENCH_DIR = Path(__file__).parent

from camera.capture import CameraCapture, shrink_jpeg_b64
from models import create_model
from models.base import Message, TextPart, ImagePart, REASONING_EFFORT
from robot.runner import RunResult, run_code, ping_robot
from storage import Storage

BroadcastFn = Callable[[dict], Awaitable[None]]

# ── Prompt inputs ─────────────────────────────────────────────────── #
# Both files are re-read on every generation so edits apply without a restart.

SYSTEM_PREAMBLE_FILE = BENCH_DIR / "system_preamble.txt"
VEX_API_COMPACT_FILE = BENCH_DIR / "vex_api_compact.txt"
_SETTINGS_FILE = BENCH_DIR / "vex" / "settings.json"


def _saved_robot_ip() -> str | None:
    try:
        return json.loads(_SETTINGS_FILE.read_text())["connection"]["host"] or None
    except Exception:
        return None

DRIVE_SYSTEM_PROMPT = """\
You are directly controlling a VEX AIM robot on a 16"×16" (~400mm×400mm) field.
TASK: {game_prompt}

Each turn you get robot status + vision scan + (every 5 turns) overhead photo.
Respond with EXACTLY this JSON, nothing else:
{{"action": "...", "params": {{...}}, "reasoning": "one line"}}

ACTIONS:
  move  — distance_mm, angle_deg (0=fwd,180=back), velocity (0-100)
  turn  — direction ("left"|"right"), angle_deg
  kick  — strength ("soft"|"medium"|"hard")
  scan  — {{}}
  photo — {{}}
  stop  — {{}}
  done  — {{}}

TIPS: keep moves ≤200mm. Turn (cx-160)*0.22° to align object at cx to center."""


# ── Persistent robot daemon client ────────────────────────────────── #

class RobotDaemonClient:
    def __init__(self, robot_ip: str, log: Callable[[str], None] | None = None):
        self.robot_ip = robot_ip
        self.log = log
        self._proc: asyncio.subprocess.Process | None = None

    async def start(self, timeout: float = 20.0) -> None:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(BENCH_DIR) + os.pathsep + env.get("PYTHONPATH", "")
        self._proc = await asyncio.create_subprocess_exec(
            sys.executable, str(BENCH_DIR / "robot" / "daemon.py"), self.robot_ip,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env, cwd=str(BENCH_DIR),
        )
        self._stderr_task = asyncio.create_task(self._pump_stderr())
        result = await asyncio.wait_for(self._read_reply(), timeout=timeout)
        if "error" in result:
            raise RuntimeError(result["error"])

    async def _pump_stderr(self) -> None:
        # The daemon's prints / vex library messages — surface them instead of dropping them
        async for raw in self._proc.stderr:
            line = raw.decode(errors="replace").rstrip()
            if line and self.log:
                self.log(f"[daemon] {line}")

    async def _read_reply(self) -> dict:
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                raise RuntimeError("robot daemon exited")
            try:
                return json.loads(line.decode())
            except ValueError:
                continue   # stray non-protocol output

    async def send(self, cmd: dict, timeout: float = 35.0) -> dict:
        assert self._proc and self._proc.stdin
        self._proc.stdin.write((json.dumps(cmd) + "\n").encode())
        await self._proc.stdin.drain()
        return await asyncio.wait_for(self._read_reply(), timeout=timeout)

    async def stop(self) -> None:
        if self._proc and self._proc.returncode is None:
            try: self._proc.stdin.close()
            except Exception: pass
            try: await asyncio.wait_for(self._proc.wait(), timeout=4)
            except asyncio.TimeoutError: self._proc.kill()


# ── Harness ───────────────────────────────────────────────────────── #

class Harness:
    def __init__(self, cfg: dict, storage: Storage):
        self.cfg = cfg
        self.storage = storage

        # The last IP set in the UI is persisted in vex/settings.json (see set_robot_ip)
        self._robot_ip: str = _saved_robot_ip() or cfg["robot"].get("robot_ip", "192.168.5.100")
        self.robot_connected: bool = False
        self.robot_port: str = ""

        self._models = {n: create_model(cfg, n)
                        for n in cfg.get("model_order", list(cfg["models"].keys()))}
        self.model_names: list[str] = list(self._models.keys())

        self.camera = CameraCapture(cfg, log=self._log)
        self.camera.on_failure = self._camera_failed
        self.broadcast: BroadcastFn | None = None
        self.status: str = "initializing"

        # Ephemeral
        self._emergency_stop: bool = False
        self._stop_event: asyncio.Event = asyncio.Event()
        self._drive_task: asyncio.Task | None = None
        self._drive_turn: int = 0
        self._log_context: tuple[str, str, str] | None = None  # (game_id, exp_id, attempt_id)
        self._busy: str | None = None    # "generating" / "running" / "driving" — one job at a time
        self.live: dict | None = None     # ids of the run/drive in progress (UI resync after reload)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: set[asyncio.Task] = set()

    async def initialize(self) -> None:
        self._loop = asyncio.get_running_loop()
        await self._check_robot()
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.camera.open)
        self.status = "ready"
        self._log("[harness] Ready.")

    # ── Robot ────────────────────────────────────────────────────── #

    def set_robot_ip(self, ip: str) -> None:
        self._robot_ip = ip.strip()
        _SETTINGS_FILE.write_text(json.dumps({"connection": {"host": self._robot_ip}}, indent=4))
        self._log(f"[harness] Robot IP → {self._robot_ip}")

    async def _check_robot(self) -> None:
        ok = await ping_robot(self._robot_ip)
        self.robot_connected = ok
        self.robot_port = f"WiFi {self._robot_ip}" if ok else ""
        self._log(f"[harness] Robot {'connected' if ok else 'not reachable'} at {self._robot_ip}")

    async def reconnect_robot(self) -> bool:
        await self._check_robot()
        await self._broadcast({"type": "robot_status",
                               "connected": self.robot_connected,
                               "port": self.robot_port})
        return self.robot_connected

    # ── Code generation ──────────────────────────────────────────── #

    def busy_reason(self) -> str | None:
        """Why a new generate/run/drive can't start right now (None = free)."""
        return f"Harness is busy ({self._busy}). Wait for it to finish." if self._busy else None

    def _plan_generation(self, game_id: str, exp_id: str) -> tuple[dict | None, dict | None]:
        """(target, prev): attempt to (re)generate into (None = create new) and the
        attempt whose results feed the prompt. Attempt N is built only from N-1. If the
        newest attempt has no code (its LLM call failed), regenerate into it instead of
        stacking a new attempt on top of a code-less one."""
        attempts = self.storage.list_attempts(game_id, exp_id)
        if attempts and not attempts[-1]["has_code"]:
            return attempts[-1], (attempts[-2] if len(attempts) > 1 else None)
        return None, (attempts[-1] if attempts else None)

    def generate_blocker(self, game_id: str, exp_id: str) -> str | None:
        """Why a new attempt can't be generated now (None = OK)."""
        if self.busy_reason():
            return self.busy_reason()
        exp = self.storage._read_experiment(game_id, exp_id)
        if exp["model"] not in self._models:
            if exp["model"] in self.cfg.get("retired_models", {}):
                return (f"{exp['model']} is retired — this experiment is kept for reference. "
                        f"Create a new experiment to continue.")
            return f"Unknown model: {exp['model']}"
        target, prev = self._plan_generation(game_id, exp_id)
        max_attempts = int(self.cfg["benchmark"].get("code_attempts", 3))
        next_num = int(target["id"].split("_")[1]) if target else exp["n_attempts"] + 1
        if next_num > max_attempts:
            return (f"This experiment already has its {max_attempts} attempts "
                    f"(benchmark.code_attempts).")
        min_trials = int(self.cfg["benchmark"].get("min_trials_before_next", 1))
        if prev:
            have = len(self._completed_runs(game_id, exp_id, prev["id"]))
            if have < min_trials:
                return (f"{prev['id']} has {have} completed trial{'s' if have != 1 else ''} — "
                        f"run at least {min_trials} before generating the next attempt.")
        if not SYSTEM_PREAMBLE_FILE.exists() or not VEX_API_COMPACT_FILE.exists():
            return f"Missing {SYSTEM_PREAMBLE_FILE.name} or {VEX_API_COMPACT_FILE.name}."
        return None

    def run_blocker(self, game_id: str, exp_id: str, attempt_id: str) -> str | None:
        """Why a trial can't start now (None = OK). Robot/camera are checked at start."""
        if self.busy_reason():
            return self.busy_reason()
        if not self.storage.get_attempt_code(game_id, exp_id, attempt_id):
            return "This attempt has no code."
        n_trials = int(self.cfg["benchmark"].get("trials_per_code", 3))
        if len(self._completed_runs(game_id, exp_id, attempt_id)) >= n_trials:
            return (f"{attempt_id} already has its {n_trials} completed trials — "
                    f"generate the next attempt.")
        return None

    async def generate_attempt(self, game_id: str, exp_id: str) -> None:
        blocker = self.generate_blocker(game_id, exp_id)
        if blocker:
            await self._notice(blocker)
            return
        self._busy = "generating"
        try:
            await self._generate_attempt(game_id, exp_id)
        except Exception as exc:
            import traceback as _tb
            self._log(f"[harness] *** GENERATION ERROR: {exc}")
            self._log(f"[harness] {_tb.format_exc()}")
            await self._notice(f"Code generation failed: {exc}")
            self.status = "error"
            await self._broadcast({"type": "status", "status": "error", "error": str(exc)})
        finally:
            self._busy = None
            self._log_context = None

    async def _generate_attempt(self, game_id: str, exp_id: str) -> None:
        exp = self.storage._read_experiment(game_id, exp_id)
        model_name = exp["model"]
        model = self._models[model_name]
        target, prev = self._plan_generation(game_id, exp_id)

        # Freeze the game description at attempt 1 so every attempt in the experiment
        # is judged against the same objective even if the game is edited later.
        live_prompt = self.storage._read_game(game_id)["prompt"]
        game_prompt = self.storage.get_experiment_game_prompt(game_id, exp_id)
        if game_prompt is None or prev is None:
            game_prompt = live_prompt
        elif game_prompt != live_prompt:
            self._log("[harness] Note: the game description was edited after attempt 1; "
                      "this experiment keeps using its original description.")

        # First image of the game: captured once, at attempt 1, and reused afterwards.
        start_b64 = self.storage.get_experiment_start_image(game_id, exp_id) if prev else None
        if start_b64 is None:
            if prev:
                self._log("[harness] Warning: no stored first image for this experiment — "
                          "using a fresh snapshot instead.")
            loop = asyncio.get_running_loop()
            snap = await loop.run_in_executor(None, self.camera.snapshot_base64)
            if not snap:
                await self._notice("Camera snapshot failed — cannot capture the first game "
                                   "image. Check the camera and try again.")
                return
            start_b64 = snap[0]

        attempt = target or self.storage.create_attempt(game_id, exp_id)
        aid = attempt["id"]
        attempt_num = int(aid.split("_")[1])
        if prev is None:
            self.storage.save_experiment_start_image(game_id, exp_id, start_b64)
            self.storage.save_experiment_game_prompt(game_id, exp_id, game_prompt)

        self.status = "generating"
        self._log_context = (game_id, exp_id, aid)
        await self._broadcast({"type": "status", "status": "generating",
                               "attempt_id": aid, "game_id": game_id, "exp_id": exp_id})
        self._log(f"[harness] Generating {model_name} ({model.model_id}) / {aid} ...")

        system_text = SYSTEM_PREAMBLE_FILE.read_text().strip()
        # ~50 JPEG decode/resize/encodes + disk reads: keep them off the event loop
        parts = await asyncio.to_thread(self._build_prompt_parts, game_id, exp_id,
                                        attempt_num, game_prompt, start_b64, prev)
        conversation = [Message(role="system", parts=[TextPart(system_text)]),
                        Message(role="user", parts=parts)]

        # Persist exactly what is sent: text with [image: ...] markers + the images
        images = [p for p in parts if isinstance(p, ImagePart)]
        user_text = _render_parts(parts)
        self.storage.save_attempt_input(game_id, exp_id, aid, user_text,
                                        [(img.data, img.media_type) for img in images])
        self.storage.save_attempt_trace(game_id, exp_id, aid,
                                        _serialize_conversation(conversation))
        await self._broadcast({"type": "prompt_ready",
                               "game_id": game_id, "exp_id": exp_id, "attempt_id": aid,
                               "n_images": len(images)})

        self._log(f"[harness] Calling {model.model_id} | reasoning effort={REASONING_EFFORT} | "
                  f"attempt {attempt_num} | {len(images)} images | {len(user_text)} chars")
        try:
            code = await model.generate(conversation)
            raw_len = len(code)
            code = _strip_fences(code)
            self._log(f"[harness] Response received | raw={raw_len} chars | code={len(code)} chars")
            if not code.strip():
                raise RuntimeError("model returned an empty response")
        except Exception as exc:
            import traceback as _tb
            self._log(f"[harness] *** LLM ERROR: {exc}")
            self._log(f"[harness] {_tb.format_exc()}")
            await self._notice(f"{model_name} API call failed: {str(exc)[:300]} — "
                               f"'New Attempt' will retry {aid}.")
            self.status = "error"
            await self._broadcast({"type": "status", "status": "error", "error": str(exc)})
            return

        reasoning_text = model._last_reasoning or (
            f"[No reasoning summary returned by {model_name} ({model.model_id}).]")
        self.storage.save_attempt_reasoning(game_id, exp_id, aid, reasoning_text)
        self._log(f"[harness] Reasoning saved ({len(model._last_reasoning)} chars)"
                  if model._last_reasoning else f"[harness] Warning: {model_name} returned no reasoning summary.")

        self.storage.save_attempt_code(game_id, exp_id, aid, code)
        self.status = "ready"
        await self._broadcast({"type": "code_generated",
                               "game_id": game_id, "exp_id": exp_id, "attempt_id": aid,
                               "code": code})
        await self._broadcast({"type": "status", "status": "ready"})
        self._log(f"[harness] Code saved → {aid}")

    def _build_prompt_parts(self, game_id: str, exp_id: str, attempt_num: int,
                            game_prompt: str, start_b64: str, prev: dict | None) -> list:
        """User message for attempt N, in this order:
             <vex_api> <game_description> <first_image>
             and, for N > 1, from attempt N-1 only (all of its trials):
             <previous_code> <previous_console_logs> <previous_telemetry> <previous_images>
        """
        pcfg = self.cfg.get("prompt", {})
        max_side = int(pcfg.get("image_max_side_px", 1280))
        max_images = int(pcfg.get("max_images", 95))
        telem_interval = float(pcfg.get("telemetry_interval_sec", 1.0))

        def img(b64: str) -> ImagePart:
            return ImagePart(data=shrink_jpeg_b64(b64, max_side), media_type="image/jpeg")

        parts: list = [
            TextPart(f"<vex_api>\n{VEX_API_COMPACT_FILE.read_text().strip()}\n</vex_api>"),
            TextPart(f"<game_description>\n{game_prompt.strip()}\n</game_description>"),
            TextPart("<first_image>\nOverhead camera view of the field at the start of the game:"),
            img(start_b64),
            TextPart("</first_image>"),
        ]

        if prev is None:
            parts.append(TextPart(f"Write the complete Python program for attempt {attempt_num}."))
            return parts

        pid, n_prev = prev["id"], attempt_num - 1
        runs = self._completed_runs(game_id, exp_id, pid)[:int(self.cfg["benchmark"].get("trials_per_code", 3))]
        code = self.storage.get_attempt_code(game_id, exp_id, pid) or ""
        parts.append(TextPart(f"<previous_code attempt=\"{n_prev}\">\n{code}\n</previous_code>"))

        # Console logs, one section per trial
        logs = []
        for t, rid in enumerate(runs, 1):
            summ = self.storage.get_run_summary(game_id, exp_id, pid, rid)
            how = ("stopped at the time limit" if summ.get("timed_out")
                   else f"program exited with code {summ.get('exit_code')}")
            logs.append(f"=== Trial {t} — ran {summ.get('elapsed_sec', '?')}s, {how} ===\n"
                        f"{self.storage.get_run_console(game_id, exp_id, pid, rid).rstrip()}")
        parts.append(TextPart(f"<previous_console_logs attempt=\"{n_prev}\">\n"
                              + "\n\n".join(logs) + "\n</previous_console_logs>"))

        # Telemetry, one table per trial
        tables = []
        for t, rid in enumerate(runs, 1):
            series = self.storage.get_run_telemetry_series(game_id, exp_id, pid, rid)
            gaps = self.storage.get_run_summary(game_id, exp_id, pid, rid).get("telemetry_gaps") or []
            if series:
                tables.append(f"=== Trial {t} — one row per {telem_interval:g}s, "
                              f"t = seconds since start ===\n"
                              + "".join(f"(warning: {g})\n" for g in gaps)
                              + _telemetry_table(series, telem_interval))
            else:
                last = self.storage.get_run_telemetry(game_id, exp_id, pid, rid)
                tables.append(f"=== Trial {t} — only the final sample was recorded ===\n"
                              + (json.dumps(last, indent=2) if last else "(no telemetry captured)"))
        parts.append(TextPart(f"<previous_telemetry attempt=\"{n_prev}\">\n"
                              + "\n\n".join(tables) + "\n</previous_telemetry>"))

        # Camera frames from every trial, subsampled evenly if over the image budget
        frame_period = 1.0 / float(self.cfg["camera"].get("capture_fps", 0.5))
        frames = [(t, i, f) for t, rid in enumerate(runs, 1)
                  for i, f in enumerate(self.storage.get_run_frames(game_id, exp_id, pid, rid))]
        budget = max(0, max_images - 1)   # first image already used one
        if len(frames) > budget:
            keep = {round(k * (len(frames) - 1) / max(1, budget - 1)) for k in range(budget)}
            frames = [f for k, f in enumerate(frames) if k in keep]
        intro = (f"<previous_images attempt=\"{n_prev}\">\n"
                 f"Overhead camera frames from each trial, one every {frame_period:g}s, "
                 f"in chronological order.")
        parts.append(TextPart(intro))
        for t, i, f in frames:
            secs = f.get("t", i * frame_period)   # real capture time when recorded
            parts.append(TextPart(f"Trial {t}, t≈{secs:.0f}s:"))
            parts.append(img(f["b64"]))
        if not frames:
            parts.append(TextPart("(no frames captured)"))
        parts.append(TextPart("</previous_images>"))

        parts.append(TextPart(f"Above are the code and results of attempt {n_prev} "
                              f"({len(runs)} trial{'s' if len(runs) != 1 else ''}). "
                              f"Write the complete Python program for attempt {attempt_num}."))
        return parts

    # ── Run execution ────────────────────────────────────────────── #

    async def run_attempt(self, game_id: str, exp_id: str, attempt_id: str) -> None:
        blocker = self.run_blocker(game_id, exp_id, attempt_id)
        if blocker:
            await self._notice(blocker)
            return
        self._busy = "running"
        try:
            await self._run_attempt(game_id, exp_id, attempt_id)
        except Exception as exc:
            import traceback as _tb
            self._log(f"[harness] *** RUN ERROR: {exc}")
            self._log(f"[harness] {_tb.format_exc()}")
            await self._notice(f"Run failed: {exc}")
            self.status = "error"
            await self._broadcast({"type": "status", "status": "error", "error": str(exc)})
        finally:
            if self.camera._running:
                await self.camera.stop_capture()
            self.camera.cleanup()   # no-op after a normal run; frees temp frames after a failure
            self.live = None
            self._busy = None
            self._log_context = None
            if self.status != "error":
                self.status = "ready"
            # The UI's status badge only changes on "status" messages — always send one
            await self._broadcast({"type": "status", "status": self.status})

    def _completed_runs(self, game_id: str, exp_id: str, attempt_id: str) -> list[str]:
        """Valid trials: finished and saved (a crashed run has no summary.json) and not
        aborted with Stop. Excluded runs stay on disk and in the UI, but don't count
        toward trials_per_code and aren't sent to the model."""
        runs = self.storage._read_attempt(game_id, exp_id, attempt_id)["runs"]
        valid = []
        for r in runs:
            summ = self.storage.get_run_summary(game_id, exp_id, attempt_id, r)
            if summ and not summ.get("stopped_by_user"):
                valid.append(r)
        return valid

    async def _run_attempt(self, game_id: str, exp_id: str, attempt_id: str) -> None:
        # Fresh stop event *before* the pre-run checks, so Stop pressed during them counts
        self._emergency_stop = False
        self._stop_event = asyncio.Event()

        code = self.storage.get_attempt_code(game_id, exp_id, attempt_id)

        # An unreachable robot or a dead camera would produce a "trial" with only
        # connection errors / no frames — fed to the model as if its code had failed.
        await self.reconnect_robot()
        if not self.robot_connected:
            await self._notice(f"Robot not reachable at {self._robot_ip} — run not started.")
            return
        snap = await asyncio.get_running_loop().run_in_executor(None, self.camera.snapshot_base64)
        if not snap:
            await self._notice("Camera not delivering frames — run not started.")
            return
        if self._stop_event.is_set():
            await self._notice("Stopped before the run started.")
            return

        code_path = BENCH_DIR / ".current_run.py"
        code_path.write_text(code)

        rid = self.storage.create_run(game_id, exp_id, attempt_id)
        self._log_context = (game_id, exp_id, attempt_id)
        self.live = {"game_id": game_id, "exp_id": exp_id, "attempt_id": attempt_id, "run_id": rid}
        duration = self.cfg["benchmark"]["run_duration_sec"]
        self.status = "running"
        self._log(f"[harness] Starting run {rid} | duration={duration}s")
        await self._broadcast({"type": "status", "status": "running",
                               "game_id": game_id, "exp_id": exp_id,
                               "attempt_id": attempt_id, "run_id": rid})

        def on_console(line: str):
            self._spawn(self._broadcast({"type": "robot_log", "text": line}))

        def on_telemetry(data: dict):
            self._spawn(self._broadcast({"type": "telemetry", "data": data}))

        async def _on_frame(b64: str, frame_num: int):
            await self._broadcast({"type": "run_frame", "b64": b64,
                                   "label": f"#{frame_num}", "frame_num": frame_num})

        await self.camera.start_capture(on_frame=_on_frame)

        async def _tick():
            for elapsed in range(duration + 1):
                await self._broadcast({"type": "timer", "elapsed": elapsed, "total": duration})
                if elapsed < duration:
                    await asyncio.sleep(1)

        tick_task = asyncio.create_task(_tick())
        t0 = time.time()

        result: RunResult = await run_code(
            code_path=code_path,
            robot_ip=self._robot_ip,
            duration=duration,
            log=self._log,
            on_console=on_console,
            on_telemetry=on_telemetry,
            stop_event=self._stop_event,
        )
        elapsed_sec = time.time() - t0

        tick_task.cancel()
        try: await tick_task
        except asyncio.CancelledError: pass
        await self._broadcast({"type": "timer", "elapsed": round(elapsed_sec), "total": duration})

        # The ball may still be rolling when the program ends — keep filming briefly
        post = float(self.cfg["benchmark"].get("post_run_capture_sec", 0))
        if self._stop_event.is_set():
            post = 0
        if post > 0:
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=post)
            except asyncio.TimeoutError:
                pass

        await self.camera.stop_capture()
        frames = self.camera.collect_and_encode()
        frame_times = self.camera.frame_times()
        self.camera.cleanup()

        summary = {
            "run_id": rid, "elapsed_sec": round(elapsed_sec, 2),
            "frame_count": len(frames), "console_lines": len(result.console_lines),
            "timed_out": result.timed_out, "exit_code": result.exit_code,
            "stopped_by_user": result.stopped_by_user,
            "telemetry_gaps": result.telemetry_gaps,
        }
        expected = (elapsed_sec + post) * float(self.cfg["camera"].get("capture_fps", 0.5))
        if len(frames) < 0.7 * expected:
            self._log(f"[harness] WARNING: only {len(frames)} frames captured "
                      f"(expected ~{expected:.0f}) — check the camera.")
        for gap in result.telemetry_gaps:
            self._log(f"[harness] WARNING: {gap}")
        self.storage.save_run_results(
            game_id, exp_id, attempt_id, rid,
            frames=frames,
            telemetry_data=result.last_telemetry_dict() if result.telemetry else {},
            console_log=result.console_log,
            summary=summary,
            telemetry_series=result.telemetry_series(t0),
            frame_times=frame_times if len(frame_times) == len(frames) else None,
        )

        self.status = "ready"
        await self._broadcast({"type": "run_complete",
                               "game_id": game_id, "exp_id": exp_id,
                               "attempt_id": attempt_id, "run_id": rid,
                               "summary": summary})
        self._log(f"[harness] Run {rid} complete — {len(frames)} frames, {len(result.console_lines)} log lines.")

    # ── LLM Drive mode ───────────────────────────────────────────── #

    async def start_drive_session(self, game_id: str, exp_id: str, max_turns: int = 100) -> None:
        if self.busy_reason():
            self._log(f"[harness] {self.busy_reason()}")
            return
        self._emergency_stop = False
        game = self.storage._read_game(game_id)
        exp = self.storage._read_experiment(game_id, exp_id)
        if exp["model"] not in self._models:
            self._log(f"[drive] Unknown model: {exp['model']}")
            return
        self._busy = "driving"
        # Each drive session is stored as an attempt with a single run
        aid = self.storage.create_attempt(game_id, exp_id)["id"]
        rid = self.storage.create_run(game_id, exp_id, aid)
        self.live = {"game_id": game_id, "exp_id": exp_id, "attempt_id": aid, "run_id": rid}

        async def _guarded():
            try:
                await self._run_drive_session(game_id, exp_id, aid, rid,
                                              game["prompt"], exp["model"], max_turns)
            finally:   # also on cancellation (emergency stop)
                self.live = None
                self._busy = None
                self._log_context = None

        self._drive_task = asyncio.create_task(_guarded())

    async def _run_drive_session(self, game_id: str, exp_id: str, aid: str, rid: str,
                                 game_prompt: str, model_name: str, max_turns: int) -> None:
        model = self._models[model_name]
        self._log_context = (game_id, exp_id, aid)
        self._log(f"[drive] Starting {aid} — {model_name}, {max_turns} turns")
        self.status = "running"

        prompt = DRIVE_SYSTEM_PROMPT.format(game_prompt=game_prompt)
        conv = [Message(role="user", parts=[TextPart(prompt)])]
        self.storage.save_attempt_input(game_id, exp_id, aid, prompt, [])
        await self._broadcast({"type": "prompt_ready", "game_id": game_id, "exp_id": exp_id,
                               "attempt_id": aid, "n_images": 0})
        await self._broadcast({"type": "status", "status": "running", "game_id": game_id,
                               "exp_id": exp_id, "attempt_id": aid, "run_id": rid})

        # Session record, written to disk when the session ends for any reason
        transcript: list[str] = []
        responses: list[str] = []
        reasoning_log: list[str] = []
        photos: list[tuple[str, str]] = []
        status_rows: list[dict] = []
        end_reason = "max turns reached"
        turns_done = 0
        t0 = time.time()

        def log_turn(text: str) -> None:
            transcript.append(text)

        daemon = RobotDaemonClient(self._robot_ip, log=self._log)
        photo_requested = False
        try:
            try:
                await daemon.start()
                self._log("[drive] Daemon ready.")
            except Exception as exc:
                end_reason = f"robot daemon failed to start: {exc}"
                self._log(f"[drive] Daemon error: {exc}")
                return

            for turn in range(1, max_turns + 1):
                if self._emergency_stop:
                    break
                self._drive_turn = turn
                await self._broadcast({"type": "drive_turn", "turn": turn, "max": max_turns})

                status_r = await daemon.send({"cmd": "status"})
                scan_r   = await daemon.send({"cmd": "scan"})
                ctx = (f"Turn {turn}/{max_turns}\n"
                       f"STATUS: {json.dumps(status_r)}\nSCAN: {json.dumps(scan_r)}\n")
                await self._broadcast({"type": "robot_log", "text": ctx})
                log_turn(f"=== Turn {turn} (t={time.time() - t0:.1f}s) ===\n{ctx.rstrip()}")
                if "error" not in status_r:
                    pos = status_r.get("position") or {}
                    status_rows.append({"t": round(time.time() - t0, 2), "turn": turn,
                                        "heading": status_r.get("heading"),
                                        "robot_x": pos.get("x"), "robot_y": pos.get("y"),
                                        "battery": status_r.get("battery"),
                                        "has_ball": status_r.get("has_ball"),
                                        "has_barrel": status_r.get("has_barrel")})

                parts: list = [TextPart(ctx)]
                if turn == 1 or turn % 5 == 0 or photo_requested:
                    photo_requested = False
                    photo = await self._overhead_photo()
                    if photo:
                        parts.append(ImagePart(data=photo, media_type="image/jpeg"))
                        photos.append((photo, "image/jpeg"))
                        log_turn(f"[overhead photo → frame_{len(photos):03d}.jpg]")
                        await self._broadcast({"type": "drive_photo", "b64": photo, "turn": turn})

                conv.append(Message(role="user", parts=parts))
                _keep_recent_images(conv, DRIVE_MAX_PHOTOS)
                try:
                    resp = await model.generate(conv)
                except Exception as exc:
                    end_reason = f"LLM error: {exc}"
                    self._log(f"[drive] LLM error: {exc}")
                    break

                turns_done = turn
                responses.append(f"# Turn {turn}\n{resp}")
                if model._last_reasoning:
                    reasoning_log.append(f"=== Turn {turn} ===\n{model._last_reasoning}")
                log_turn(f"MODEL: {resp.strip()}")
                conv.append(Message(role="assistant", parts=[TextPart(resp)]))
                await self._broadcast({"type": "drive_action", "turn": turn, "text": resp})
                self._log(f"[drive] T{turn}: {resp[:200]}")

                action, params = self._parse_drive_action(resp)
                log_turn(f"ACTION: {action} {json.dumps(params)}")
                if action == "done":
                    end_reason = "model declared done"
                    self._log("[drive] Done!")
                    break
                if action == "photo":
                    photo_requested = True   # attached to the next turn's message
                    continue
                cmd = self._build_daemon_cmd(action, params)
                if cmd:
                    result = await daemon.send(cmd, timeout=45.0)
                    log_turn(f"RESULT: {json.dumps(result)}")
                    await self._broadcast({"type": "drive_result", "turn": turn,
                                          "action": action, "result": result})
        except asyncio.CancelledError:
            end_reason = "stopped by the operator"
            raise
        except Exception as exc:
            end_reason = f"aborted: {type(exc).__name__}: {exc}"
            self._log(f"[drive] *** Session aborted: {type(exc).__name__}: {exc}")
        finally:
            await daemon.stop()
            if self._emergency_stop and end_reason == "max turns reached":
                end_reason = "stopped by the operator"
            summary = {"run_id": rid, "mode": "drive", "model": model_name,
                       "elapsed_sec": round(time.time() - t0, 2), "turns": turns_done,
                       "max_turns": max_turns, "end_reason": end_reason,
                       "frame_count": len(photos), "console_lines": len(transcript),
                       "stopped_by_user": end_reason == "stopped by the operator"}
            self.storage.save_run_results(
                game_id, exp_id, aid, rid, frames=photos,
                telemetry_data={k: v for k, v in (status_rows[-1] if status_rows else {}).items() if k != "t"},
                console_log="\n".join(transcript) + f"\n=== Session ended: {end_reason} ===",
                summary=summary, telemetry_series=status_rows)
            if reasoning_log:
                self.storage.save_attempt_reasoning(game_id, exp_id, aid, "\n\n".join(reasoning_log))
            if responses:   # the model's raw replies, shown in the Code tab
                self.storage.save_attempt_code(game_id, exp_id, aid, "\n\n".join(responses))
            self.storage.save_attempt_trace(game_id, exp_id, aid, _serialize_conversation(conv))
            self.status = "ready"
            self._drive_turn = 0
            await self._broadcast({"type": "run_complete", "game_id": game_id, "exp_id": exp_id,
                                   "attempt_id": aid, "run_id": rid, "summary": summary})
            await self._broadcast({"type": "status", "status": "ready"})
            self._log(f"[drive] Session ended ({end_reason}) — {turns_done} turns, "
                      f"{len(photos)} photos saved to {aid}/{rid}.")

    def _parse_drive_action(self, text: str) -> tuple[str, dict]:
        # The reply is a JSON object with a nested "params" object, so take the outermost
        # {...} span (a no-nested-braces regex would grab "params" and lose "action").
        text = text.strip()
        m = re.search(r'```(?:json)?\s*(\{.*\})\s*```', text, re.DOTALL)
        raw = m.group(1) if m else text[text.find("{"):text.rfind("}") + 1]
        try:
            data = json.loads(raw)
            return str(data.get("action", "stop")), dict(data.get("params") or {})
        except Exception:
            return "stop", {}

    def _build_daemon_cmd(self, action: str, params: dict) -> dict | None:
        if action == "move":
            return {"cmd": "move", "distance_mm": float(params.get("distance_mm", 150)),
                    "angle_deg": float(params.get("angle_deg", 0)),
                    "velocity": float(params.get("velocity", 55))}
        if action == "turn":
            return {"cmd": "turn", "direction": str(params.get("direction", "right")),
                    "angle_deg": float(params.get("angle_deg", 45))}
        if action == "kick":
            return {"cmd": "kick", "strength": str(params.get("strength", "hard"))}
        if action in ("scan", "stop"):
            return {"cmd": action}
        return None

    async def _overhead_photo(self) -> str | None:
        loop = asyncio.get_running_loop()
        snap = await loop.run_in_executor(None, self.camera.snapshot_base64)
        return shrink_jpeg_b64(snap[0], int(self.cfg.get("prompt", {}).get("image_max_side_px", 1280))) if snap else None

    async def emergency_stop(self) -> None:
        self._emergency_stop = True
        if hasattr(self, "_stop_event") and self._stop_event:
            self._stop_event.set()
        if self._drive_task and not self._drive_task.done():
            self._drive_task.cancel()
        if self._busy == "generating":
            self._log("[harness] Stop has no effect on code generation — wait for it to finish.")
            return
        await self.camera.stop_capture()
        self._log("[harness] Emergency stop.")

    # ── Camera ───────────────────────────────────────────────────── #

    def scan_cameras(self) -> list[dict]:
        import cv2
        results = []
        for idx in range(6):
            if idx == self.camera._idx and self.camera._cap is not None:
                # Opening the device a second time can disrupt the live capture
                results.append({"index": idx, "label": f"Camera {idx} — overhead (in use)", "in_use": True})
                continue
            cap, desc = self.camera._open_verified(idx)
            if cap is not None:
                cap.release()
                results.append({"index": idx, "label": f"Camera {idx} — overhead ({desc})"})
            elif desc != "not available":
                results.append({"index": idx, "label": f"Camera {idx} — {desc}"})
        return results

    def set_camera_index(self, idx: int) -> bool:
        # Called in a worker thread by the server (open() blocks ~1s)
        return self.camera.open(index=idx)

    # ── Internal ─────────────────────────────────────────────────── #

    def _log(self, msg: str) -> None:
        import datetime as _dt
        ts = _dt.datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line)
        if self.broadcast:
            payload: dict = {"type": "log", "text": line}
            if self._log_context:
                gid, eid, aid = self._log_context
                payload["harness_log"] = True
                payload["game_id"] = gid
                payload["exp_id"] = eid
                payload["attempt_id"] = aid
                try:
                    self.storage.append_attempt_harness_log(gid, eid, aid, line)
                except Exception:
                    pass
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._broadcast(payload))
            except RuntimeError:
                # Called from a worker thread (e.g. camera executor) — there is no loop
                # in this thread, so hand the broadcast to the main loop.
                if self._loop and not self._loop.is_closed():
                    self._loop.call_soon_threadsafe(
                        lambda p=payload: self._loop.create_task(self._broadcast(p)))

    def _camera_failed(self) -> None:
        # Runs in a camera worker thread — hand the notice to the event loop
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(lambda: self._loop.create_task(self._notice(
                "The overhead camera stopped (usually the Mac slept or the USB cable "
                "dropped). Click 'Restart server' on the Config page to reconnect it.")))

    async def _notice(self, text: str) -> None:
        """Log + show to the operator (UI pops it up on every page)."""
        self._log(f"[harness] {text}")
        await self._broadcast({"type": "notice", "text": text})

    def _spawn(self, coro) -> None:
        # The loop only weakly references tasks; hold them until done so they aren't GC'd
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _broadcast(self, data: dict) -> None:
        if self.broadcast:
            try:
                await self.broadcast(data)
            except Exception:
                pass


DRIVE_MAX_PHOTOS = 10   # photos kept in a drive conversation (providers cap images/request)


def _keep_recent_images(conv: list, keep: int) -> None:
    """Replace all but the newest `keep` images in the conversation with a placeholder."""
    seen = 0
    for msg in reversed(conv):
        for i in range(len(msg.parts) - 1, -1, -1):
            if isinstance(msg.parts[i], ImagePart):
                seen += 1
                if seen > keep:
                    msg.parts[i] = TextPart("[older overhead photo omitted]")


def _render_parts(parts: list) -> str:
    """Text of a message with each image shown as an [image: img_NNN.jpg] marker,
    matching the file names Storage.save_attempt_input gives the images."""
    out, n = [], 0
    for p in parts:
        if isinstance(p, ImagePart):
            n += 1
            out.append(f"[image: img_{n:03d}.jpg]")
        else:
            out.append(p.text)
    return "\n".join(out)


def _serialize_conversation(conversation: list) -> str:
    """Render the full conversation to a human-readable text for the Trace tab."""
    return "\n\n".join(f"{'='*60}\n[{msg.role.upper()}]\n{_render_parts(msg.parts)}"
                       for msg in conversation)


def _telemetry_table(series: list[dict], interval: float) -> str:
    """CSV of telemetry samples, keeping the first sample in each `interval` window."""
    rows, next_t = [], 0.0
    for row in series:
        if row.get("t", 0) >= next_t:
            rows.append(row)
            next_t = row.get("t", 0) + interval
    if series and rows[-1] is not series[-1]:
        rows.append(series[-1])   # always include the final state
    cols = list(rows[0].keys())
    return "\n".join([",".join(cols)] +
                     [",".join(str(r.get(c, "")) for c in cols) for r in rows])


def _strip_fences(code: str) -> str:
    code = code.strip()
    # Extract fenced code anywhere in the response (handles leading prose). If there
    # are several blocks, the program is the longest one, not e.g. a usage snippet.
    blocks = re.findall(r"```[a-zA-Z0-9_+-]*[ \t]*\n(.*?)```", code, re.DOTALL)
    if blocks:
        return max(blocks, key=len).strip()
    # If no fences, strip any leading non-Python prose (lines before first import/from/def/class/robot)
    lines = code.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped and (
            stripped.startswith(("from ", "import ", "def ", "class ", "robot", "#", "if ", "for ", "while "))
        ):
            return "\n".join(lines[i:]).strip()
    return code
