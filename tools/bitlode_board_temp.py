#!/usr/bin/env python3
"""Read the bitLode-connected hashboard's I2C temperature sensor (0x48) through the bitLode control port."""
import sys, time, struct, serial
def send_ctrl(ser, cmd_id, page, cmd_bytes, wait=0.08):
    payload = bytes([cmd_id, 0, page]) + cmd_bytes
    ser.reset_input_buffer(); ser.write(struct.pack('<H', len(payload) + 2) + payload); time.sleep(wait)
    hdr = ser.read(2)
    if len(hdr) < 2: return None, b''
    dlen = struct.unpack('<H', hdr)[0]; body = ser.read(1 + dlen)
    return (body[0] if body else None), (body[1:] if len(body) > 1 else b'')
from bitlode_ports import find_ports
ctrl = serial.Serial(find_ports()[0] or "/dev/ttyACM0", 9600, timeout=1); time.sleep(0.2)
send_ctrl(ctrl, 1, 5, bytes([0x10]) + struct.pack('<I', 100000))
rid, d = send_ctrl(ctrl, 2, 5, bytes([0x30, 0x48, 2]))
ok = d and len(d) >= 2 and not (d[0] == 0xFF and d[1:5] == b'I2C ')
print(f"bitlode_board_temp={d[0] + d[1]/256:.2f}C" if ok else f"bitlode_board_temp=ERR({d!r})")
