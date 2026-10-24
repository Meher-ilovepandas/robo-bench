"""
VEX AIM Bluetooth Low Energy (BLE) driver.

Discovered GATT map (from live robot exploration):
  Service 08590f7e-db05-467e-8757-72f6faeb13d5  (Programming channel)
    13f5  RXData        write / write-without-response   → send commands/data TO robot
    1306  TXData        notify / indicate                ← receive data FROM robot
    1326  RXUser        write / write-without-response   → send user text commands
    1316  TXUser        notify                           ← console log lines from robot
    13e5  Code          write / read / write-without-response  ↔ code management

  Service 6faa5911-9200-49a0-a353-6152c5930ec5  (Control)
    0ec5  Command       write / notify / read            ↔ start/stop/control

  Service 6c7851a0-adf7-4de6-881d-1ce2cd0fecd9  (Sensors / RC)
    ece9  RC_Cmd        write / notify / read
    ecf9  RC_Status     notify                           ← IMU + motion sensor packets
    ecc9  RC_AI_Status  notify                           ← AI/camera detection packets
    ecb9  VEXLink       write / notify / read

RC_AI_Status packet (22 bytes, little-endian):
  u8[0]  status (1 = active)
  u8[1]  reserved
  u8[2]  camera status
  u8[3]  num_objects
  u8[4]  reserved
  u8[5]  class_id  (e.g. 0=none, 4=target?)
  u16[3] x           bounding-box centre x (pixels)
  u16[4] y           bounding-box centre y (pixels)
  u16[5] width       bounding-box width
  u16[6] height      bounding-box height
  u16[7] confidence  0-100
  u16[8..10] reserved

RC_Status packet (72 bytes):
  u8[0..31]   zeros / status flags
  f32[8..14]  sensor floats (accel, gyro, heading)
    offset 32: heading/quat component
    offset 36: quat/pitch
    offset 40: accel x
    offset 44: accel y
    offset 48: accel z  (~-1.0 when flat = 1g)
    offset 52: gyro x
    offset 56: gyro z  (~-1.0 when upright)

Code upload protocol (empirically determined attempt — framed write):
  1. Write to Code char:  [0x01][filename_len u8][filename bytes][file_len u32 LE][code bytes]
     In 512-byte chunks if needed.
  2. Final write: [0x02] (EOM / execute).
  Confirmation comes on TXData or TXUser notification.
  Fallback: write code as raw bytes to Code char in chunks; robot may accept raw Python.
"""
from __future__ import annotations

import asyncio
import struct
import time
from dataclasses import dataclass, field
from typing import Callable

try:
    from bleak import BleakClient, BleakScanner
    HAS_BLEAK = True
except ImportError:
    HAS_BLEAK = False

# ── Characteristic UUIDs ──────────────────────────────────────────
SVC_PROG = "08590f7e-db05-467e-8757-72f6faeb13d5"
CHAR_RX_DATA  = "08590f7e-db05-467e-8757-72f6faeb13f5"  # write to robot
CHAR_TX_DATA  = "08590f7e-db05-467e-8757-72f6faeb1306"  # notify from robot
CHAR_RX_USER  = "08590f7e-db05-467e-8757-72f6faeb1326"  # write user commands
CHAR_TX_USER  = "08590f7e-db05-467e-8757-72f6faeb1316"  # console logs
CHAR_CODE     = "08590f7e-db05-467e-8757-72f6faeb13e5"  # code R/W

CHAR_COMMAND  = "6faa5920-9200-49a0-a353-6152c5930ec5"  # control

CHAR_RC_STATUS   = "6c7851a0-adf7-4de6-881d-1ce2cd0fecf9"  # IMU
CHAR_AI_STATUS   = "6c7851a0-adf7-4de6-881d-1ce2cd0fecc9"  # AI detection
CHAR_VEXLINK     = "6c7851a0-adf7-4de6-881d-1ce2cd0fecb9"

VEX_AIM_NAME = "VEX_AIM"
CHUNK_SIZE   = 512  # bytes per BLE write (negotiated MTU is usually 512 for newer devices)

# ── Data structures ───────────────────────────────────────────────

@dataclass
class AISample:
    timestamp: float = field(default_factory=time.time)
    status: int = 0
    num_objects: int = 0
    class_id: int = 0
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    confidence: int = 0

    @classmethod
    def from_bytes(cls, data: bytes) -> "AISample":
        if len(data) < 16:
            return cls()
        return cls(
            status      = data[0],
            num_objects = data[3],
            class_id    = data[5],
            x           = struct.unpack_from("<H", data, 6)[0],
            y           = struct.unpack_from("<H", data, 8)[0],
            width       = struct.unpack_from("<H", data, 10)[0],
            height      = struct.unpack_from("<H", data, 12)[0],
            confidence  = struct.unpack_from("<H", data, 14)[0],
        )

    def to_dict(self) -> dict:
        return {
            "ai_status":      self.status,
            "ai_objects":     self.num_objects,
            "ai_class":       self.class_id,
            "ai_x":           self.x,
            "ai_y":           self.y,
            "ai_width":       self.width,
            "ai_height":      self.height,
            "ai_confidence":  self.confidence,
        }


@dataclass
class IMUSample:
    timestamp: float = field(default_factory=time.time)
    heading: float = 0.0
    pitch: float = 0.0
    accel_x: float = 0.0
    accel_y: float = 0.0
    accel_z: float = 0.0
    gyro_x: float = 0.0
    gyro_z: float = 0.0

    @classmethod
    def from_bytes(cls, data: bytes) -> "IMUSample":
        if len(data) < 60:
            return cls()
        def f(offset):
            return round(struct.unpack_from("<f", data, offset)[0], 4)
        return cls(
            heading = f(32),
            pitch   = f(36),
            accel_x = f(40),
            accel_y = f(44),
            accel_z = f(48),
            gyro_x  = f(52),
            gyro_z  = f(56),
        )

    def to_dict(self) -> dict:
        return {
            "heading": self.heading,
            "pitch":   self.pitch,
            "accel_x": self.accel_x,
            "accel_y": self.accel_y,
            "accel_z": self.accel_z,
            "gyro_x":  self.gyro_x,
            "gyro_z":  self.gyro_z,
        }


# ── Main BLE driver ───────────────────────────────────────────────

class VexAimBLE:
    """
    Manages the BLE connection to one VEX AIM robot.

    Usage:
        ble = VexAimBLE(log=print)
        await ble.connect()
        ok = await ble.upload_code(path)
        await ble.start_monitoring()
        ...
        await ble.stop()
        await ble.disconnect()
    """

    def __init__(self, address: str | None = None,
                 log: Callable[[str], None] | None = None):
        if not HAS_BLEAK:
            raise RuntimeError("bleak is not installed. Run: pip3 install bleak")
        self.address = address         # if None, auto-scan
        self._log = log or print
        self._client: BleakClient | None = None
        self._monitoring = False

        # Collected telemetry
        self._ai_samples:  list[AISample]  = []
        self._imu_samples: list[IMUSample] = []
        self._console_lines: list[str]     = []

        # Callbacks for live streaming to web UI
        self.on_telemetry: Callable[[dict], None] | None = None
        self.on_console:   Callable[[str],  None] | None = None
        self._console_buf = ""

    # ── Connection ────────────────────────────────────────────── #

    async def find_and_connect(self) -> bool:
        if self.address:
            return await self._connect(self.address)

        self._log("[ble] Scanning for VEX_AIM ...")
        devices = await BleakScanner.discover(timeout=5.0, return_adv=True)
        for addr, (device, _adv) in devices.items():
            if device.name and VEX_AIM_NAME in device.name:
                self._log(f"[ble] Found {device.name} at {addr}")
                self.address = addr
                return await self._connect(addr)

        self._log("[ble] VEX_AIM not found during scan.")
        return False

    async def _connect(self, address: str) -> bool:
        try:
            self._client = BleakClient(address, timeout=10.0)
            await self._client.connect()
            self._log(f"[ble] Connected to VEX AIM ({address})")
            return True
        except Exception as exc:
            self._log(f"[ble] Connection failed: {exc}")
            self._client = None
            return False

    async def disconnect(self) -> None:
        if self._client and self._client.is_connected:
            await self._client.disconnect()
            self._log("[ble] Disconnected from VEX AIM.")
        self._client = None

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    # ── Code upload ───────────────────────────────────────────── #

    async def upload_code(self, code: bytes, filename: str = "main.py") -> bool:
        """
        Upload Python code to the robot.

        Tries three methods in order:
          1. MicroPython raw REPL via RXUser (most reliable for MicroPython devices)
          2. Framed protocol to Code char (header + chunked code + EOM as separate write)
          3. Raw chunked write to Code char
        Returns True if any method succeeds.
        """
        if not self.is_connected:
            self._log("[ble] Cannot upload: not connected.")
            return False

        self._log(f"[ble] Uploading {filename} ({len(code)} bytes) ...")

        # Subscribe to TXData and TXUser to watch for confirmation
        confirmed = asyncio.Event()
        conf_msg: list[str] = []

        def _tx_data_cb(sender, data: bytes):
            hex_str = data.hex()
            conf_msg.append(hex_str)
            # Any non-deadface response is considered a signal
            if hex_str != "deadface":
                confirmed.set()

        def _tx_user_cb(sender, data: bytes):
            txt = data.decode(errors="replace")
            conf_msg.append(txt)
            if txt.strip():
                confirmed.set()

        for uuid, cb in [(CHAR_TX_DATA, _tx_data_cb), (CHAR_TX_USER, _tx_user_cb)]:
            try:
                await self._client.start_notify(uuid, cb)
            except Exception:
                pass

        ok = False
        try:
            # Method 1: MicroPython raw REPL via RXUser — confirmed by UPLOAD_OK on TXUser
            await self._upload_repl(code, filename)
            try:
                await asyncio.wait_for(confirmed.wait(), timeout=4.0)
                # confirmed.set() fired — check if UPLOAD_OK came back
                repl_ok = any("UPLOAD_OK" in m for m in conf_msg)
                if repl_ok:
                    self._log("[ble] REPL upload confirmed.")
                    ok = True
                else:
                    self._log(f"[ble] REPL response (not UPLOAD_OK): {conf_msg[:2]}")
            except asyncio.TimeoutError:
                self._log("[ble] REPL: no confirmation — trying framed protocol.")

            # Method 2: Framed binary protocol to Code char
            if not ok:
                confirmed.clear()
                conf_msg.clear()
                ok = await self._upload_framed(code, filename)
                if ok:
                    try:
                        await asyncio.wait_for(confirmed.wait(), timeout=3.0)
                        self._log(f"[ble] Framed confirmation: {conf_msg[:2]}")
                    except asyncio.TimeoutError:
                        self._log("[ble] Framed: no confirmation (may still be OK).")

            # Method 3: Raw chunked write to Code char
            if not ok:
                ok = await self._upload_raw_chunks(code)

        finally:
            for uuid in [CHAR_TX_DATA, CHAR_TX_USER]:
                try:
                    await self._client.stop_notify(uuid)
                except Exception:
                    pass

        if ok:
            self._log("[ble] Upload complete.")
        return ok

    async def _upload_repl(self, code: bytes, filename: str) -> bool:
        """
        Upload via MicroPython raw REPL on RXUser characteristic.
        Protocol:
          - Ctrl+C x2  (interrupt running program)
          - Ctrl+A     (enter raw REPL mode)
          - Send Python script that writes the file to /flash/<filename>
          - Ctrl+D     (execute)
        """
        try:
            import base64
            b64 = base64.b64encode(code).decode()
            script = (
                f"import base64\n"
                f"_d=base64.b64decode('{b64}')\n"
                f"f=open('/flash/{filename}','wb')\n"
                f"f.write(_d)\n"
                f"f.close()\n"
                f"print('UPLOAD_OK:{filename}')\n"
            ).encode()

            async def _send(data: bytes) -> None:
                for i in range(0, len(data), 20):  # BLE MTU safe chunk
                    await self._client.write_gatt_char(
                        CHAR_RX_USER, data[i:i + 20], response=False
                    )
                    await asyncio.sleep(0.005)

            self._log("[ble] REPL upload: interrupting ...")
            await _send(b"\x03\x03")          # Ctrl+C x2
            await asyncio.sleep(0.3)
            await _send(b"\x01")              # raw REPL mode
            await asyncio.sleep(0.2)
            await _send(script)               # code
            await _send(b"\x04")              # execute (Ctrl+D)
            await asyncio.sleep(0.5)
            self._log(f"[ble] REPL upload sent ({len(script)} bytes).")
            return True
        except Exception as exc:
            self._log(f"[ble] REPL upload error: {exc}")
            return False

    async def _upload_framed(self, code: bytes, filename: str) -> bool:
        """
        Framed upload to Code char:
          [0x01][name_len][name][file_len 4B LE] then code in chunks, then [0x02] EOM.
        """
        try:
            fn_bytes = filename.encode()
            header = (
                bytes([0x01, len(fn_bytes)])
                + fn_bytes
                + struct.pack("<I", len(code))
            )

            # Write header
            await self._client.write_gatt_char(CHAR_CODE, header, response=False)
            await asyncio.sleep(0.05)

            # Write code in chunks
            for i in range(0, len(code), CHUNK_SIZE):
                chunk = code[i:i + CHUNK_SIZE]
                await self._client.write_gatt_char(CHAR_CODE, chunk, response=False)
                await asyncio.sleep(0.01)

            # Write EOM as a separate write
            await self._client.write_gatt_char(CHAR_CODE, bytes([0x02]), response=False)

            self._log(f"[ble] Framed upload sent ({len(code)} bytes).")
            return True
        except Exception as exc:
            self._log(f"[ble] Framed upload error: {exc}")
            return False

    async def _upload_raw_chunks(self, code: bytes) -> bool:
        """Raw Python code written in chunks to Code char — simplest fallback."""
        try:
            for i in range(0, len(code), CHUNK_SIZE):
                chunk = code[i:i + CHUNK_SIZE]
                await self._client.write_gatt_char(
                    CHAR_CODE, chunk, response=False
                )
                await asyncio.sleep(0.01)
            self._log("[ble] Raw chunk upload sent.")
            return True
        except Exception as exc:
            self._log(f"[ble] Raw chunk upload error: {exc}")
            return False

    # ── Run control ───────────────────────────────────────────── #

    async def start_program(self) -> None:
        """Write a 'start' command to the Command characteristic."""
        if not self.is_connected:
            return
        try:
            await self._client.write_gatt_char(CHAR_COMMAND, bytes([0x01]))
            self._log("[ble] Start command sent.")
        except Exception as exc:
            self._log(f"[ble] Start command error: {exc}")

    async def stop_program(self) -> None:
        """Write a 'stop' command (Ctrl-C) to the robot."""
        if not self.is_connected:
            return
        try:
            # Try command char first
            await self._client.write_gatt_char(CHAR_COMMAND, bytes([0x00]))
        except Exception:
            pass
        try:
            # Also send Ctrl-C on user channel
            await self._client.write_gatt_char(CHAR_RX_USER, b"\x03")
        except Exception:
            pass
        self._log("[ble] Stop command sent.")

    # ── Monitoring ────────────────────────────────────────────── #

    async def start_monitoring(self) -> None:
        """Subscribe to all notification characteristics."""
        if not self.is_connected:
            return
        self._monitoring = True
        self._ai_samples  = []
        self._imu_samples = []
        self._console_lines = []
        self._console_buf = ""

        for uuid, cb in [
            (CHAR_TX_USER,   self._handle_console),
            (CHAR_TX_DATA,   self._handle_tx_data),
            (CHAR_RC_STATUS,    self._handle_rc_status),
            (CHAR_AI_STATUS,    self._handle_ai_status),
        ]:
            try:
                await self._client.start_notify(uuid, cb)
            except Exception as exc:
                self._log(f"[ble] Could not subscribe {uuid[-4:]}: {exc}")

        self._log("[ble] Monitoring started.")

    async def stop_monitoring(self) -> None:
        """Unsubscribe from all notifications."""
        self._monitoring = False
        if not self.is_connected:
            return
        for uuid in [CHAR_TX_USER, CHAR_TX_DATA, CHAR_RC_STATUS, CHAR_AI_STATUS]:
            try:
                await self._client.stop_notify(uuid)
            except Exception:
                pass
        self._log("[ble] Monitoring stopped.")

    def drain(self) -> tuple[list[str], list[AISample], list[IMUSample]]:
        """Return and clear all collected data."""
        lines = list(self._console_lines)
        ai    = list(self._ai_samples)
        imu   = list(self._imu_samples)
        self._console_lines = []
        self._ai_samples    = []
        self._imu_samples   = []
        return lines, ai, imu

    # ── Notification handlers ─────────────────────────────────── #

    def _handle_console(self, sender, data: bytes) -> None:
        self._console_buf += data.decode(errors="replace")
        while "\n" in self._console_buf:
            line, self._console_buf = self._console_buf.split("\n", 1)
            line = line.rstrip()
            if line:
                self._console_lines.append(line)
                if self.on_console:
                    self.on_console(line)

    def _handle_tx_data(self, sender, data: bytes) -> None:
        # Binary protocol response — log raw for debugging
        if data != bytes([0xde, 0xad, 0xfa, 0xce]):  # skip deadface
            self._log(f"[ble:TXData] {data.hex()}")

    def _handle_rc_status(self, sender, data: bytes) -> None:
        sample = IMUSample.from_bytes(bytes(data))
        self._imu_samples.append(sample)
        if self.on_telemetry:
            self.on_telemetry(sample.to_dict())

    def _handle_ai_status(self, sender, data: bytes) -> None:
        sample = AISample.from_bytes(bytes(data))
        self._ai_samples.append(sample)
        if self.on_telemetry:
            self.on_telemetry(sample.to_dict())

    # ── Formatting for prompts ────────────────────────────────── #

    def format_telemetry(self) -> str:
        lines = []
        if self._imu_samples:
            last = self._imu_samples[-1]
            lines += [
                f"IMU snapshots: {len(self._imu_samples)}",
                f"  Heading:  {last.heading:+.4f}",
                f"  Pitch:    {last.pitch:+.4f}",
                f"  Accel XYZ: ({last.accel_x:+.4f}, {last.accel_y:+.4f}, {last.accel_z:+.4f})",
                f"  Gyro X/Z:  ({last.gyro_x:+.4f}, {last.gyro_z:+.4f})",
            ]
        if self._ai_samples:
            last = self._ai_samples[-1]
            lines += [
                f"AI snapshots: {len(self._ai_samples)}",
                f"  Objects: {last.num_objects}  class={last.class_id}",
                f"  BBox: x={last.x} y={last.y} w={last.width} h={last.height}",
                f"  Confidence: {last.confidence}%",
            ]
        return "\n".join(lines) if lines else "(no telemetry)"

    def format_console(self) -> str:
        return "\n".join(self._console_lines) if self._console_lines else "(no console output)"
