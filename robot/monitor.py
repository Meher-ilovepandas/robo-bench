"""
Read console logs and telemetry from the VEX AIM robot.

Primary source: BLE (TXUser for console, RC_Status/RC_AI_Status for telemetry).
Fallback:       USB serial or WiFi streaming.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Callable

import httpx
import serial
import serial.tools.list_ports

from .transfer import find_vex_serial_port


@dataclass
class Telemetry:
    timestamp: float = field(default_factory=time.time)
    raw: dict = field(default_factory=dict)

    def to_text(self) -> str:
        if not self.raw:
            return "(no telemetry)"
        return "\n".join(f"  {k}: {v}" for k, v in self.raw.items())


class RobotMonitor:
    """
    Collects console lines and telemetry during a run.
    Works with BLE driver (primary) or serial fallback.
    """

    def __init__(self, cfg: dict, ble=None,
                 log: Callable[[str], None] | None = None):
        self._cfg = cfg["robot"]
        self._ble = ble      # VexAimBLE — set externally
        self._log = log or print
        self._console_lines: list[str] = []
        self._telemetry: list[Telemetry] = []
        self._running = False
        self._task: asyncio.Task | None = None

    def set_ble(self, ble) -> None:
        self._ble = ble

    # ------------------------------------------------------------------ #
    #  Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        self._console_lines = []
        self._telemetry = []
        self._running = True

        if self._ble and self._ble.is_connected:
            # Chain onto any existing callbacks (e.g. web broadcast) rather than replace them
            prev_console = self._ble.on_console
            prev_telem   = self._ble.on_telemetry

            def _console_cb(line: str):
                self._handle_console_line(line)
                if prev_console:
                    prev_console(line)

            def _telem_cb(data: dict):
                self._handle_telemetry_dict(data)
                if prev_telem:
                    prev_telem(data)

            self._ble.on_console   = _console_cb
            self._ble.on_telemetry = _telem_cb
            await self._ble.start_monitoring()
        else:
            # Serial / WiFi fallback
            self._task = asyncio.create_task(self._read_loop())

        self._log("[monitor] Started.")

    async def stop(self) -> None:
        self._running = False
        if self._ble and self._ble.is_connected:
            await self._ble.stop_monitoring()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._log(f"[monitor] Stopped. {len(self._console_lines)} lines, "
                  f"{len(self._telemetry)} telemetry samples.")

    def drain(self) -> tuple[list[str], list[Telemetry]]:
        return list(self._console_lines), list(self._telemetry)

    # ------------------------------------------------------------------ #
    #  BLE callbacks                                                       #
    # ------------------------------------------------------------------ #

    def _handle_console_line(self, line: str) -> None:
        self._console_lines.append(line)

    def _handle_telemetry_dict(self, data: dict) -> None:
        self._telemetry.append(Telemetry(raw=data))

    # ------------------------------------------------------------------ #
    #  Serial / WiFi fallback read loop                                    #
    # ------------------------------------------------------------------ #

    async def _read_loop(self) -> None:
        if self._cfg.get("wifi_ip"):
            await self._read_wifi()
        else:
            port = self._cfg.get("serial_port") or find_vex_serial_port()
            if port:
                await self._read_serial(port)

    async def _read_serial(self, port: str) -> None:
        import serial as _serial
        baud = self._cfg.get("serial_baud", 115200)
        loop = asyncio.get_event_loop()
        buf  = ""
        try:
            ser = await loop.run_in_executor(
                None, lambda: _serial.Serial(port, baud, timeout=0.1)
            )
        except Exception as exc:
            self._log(f"[monitor] Serial open error: {exc}")
            return
        try:
            while self._running:
                data = await loop.run_in_executor(None, lambda: ser.read(256))
                if data:
                    buf += data.decode(errors="replace")
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        self._handle_raw_line(line.rstrip())
                else:
                    await asyncio.sleep(0.05)
        finally:
            ser.close()

    async def _read_wifi(self) -> None:
        ip   = self._cfg["wifi_ip"]
        port = self._cfg.get("wifi_port", 8765)
        url  = f"http://{ip}:{port}/console"
        buf  = ""
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("GET", url) as resp:
                    async for chunk in resp.aiter_text():
                        if not self._running:
                            break
                        buf += chunk
                        while "\n" in buf:
                            line, buf = buf.split("\n", 1)
                            self._handle_raw_line(line.rstrip())
        except Exception as exc:
            self._log(f"[monitor] WiFi read error: {exc}")

    def _handle_raw_line(self, line: str) -> None:
        if line.startswith("TELEMETRY:"):
            try:
                data = json.loads(line[len("TELEMETRY:"):].strip())
                self._handle_telemetry_dict(data)
            except json.JSONDecodeError:
                self._console_lines.append(line)
        else:
            self._console_lines.append(line)

    # ------------------------------------------------------------------ #
    #  Formatting                                                          #
    # ------------------------------------------------------------------ #

    @staticmethod
    def format_console_log(lines: list[str]) -> str:
        return "\n".join(lines) if lines else "(no console output)"

    @staticmethod
    def format_telemetry(samples: list[Telemetry]) -> str:
        if not samples:
            return "(no telemetry captured)"
        last = samples[-1]
        return f"Total snapshots: {len(samples)}\nLatest:\n{last.to_text()}"
