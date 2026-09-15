#!/usr/bin/env python3
"""bitLode SYSTEM control page (0x0F), added to the KF1950 Ember One build 2026-09-14.

    bitlode_sys.py ident     # print the firmware identity string ("(no reply)" = older firmware)
    bitlode_sys.py bootsel   # reboot the bitLode into its RP2040 USB bootloader (appears as RPI-RP2)

Packet format (control port, found by USB VID:PID e3be:40e1): u16 LE total length, then [id, bus, page, cmd bytes...].
"""
import sys, time, struct, serial, glob, os

from bitlode_ports import find_ports
CTRL = find_ports()[0] or "/dev/ttyACM0"


def send(ser, cid, page, body, wait=0.3):
    payload = bytes([cid, 0, page]) + body
    ser.reset_input_buffer()
    ser.write(struct.pack('<H', len(payload) + 2) + payload); ser.flush()
    time.sleep(wait)
    hdr = ser.read(2)
    if len(hdr) < 2:
        return None
    n = struct.unpack('<H', hdr)[0]
    rest = ser.read(1 + n)
    return rest[1:]


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("ident", "bootsel"):
        print(__doc__); return 2
    ser = serial.Serial(CTRL, 9600, timeout=1); time.sleep(0.2)
    if sys.argv[1] == "ident":
        r = send(ser, 1, 0x0F, bytes([0x00]))
        if r and all(32 <= b < 127 for b in r):
            print(r.decode())
        else:
            print(f"(SYSTEM page not supported by this firmware -- reply {r!r}; 0x11 = invalid command)")
        return 0
    # ⚠ the firmware reboots 200 ms after sending the ack, so read it quickly; losing the port here is expected.
    try:
        r = send(ser, 2, 0x0F, bytes([0x01, 0xB0, 0x07]), wait=0.03)
        print(f"ack: {r!r}")
    except serial.SerialException:
        print("port dropped before the ack was read (board already rebooting)")
    try:
        ser.close()
    except Exception:
        pass
    for _ in range(40):                       # up to ~20 s for the ROM bootloader to enumerate
        time.sleep(0.5)
        if os.popen("lsusb").read().find("2e8a:0003") >= 0:
            print("RP2040 USB bootloader present (2e8a:0003) -- copy a UF2 onto the RPI-RP2 drive\n"
                  "  (mounting may need your desktop session: udisksctl mount -b /dev/sdX1, or the file manager)")
            return 0
    print("bootloader did not appear"); return 1


if __name__ == "__main__":
    sys.exit(main())
