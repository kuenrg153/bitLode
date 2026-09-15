#!/usr/bin/env python3
"""Rig power control — the ONE place the relay mapping is encoded.

    rigpower.py on     # energise WM + bitLode hashboards
    rigpower.py off    # de-energise
    rigpower.py state  # report pin states, change nothing

⚠⚠ HARDWARE CHANGED 2026-09-07. The old mechanical relay on pin 8 was FRIED and replaced with
an SSR. The polarity is now INVERTED from every pre-2026-09-07 script:

        OLD relay (dead):   8L = ON    8H = OFF
        NEW SSR   (live):   8H = ON    8L = OFF     <-- HIGH IS ON

Any script still calling `relaycmd.py 8H` to power *down* now powers the rig *UP*. Audit before
reuse; the pre-2026-09-07 session runners in sessions/*/ are all stale in this respect.

Pin map (confirmed on hardware 2026-09-07, with the user watching the miner):
    GPIO8   SSR, WM + bitLode PSU     8H = ON, 8L = OFF
    GPIO9   FRIED — drives nothing, ignore it
    GPIO10  DSLogic trigger / marker  (Ch3), unchanged

⚠ Two traps, both learned the hard way:
 1. Opening /dev/ttyUSB0 pulses DTR and RESETS the Arduino, returning pins to sketch defaults.
    Two separate invocations therefore cannot hold two pins — the second wipes the first. Set
    multi-pin state in ONE session (see tools/relaymulti.py).
 2. After that reset the status line can read `Pin8(adit)=ON` while the pin is NOT actually
    driven. The reported state is not proof of power; only an explicit 8H drives it. This is
    why pin 8 "looked on" for several minutes while the miner stayed dark.
 3. `ping 192.168.88.253` is NOT a power test — the route is periodically hijacked by
    Tailscale (`ip route get` shows dev tailscale0). Confirm power via the ASIC chain over USB.
"""
import serial, sys, time

PORT = '/dev/ttyUSB0'
CMD = {'on': '8H', 'off': '8L'}


def send(cmds):
    s = serial.Serial(PORT, 115200, timeout=1)
    s.dtr = False
    time.sleep(3)                       # ride out the reset-on-open
    s.reset_input_buffer()
    last = ''
    for c in cmds:
        s.write((c + '\n').encode()); s.flush()
        time.sleep(0.4)
        out = s.read(400).decode(errors='replace').strip().replace('\r', '')
        last = out.splitlines()[-1] if out else '(no reply)'
        print('%-4s -> %s' % (c, last))
    s.close()
    return last


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ('on', 'off', 'state'):
        print(__doc__); return 2
    act = sys.argv[1]
    if act == 'state':
        # ⚠⚠ THIS IS NOT A READ-ONLY OPERATION. Opening the port pulses DTR and RESETS the
        # Arduino, returning pin 8 to its sketch default -- and the miner has been observed to
        # come ON when the Arduino is powered. So a "just checking" status call can energise the
        # rig, and the reset state then PRINTS `Pin8(adit)=ON` whether or not the pin is driven.
        # Always drive an explicit state afterwards; never infer power from this line alone.
        print("⚠ 'state' resets the Arduino and may energise the rig; driving OFF afterwards.")
        send(['s'])
        send([CMD['off'], 's'])
        print("rig driven OFF after status read")
        return 0
    line = send([CMD[act], 's'])
    want = 'Pin8(adit)=ON' if act == 'on' else 'Pin8(adit)=OFF'
    if want not in line:
        print('⚠ pin 8 did not report %s — got: %s' % (want, line)); return 1
    print('rig %s (pin 8 %s)' % (act.upper(), 'HIGH' if act == 'on' else 'LOW'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
