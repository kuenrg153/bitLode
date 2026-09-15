"""Locate a bitLode's USB serial ports by VID:PID (e3be:40e1).

    ctrl, data, serial_no = find_ports()

ctrl is USB interface 0 (control), data is interface 2 (ASIC UART pass-through).
BITLODE_CTRL / BITLODE_DATA environment variables override discovery, which is
needed when more than one bitLode is connected.
"""
import glob, os, subprocess

VIDPID = ('e3be', '40e1')


def find_ports():
    ctrl = data = serial_no = None
    for dev in sorted(glob.glob('/dev/ttyACM*')):
        info = subprocess.run(['udevadm', 'info', '--name=' + dev], capture_output=True, text=True).stdout
        kv = dict(l[3:].split('=', 1) for l in info.splitlines() if l.startswith('E: ') and '=' in l)
        if (kv.get('ID_VENDOR_ID'), kv.get('ID_MODEL_ID')) != VIDPID:
            continue
        serial_no = kv.get('ID_SERIAL_SHORT')
        if kv.get('ID_USB_INTERFACE_NUM') == '00': ctrl = dev
        if kv.get('ID_USB_INTERFACE_NUM') == '02': data = dev
    return os.environ.get('BITLODE_CTRL', ctrl), os.environ.get('BITLODE_DATA', data), serial_no
