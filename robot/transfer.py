"""
Upload Python code to the VEX AIM robot.

Strategy (tried in order):
  1. BLE (Bluetooth Low Energy) — primary for VEX AIM
  2. WiFi REST API              — if robot.wifi_ip is set
  3. USB serial                 — fallback
  4. VEX CLI tool               — last resort
"""
from __future__ import annotations

import asyncio
import glob
import shutil
from pathlib import Path
from typing import Callable

import httpx
import serial
import serial.tools.list_ports

VEX_USB_VIDS = {0x2888}


def find_vex_serial_port() -> str | None:
    for port in serial.tools.list_ports.comports():
        if port.vid and port.vid in VEX_USB_VIDS:
            return port.device
    for pat in ["/dev/cu.usbmodem*", "/dev/cu.VEX*", "/dev/tty.usbmodem*"]:
        matches = glob.glob(pat)
        if matches:
            return matches[0]
    return None


class RobotTransfer:
    def __init__(self, cfg: dict, ble=None,
                 log: Callable[[str], None] | None = None):
        self._cfg = cfg["robot"]
        self._ble = ble     # VexAimBLE instance (set externally)
        self._log = log or print

    def set_ble(self, ble) -> None:
        self._ble = ble

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    async def upload(self, code_path: Path) -> bool:
        code_bytes = code_path.read_bytes()
        filename   = code_path.name

        # 1. BLE (primary for VEX AIM)
        if self._ble and self._ble.is_connected:
            ok = await self._ble.upload_code(code_bytes, filename)
            if ok:
                return True

        # 2. WiFi REST API
        if self._cfg.get("wifi_ip"):
            ok = await self._upload_wifi(code_path)
            if ok:
                return True

        # 3. USB serial
        port = self._cfg.get("serial_port") or find_vex_serial_port()
        if port:
            ok = await self._upload_serial(code_path, port)
            if ok:
                return True

        # 4. VEX CLI tool
        cli = self._cfg.get("vex_cli")
        if cli and shutil.which(cli):
            ok = await self._upload_cli(code_path, cli)
            if ok:
                return True

        self._log("[robot] ERROR: All upload methods failed.")
        return False

    async def stop(self) -> None:
        """Stop the currently running program."""
        if self._ble and self._ble.is_connected:
            await self._ble.stop_program()
            return
        if self._cfg.get("wifi_ip"):
            await self._wifi_command("stop")
            return
        port = self._cfg.get("serial_port") or find_vex_serial_port()
        if port:
            await self._serial_command(port, b"\x03")

    # ------------------------------------------------------------------ #
    #  WiFi                                                                #
    # ------------------------------------------------------------------ #

    async def _upload_wifi(self, code_path: Path) -> bool:
        ip   = self._cfg["wifi_ip"]
        port = self._cfg.get("wifi_port", 8765)
        url  = f"http://{ip}:{port}/upload"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.put(
                    url,
                    content=code_path.read_bytes(),
                    headers={"Content-Type": "text/x-python",
                             "X-Filename":   code_path.name},
                )
            if resp.status_code == 200:
                self._log(f"[robot] WiFi upload OK: {code_path.name}")
                return True
            self._log(f"[robot] WiFi upload HTTP {resp.status_code}")
        except Exception as exc:
            self._log(f"[robot] WiFi upload error: {exc}")
        return False

    # ------------------------------------------------------------------ #
    #  Serial                                                              #
    # ------------------------------------------------------------------ #

    async def _upload_serial(self, code_path: Path, port: str) -> bool:
        self._log(f"[robot] Serial upload on {port} ...")
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._serial_upload_sync, code_path, port)
            self._log(f"[robot] Serial upload OK: {code_path.name}")
            return True
        except Exception as exc:
            self._log(f"[robot] Serial upload error: {exc}")
            return False

    def _serial_upload_sync(self, code_path: Path, port: str) -> None:
        import time, base64
        baud = self._cfg.get("serial_baud", 115200)
        code_bytes = code_path.read_bytes()
        with serial.Serial(port, baud, timeout=3) as ser:
            ser.write(b"\x03\x03")
            time.sleep(0.5)
            ser.reset_input_buffer()
            ser.write(b"\x01")   # raw REPL
            time.sleep(0.2)
            ser.read(ser.in_waiting or 1)
            b64 = base64.b64encode(code_bytes).decode()
            script = (
                f"import base64\n"
                f"_d = base64.b64decode('{b64}')\n"
                f"with open('/flash/{code_path.name}', 'wb') as _f:\n"
                f"    _f.write(_d)\n"
                f"print('UPLOAD_OK')\n"
            ).encode()
            ser.write(script)
            ser.write(b"\x04")
            time.sleep(2)
            out = ser.read(ser.in_waiting or 256).decode(errors="replace")
            if "UPLOAD_OK" not in out:
                raise RuntimeError(f"Not confirmed: {out!r}")
            ser.write(b"\x02")

    # ------------------------------------------------------------------ #
    #  CLI                                                                 #
    # ------------------------------------------------------------------ #

    async def _upload_cli(self, code_path: Path, cli: str) -> bool:
        self._log(f"[robot] CLI upload: {cli}")
        try:
            proc = await asyncio.create_subprocess_exec(
                cli, "upload", str(code_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode == 0:
                return True
            self._log(f"[robot] CLI failed: {stderr.decode()}")
        except Exception as exc:
            self._log(f"[robot] CLI error: {exc}")
        return False

    # ------------------------------------------------------------------ #
    #  Utility                                                             #
    # ------------------------------------------------------------------ #

    async def _wifi_command(self, cmd: str) -> None:
        ip   = self._cfg["wifi_ip"]
        port = self._cfg.get("wifi_port", 8765)
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(f"http://{ip}:{port}/{cmd}")
        except Exception:
            pass

    async def _serial_command(self, port: str, data: bytes) -> None:
        try:
            baud = self._cfg.get("serial_baud", 115200)
            loop = asyncio.get_event_loop()
            def _send():
                with serial.Serial(port, baud, timeout=1) as ser:
                    ser.write(data)
            await loop.run_in_executor(None, _send)
        except Exception:
            pass
