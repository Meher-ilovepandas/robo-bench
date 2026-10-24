"""
VEX AIM BLE protocol probe.

Tries various initiation sequences on RXData and monitors TXData for responses.
Goal: discover the handshake needed before code upload.

Usage:
    python3 test_proto.py
"""
import asyncio
import struct
import sys
import time

try:
    from bleak import BleakClient
except ImportError:
    print("ERROR: bleak not installed"); sys.exit(1)

BLE_ADDRESS = "760FE908-F0DD-3B15-F642-5C9AE8298F94"

CHAR_RX_DATA = "08590f7e-db05-467e-8757-72f6faeb13f5"
CHAR_TX_DATA = "08590f7e-db05-467e-8757-72f6faeb1306"
CHAR_RX_USER = "08590f7e-db05-467e-8757-72f6faeb1326"
CHAR_TX_USER = "08590f7e-db05-467e-8757-72f6faeb1316"
CHAR_CODE    = "08590f7e-db05-467e-8757-72f6faeb13e5"
CHAR_COMMAND = "6faa5920-9200-49a0-a353-6152c5930ec5"
CHAR_RC_CMD  = "6c7851a0-adf7-4de6-881d-1ce2cd0fece9"

responses: list[tuple[str, bytes]] = []

def on_txdata(sender, data: bytes):
    ts = time.strftime("%H:%M:%S.") + f"{int(time.time()*1000)%1000:03d}"
    print(f"  [{ts}] TXData << {data.hex()}  ({data!r})")
    responses.append(("TXData", data))

def on_txuser(sender, data: bytes):
    ts = time.strftime("%H:%M:%S.") + f"{int(time.time()*1000)%1000:03d}"
    txt = data.decode(errors="replace")
    print(f"  [{ts}] TXUser << {data.hex()}  ({txt!r})")
    responses.append(("TXUser", data))

def on_cmd(sender, data: bytes):
    ts = time.strftime("%H:%M:%S.") + f"{int(time.time()*1000)%1000:03d}"
    print(f"  [{ts}] Command << {data.hex()}")
    responses.append(("Command", data))


async def probe(client: BleakClient, name: str, char: str, data: bytes, wait: float = 0.5):
    before = len(responses)
    print(f"\n── {name}")
    print(f"   >> {char[-4:]} : {data.hex()}")
    try:
        await client.write_gatt_char(char, data, response=False)
    except Exception as e:
        print(f"   write error: {e}")
        return
    await asyncio.sleep(wait)
    new = responses[before:]
    if not new:
        print("   (no response)")


async def main():
    print(f"Connecting to {BLE_ADDRESS} ...")
    async with BleakClient(BLE_ADDRESS, timeout=15.0) as client:
        print("Connected.\n")

        # Subscribe to notify channels
        await client.start_notify(CHAR_TX_DATA, on_txdata)
        await client.start_notify(CHAR_TX_USER, on_txuser)
        try:
            await client.start_notify(CHAR_COMMAND, on_cmd)
        except Exception:
            pass

        await asyncio.sleep(0.3)

        # ── Read current Code char value ──────────────────────────────
        try:
            val = await client.read_gatt_char(CHAR_CODE)
            print(f"Code char current value: {val.hex()}")
        except Exception as e:
            print(f"Code char read error: {e}")

        # ══════════════════════════════════════════════════════════════
        # GROUP 1: RXData probes (VEX binary protocol variants)
        # ══════════════════════════════════════════════════════════════

        # VEX V5/IQ serial protocol magic header + noop
        await probe(client, "VEX magic c936b847 ping",
                    CHAR_RX_DATA, bytes([0xc9, 0x36, 0xb8, 0x47, 0x21, 0x00]))

        # VEX AIM might use 0x56 0x11 prefix
        await probe(client, "VEX 5611 ping (type=0x01)",
                    CHAR_RX_DATA, bytes([0x56, 0x11, 0x01, 0x00]))

        await probe(client, "VEX 5611 get-version (type=0x02)",
                    CHAR_RX_DATA, bytes([0x56, 0x11, 0x02, 0x00]))

        await probe(client, "VEX 5611 get-status (type=0x03)",
                    CHAR_RX_DATA, bytes([0x56, 0x11, 0x03, 0x00]))

        await probe(client, "VEX 5611 list-files (type=0x04)",
                    CHAR_RX_DATA, bytes([0x56, 0x11, 0x04, 0x00]))

        await probe(client, "VEX 5611 init-upload (type=0x10)",
                    CHAR_RX_DATA, bytes([0x56, 0x11, 0x10, 0x00]))

        # Single-byte probes
        for b in [0x00, 0x01, 0x02, 0x10, 0x20, 0x40, 0x50, 0xFF]:
            await probe(client, f"RXData single byte 0x{b:02x}",
                        CHAR_RX_DATA, bytes([b]), wait=0.3)

        # VEX IQ known message: 0xAA 0x55 sync
        await probe(client, "AA55 sync probe",
                    CHAR_RX_DATA, bytes([0xAA, 0x55, 0x00, 0x00]))

        # ══════════════════════════════════════════════════════════════
        # GROUP 2: Command char probes
        # ══════════════════════════════════════════════════════════════

        for val in [b"\x00", b"\x01", b"\x02", b"\x10", b"\x20", b"\xFF",
                    b"\x56\x11\x00\x00", b"\x56\x11\x01\x00"]:
            await probe(client, f"Command 0x{val.hex()}",
                        CHAR_COMMAND, val, wait=0.3)

        # ══════════════════════════════════════════════════════════════
        # GROUP 3: Code char - read after writes to see if it changes
        # ══════════════════════════════════════════════════════════════
        print("\n── Code char: write 0x01 then re-read")
        await client.write_gatt_char(CHAR_CODE, bytes([0x01]), response=False)
        await asyncio.sleep(0.2)
        try:
            val = await client.read_gatt_char(CHAR_CODE)
            print(f"   Code char after 0x01: {val.hex()}")
        except Exception as e:
            print(f"   read error: {e}")

        print("\n── Code char: write 0x56 0x11 0x00 0x00 then re-read")
        await client.write_gatt_char(CHAR_CODE, bytes([0x56, 0x11, 0x00, 0x00]), response=False)
        await asyncio.sleep(0.2)
        try:
            val = await client.read_gatt_char(CHAR_CODE)
            print(f"   Code char after: {val.hex()}")
        except Exception as e:
            print(f"   read error: {e}")

        # ══════════════════════════════════════════════════════════════
        # GROUP 4: RC_Cmd probes (control/sensors channel)
        # ══════════════════════════════════════════════════════════════
        for val in [bytes([0x01]), bytes([0x56, 0x11, 0x00, 0x00])]:
            await probe(client, f"RC_Cmd 0x{val.hex()}",
                        CHAR_RC_CMD, val, wait=0.3)

        # ══════════════════════════════════════════════════════════════
        # GROUP 5: Listen for 5 more seconds in case robot is slow
        # ══════════════════════════════════════════════════════════════
        print("\n── Listening for 5 more seconds ...")
        await asyncio.sleep(5)

        await client.stop_notify(CHAR_TX_DATA)
        await client.stop_notify(CHAR_TX_USER)
        try:
            await client.stop_notify(CHAR_COMMAND)
        except Exception:
            pass

    print(f"\n══ Done. Total TXData/TXUser responses: {len(responses)} ══")
    if responses:
        print("Responses received:")
        for ch, data in responses:
            print(f"  {ch}: {data.hex()}")
    else:
        print("No responses received on any channel — robot requires different protocol.")


if __name__ == "__main__":
    asyncio.run(main())
