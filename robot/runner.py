"""
Execute LLM-generated VEX AIM code locally and collect results.

The VEX AIM WebSocket API runs the program on the HOST, not on the robot.
The vex library (bench/vex/) sends WebSocket commands to the robot at robot_ip.

This module:
  - Runs the generated Python file as a timed subprocess
  - Captures stdout as the console log
  - Records telemetry from the program's own ws_status client (robot/launcher.py)
  - Kills the process when the trial duration expires
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Callable

BENCH_DIR = Path(__file__).parent.parent


def _settings_path() -> Path:
    return BENCH_DIR / "vex" / "settings.json"


def _write_robot_ip(robot_ip: str) -> None:
    """Update vex/settings.json with the current robot IP."""
    path = _settings_path()
    path.write_text(json.dumps({"connection": {"host": robot_ip}}, indent=4))


# ---------------------------------------------------------------------------
# WebSocket telemetry collector
# ---------------------------------------------------------------------------

class TelemetrySample:
    __slots__ = ("timestamp", "raw")

    def __init__(self, raw: dict, timestamp: float | None = None):
        self.timestamp = timestamp if timestamp is not None else time.time()
        self.raw = raw

    def to_series_row(self, t0: float) -> dict:
        return {"t": round(self.timestamp - t0, 2), **self.to_dict()}

    def to_dict(self) -> dict:
        """Flatten status JSON into a flat telemetry dict."""
        robot = self.raw.get("robot", {})
        ai    = self.raw.get("aivision", {})
        accel = robot.get("acceleration", {})
        gyro  = robot.get("gyro_rate", {})
        objects = ai.get("objects", {})
        first_obj = (objects.get("items") or [{}])[0]
        return {
            "heading":   robot.get("heading", 0),
            "pitch":     robot.get("pitch", 0),
            "roll":      robot.get("roll", 0),
            "yaw":       robot.get("yaw", 0),
            "robot_x":   robot.get("robot_x", 0),
            "robot_y":   robot.get("robot_y", 0),
            "battery":   robot.get("battery", 0),
            "accel_x":   accel.get("x", 0),
            "accel_y":   accel.get("y", 0),
            "accel_z":   accel.get("z", 0),
            "gyro_x":    gyro.get("x", 0),
            "gyro_z":    gyro.get("z", 0),
            "ai_objects": objects.get("count", 0),
            "ai_type":    first_obj.get("type_str", ""),
            "ai_x":       first_obj.get("originx", 0),
            "ai_y":       first_obj.get("originy", 0),
            "ai_score":   first_obj.get("score", 0),
        }

    def to_text(self) -> str:
        d = self.to_dict()
        return "\n".join(f"  {k}: {v}" for k, v in d.items())


GAP_SEC = 1.5   # silence longer than this between status samples is reported


async def _tail_telemetry(
    path: Path,
    stop_event: asyncio.Event,
    samples: list[TelemetrySample],
    on_sample: Callable[[dict], None] | None = None,
) -> None:
    """Follow the JSON-lines file robot/launcher.py writes, until stop_event is set."""
    pos = 0
    buf = ""
    while True:
        stopping = stop_event.is_set()   # read once more after stop to catch the tail
        try:
            with path.open() as f:
                f.seek(pos)
                chunk = f.read()
                pos = f.tell()
        except FileNotFoundError:
            chunk = ""
        buf += chunk
        *lines, buf = buf.split("\n")
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            sample = TelemetrySample(rec.get("status") or {}, rec.get("ts"))
            samples.append(sample)
            if on_sample:
                on_sample(sample.to_dict())
        if stopping:
            return
        await asyncio.sleep(0.2)


def _telemetry_gaps(samples: list[TelemetrySample], t0: float, t_end: float) -> list[str]:
    gaps = []
    for a, b in zip(samples, samples[1:]):
        if b.timestamp - a.timestamp > GAP_SEC:
            gaps.append(f"no telemetry from t={a.timestamp - t0:.1f}s to t={b.timestamp - t0:.1f}s")
    if samples and t_end - samples[-1].timestamp > GAP_SEC:
        gaps.append(f"no telemetry from t={samples[-1].timestamp - t0:.1f}s until the program "
                    f"ended at t={t_end - t0:.1f}s")
    return gaps


# ---------------------------------------------------------------------------
# Code runner
# ---------------------------------------------------------------------------

class RunResult:
    def __init__(self):
        self.console_lines: list[str] = []
        self.telemetry: list[TelemetrySample] = []
        self.exit_code: int | None = None
        self.timed_out: bool = False
        self.stopped_by_user: bool = False
        self.telemetry_gaps: list[str] = []

    @property
    def console_log(self) -> str:
        return "\n".join(self.console_lines) if self.console_lines else "(no console output)"

    @property
    def telemetry_text(self) -> str:
        if not self.telemetry:
            return "(no telemetry captured)"
        last = self.telemetry[-1]
        return f"Total snapshots: {len(self.telemetry)}\nLatest:\n{last.to_text()}"

    def last_telemetry_dict(self) -> dict:
        return self.telemetry[-1].to_dict() if self.telemetry else {}

    def telemetry_series(self, t0: float) -> list[dict]:
        return [s.to_series_row(t0) for s in self.telemetry]


async def run_code(
    code_path: Path,
    robot_ip: str,
    duration: int,
    log: Callable[[str], None] | None = None,
    on_console: Callable[[str], None] | None = None,
    on_telemetry: Callable[[dict], None] | None = None,
    stop_event: asyncio.Event | None = None,
) -> RunResult:
    """
    Run the generated Python file as a subprocess for `duration` seconds,
    capturing stdout as console log and ws_status as telemetry.

    Args:
        code_path:    Path to the generated .py file.
        robot_ip:     IP of the robot (e.g. "192.168.4.1").
        duration:     Trial duration in seconds; process is killed after this.
        log:          Harness logger (for status messages).
        on_console:   Called for each stdout line (live streaming to web UI).
        on_telemetry: Called for each telemetry sample dict (live streaming).

    Returns:
        RunResult with console_lines and telemetry samples.
    """
    _log = log or print
    result = RunResult()

    # Write robot IP to vex/settings.json so the vex library picks it up
    _write_robot_ip(robot_ip)

    # Build environment: code runs from bench dir so `from vex import Robot` works
    env = os.environ.copy()
    env["PYTHONPATH"] = str(BENCH_DIR) + (
        os.pathsep + env.get("PYTHONPATH", "")
    )

    _log(f"[runner] Starting {code_path.name} (robot={robot_ip}, duration={duration}s)")

    import tempfile
    telem_fd, telem_name = tempfile.mkstemp(prefix="vex_telemetry_", suffix=".jsonl")
    os.close(telem_fd)
    telem_path = Path(telem_name)
    env["BENCH_TELEMETRY_FILE"] = telem_name
    t0 = time.time()

    # Start the subprocess. -u is essential: with a piped stdout Python block-buffers
    # print() output, and whatever is still buffered when we kill the process at the
    # time limit is lost — i.e. the most important end-of-run log lines.
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", str(BENCH_DIR / "robot" / "launcher.py"), str(code_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
            cwd=str(BENCH_DIR),
            limit=1 << 20,   # tolerate very long print() lines (default 64 KiB raises)
        )
    except Exception as exc:
        _log(f"[runner] Failed to start subprocess: {exc}")
        result.console_lines.append(f"ERROR: {exc}")
        telem_path.unlink(missing_ok=True)
        return result

    stop_telem = asyncio.Event()

    async def _read_stdout():
        assert proc.stdout is not None
        while True:
            line_bytes = await proc.stdout.readline()
            if not line_bytes:
                break
            line = line_bytes.decode(errors="replace").rstrip()
            result.console_lines.append(line)
            if on_console:
                on_console(line)

    # Run stdout reader + telemetry collector + timer in parallel
    stdout_task = asyncio.create_task(_read_stdout())
    telem_task  = asyncio.create_task(
        _tail_telemetry(telem_path, stop_telem, result.telemetry, on_telemetry)
    )

    # Wait for process to finish, duration to expire, or stop_event to be set
    proc_done = asyncio.create_task(proc.wait())
    stop_wait = asyncio.create_task(
        (stop_event.wait() if stop_event else asyncio.sleep(float("inf")))
    )
    try:
        done, _ = await asyncio.wait(
            [proc_done, stop_wait],
            timeout=duration,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stop_wait in done:
            _log("[runner] Emergency stop — killing process.")
            result.stopped_by_user = True
        elif proc_done in done:
            result.exit_code = proc.returncode
            _log(f"[runner] Process exited (code={result.exit_code}) before duration ended.")
        else:
            # timeout
            result.timed_out = True
            _log(f"[runner] {duration}s elapsed — terminating process ...")
    finally:
        proc_done.cancel()
        stop_wait.cancel()
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                pass
        result.exit_code = proc.returncode
    t_end = time.time()

    # Stop telemetry collection
    stop_telem.set()
    await asyncio.gather(stdout_task, telem_task, return_exceptions=True)
    telem_path.unlink(missing_ok=True)
    result.telemetry_gaps = _telemetry_gaps(result.telemetry, t0, t_end)

    _log(
        f"[runner] Done — {len(result.console_lines)} log lines, "
        f"{len(result.telemetry)} telemetry samples."
    )
    return result


# ---------------------------------------------------------------------------
# WebSocket connectivity check
# ---------------------------------------------------------------------------

async def ping_robot(robot_ip: str, timeout: float = 4.0) -> bool:
    """Return True if the robot's ws_status endpoint is reachable."""
    def _ping() -> bool:
        try:
            import websocket
            uri = f"ws://{robot_ip}/ws_status"
            ws = websocket.WebSocket()
            ws.connect(uri, timeout=timeout)
            ws.close()
            return True
        except Exception:
            return False
    # Blocking connect — keep it off the event loop
    return await asyncio.to_thread(_ping)
