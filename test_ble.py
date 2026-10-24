"""
End-to-end BLE diagnostic test for VEX AIM robot.

Usage:
    python3 test_ble.py

What it does:
  1. Connects to the robot at the known BLE address
  2. Dumps ALL services and characteristics
  3. Tries multiple upload methods for a tiny test program
  4. Subscribes to all notify characteristics and watches for output
  5. Tries to start the program
  6. Prints everything received for 15 seconds
"""
import asyncio
import struct
import time
import sys

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    print("ERROR: bleak not installed. Run: pip3 install bleak")
    sys.exit(1)

# ── Known addresses / UUIDs ────────────────────────────────────────
BLE_ADDRESS = "760FE908-F0DD-3B15-F642-5C9AE8298F94"

CHAR_RX_DATA  = "08590f7e-db05-467e-8757-72f6faeb13f5"
CHAR_TX_DATA  = "08590f7e-db05-467e-8757-72f6faeb1306"
CHAR_RX_USER  = "08590f7e-db05-467e-8757-72f6faeb1326"
CHAR_TX_USER  = "08590f7e-db05-467e-8757-72f6faeb1316"
CHAR_CODE     = "08590f7e-db05-467e-8757-72f6faeb13e5"
CHAR_COMMAND  = "6faa5920-9200-49a0-a353-6152c5930ec5"
CHAR_RC_STATUS   = "6c7851a0-adf7-4de6-881d-1ce2cd0fecf9"
CHAR_AI_STATUS   = "6c7851a0-adf7-4de6-881d-1ce2cd0fecc9"

# ── Simple test program ────────────────────────────────────────────
TEST_CODE = b"""\
import vex
b = vex.Brain()
b.screen.print("HELLO TEST")
b.screen.next_row()
for i in range(5):
    b.screen.print(i)
    b.screen.next_row()
    vex.wait(500, vex.MSEC)
print("TEST_DONE")
"""

log_lines = []

def log(msg: str):
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    log_lines.append(line)


# ── Notification handlers ──────────────────────────────────────────

def make_handler(name: str):
    def handler(sender, data: bytes):
        try:
            txt = data.decode(errors="replace").rstrip()
            if txt:
                log(f"  << {name} TEXT: {txt!r}")
        except Exception:
            pass
        log(f"  << {name} HEX [{len(data)}B]: {data.hex()}")
    return handler


# ── GATT dump ─────────────────────────────────────────────────────

async def dump_services(client: BleakClient):
    log("── GATT Service / Characteristic Map ─────────────────────")
    for svc in client.services:
        log(f"  SVC {svc.uuid}  ({svc.description})")
        for ch in svc.characteristics:
            extras = []
            # Try to read the value if readable
            if "read" in ch.properties:
                try:
                    val = await client.read_gatt_char(ch.uuid)
                    extras.append(f"value={val.hex()!r}")
                except Exception as e:
                    extras.append(f"read-err={e}")
            # Try to read User Description descriptor
            for d in ch.descriptors:
                if d.uuid.startswith("00002901"):
                    try:
                        desc_val = await client.read_gatt_descriptor(d.handle)
                        extras.append(f"desc={desc_val.decode(errors='replace')!r}")
                    except Exception:
                        pass
            log(f"    CHAR {ch.uuid[-4:]}  props={ch.properties}  {' '.join(extras)}")
    log("───────────────────────────────────────────────────────────")


# ── Upload methods ─────────────────────────────────────────────────

async def try_repl_upload(client: BleakClient, code: bytes, filename: str) -> bool:
    """MicroPython raw-REPL upload via RXUser."""
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

    async def _send(data: bytes):
        for i in range(0, len(data), 20):
            await client.write_gatt_char(CHAR_RX_USER, data[i:i+20], response=False)
            await asyncio.sleep(0.005)

    log("[REPL] Sending Ctrl+C x2 ...")
    await _send(b"\x03\x03")
    await asyncio.sleep(0.4)

    log("[REPL] Sending Ctrl+A (raw REPL) ...")
    await _send(b"\x01")
    await asyncio.sleep(0.3)

    log(f"[REPL] Sending {len(script)}-byte write script ...")
    await _send(script)

    log("[REPL] Sending Ctrl+D (execute) ...")
    await _send(b"\x04")
    await asyncio.sleep(1.0)

    log("[REPL] Done — watch for UPLOAD_OK above.")
    return True


async def try_framed_upload(client: BleakClient, code: bytes, filename: str) -> bool:
    """Framed protocol to Code characteristic."""
    fn_bytes = filename.encode()
    header = bytes([0x01, len(fn_bytes)]) + fn_bytes + struct.pack("<I", len(code))
    log(f"[FRAMED] Header ({len(header)}B): {header.hex()}")

    try:
        await client.write_gatt_char(CHAR_CODE, header, response=False)
        await asyncio.sleep(0.1)

        CHUNK = 512
        for i in range(0, len(code), CHUNK):
            chunk = code[i:i+CHUNK]
            await client.write_gatt_char(CHAR_CODE, chunk, response=False)
            await asyncio.sleep(0.02)
            log(f"[FRAMED] chunk {i}–{i+len(chunk)}")

        log("[FRAMED] Sending EOM (0x02) ...")
        await client.write_gatt_char(CHAR_CODE, bytes([0x02]), response=False)
        await asyncio.sleep(0.5)
        return True
    except Exception as e:
        log(f"[FRAMED] ERROR: {e}")
        return False


async def try_raw_upload(client: BleakClient, code: bytes) -> bool:
    """Raw Python bytes → Code char."""
    try:
        CHUNK = 512
        for i in range(0, len(code), CHUNK):
            chunk = code[i:i+CHUNK]
            await client.write_gatt_char(CHAR_CODE, chunk, response=False)
            await asyncio.sleep(0.02)
            log(f"[RAW] chunk {i}–{i+len(chunk)}")
        log("[RAW] Done.")
        return True
    except Exception as e:
        log(f"[RAW] ERROR: {e}")
        return False


async def try_rxdata_upload(client: BleakClient, code: bytes, filename: str) -> bool:
    """VEX binary framing on RXData characteristic."""
    fn_bytes = filename.encode()
    # Try VEX-style header: magic 0x56 0x11, type 0x10 (file upload), ...
    header = bytes([0x56, 0x11, 0x10, len(fn_bytes)]) + fn_bytes + struct.pack("<I", len(code))
    log(f"[RXDATA] header: {header.hex()}")
    try:
        await client.write_gatt_char(CHAR_RX_DATA, header, response=False)
        await asyncio.sleep(0.1)
        CHUNK = 512
        for i in range(0, len(code), CHUNK):
            chunk = code[i:i+CHUNK]
            await client.write_gatt_char(CHAR_RX_DATA, chunk, response=False)
            await asyncio.sleep(0.02)
        log("[RXDATA] Done.")
        return True
    except Exception as e:
        log(f"[RXDATA] ERROR: {e}")
        return False


async def try_start_program(client: BleakClient):
    for val in [b"\x01", b"\x01\x00", b"\x40", bytes([0x56, 0x11, 0x18, 0x00])]:
        try:
            await client.write_gatt_char(CHAR_COMMAND, val, response=False)
            log(f"[CMD] Sent start command: {val.hex()}")
            await asyncio.sleep(0.3)
        except Exception as e:
            log(f"[CMD] {val.hex()} → error: {e}")


# ── Main ───────────────────────────────────────────────────────────

async def main():
    log(f"Connecting to VEX AIM at {BLE_ADDRESS} ...")
    async with BleakClient(BLE_ADDRESS, timeout=15.0) as client:
        log(f"Connected: {client.is_connected}")

        # 1. Dump GATT map
        await dump_services(client)

        # 2. Subscribe to every notify characteristic
        notify_chars = []
        for svc in client.services:
            for ch in svc.characteristics:
                if "notify" in ch.properties or "indicate" in ch.properties:
                    try:
                        await client.start_notify(ch.uuid, make_handler(ch.uuid[-4:]))
                        notify_chars.append(ch.uuid)
                        log(f"  Subscribed to {ch.uuid[-4:]}")
                    except Exception as e:
                        log(f"  Could not subscribe {ch.uuid[-4:]}: {e}")

        await asyncio.sleep(0.5)

        filename = "test_hello.py"

        # 3. Try REPL upload
        log("\n══ METHOD 1: MicroPython REPL upload ══")
        await try_repl_upload(client, TEST_CODE, filename)
        log("Waiting 3s for any response ...")
        await asyncio.sleep(3)

        # 4. Try framed upload
        log("\n══ METHOD 2: Framed Code char upload ══")
        await try_framed_upload(client, TEST_CODE, filename)
        log("Waiting 3s for any response ...")
        await asyncio.sleep(3)

        # 5. Try raw upload
        log("\n══ METHOD 3: Raw Code char upload ══")
        await try_raw_upload(client, TEST_CODE)
        log("Waiting 3s for any response ...")
        await asyncio.sleep(3)

        # 6. Try start command
        log("\n══ Attempting to start program ══")
        await try_start_program(client)
        log("Watching for 10s of robot output ...")
        await asyncio.sleep(10)

        # 7. Unsubscribe
        for uuid in notify_chars:
            try:
                await client.stop_notify(uuid)
            except Exception:
                pass

    log("\n══ Test complete ══")
    log(f"Total log lines: {len(log_lines)}")


if __name__ == "__main__":
    asyncio.run(main())
