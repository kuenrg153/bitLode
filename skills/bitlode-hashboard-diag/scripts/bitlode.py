#!/usr/bin/env python3
"""bitLode control-port CLI — board-side diagnostics that need NO chain traffic.

    bitlode.py find                      locate ctrl/data ports (by USB VID:PID e3be:40e1)
    bitlode.py ident                     firmware identity (page 0x0F; older builds answer 0x11)
    bitlode.py rst [get|low|high|pulse [MS]]   RESETN on GPIO11 (default pulse 1000 ms)
    bitlode.py adc                       raw VDD/VIN ADC (GPIO26/27) — NOT characterised on M30S boards
    bitlode.py temp                      hashboard temp sensor, I2C 0x48 (°C = b0 + b1/256)
    bitlode.py i2c-scan                  scan 0x08-0x77
    bitlode.py eeprom-dump  [--addr 0x50] [--out FILE]      3 reads, must agree; detects 128-B parts
    bitlode.py eeprom-decode FILE                            parse + sanity-check a dump
    bitlode.py eeprom-write --offset 0xNN --hex "aa bb" [--addr 0x50] [--yes]
    bitlode.py eeprom-restore FILE [--addr 0x50] [--yes]    write only the bytes that differ

WRITES ARE DRY-RUN UNLESS --yes. Every write first takes (and keeps) a fresh verified backup,
writes one byte at a time, then reads everything back and compares.

Nothing here switches hashboard power, and nothing here touches the data (UART) port.
⚠ Control-port commands collide with a replay tool that has /dev/ttyACM0 open — run between arms.
"""
import sys, os, time, struct, glob, subprocess, hashlib, argparse

VIDPID = ('e3be', '40e1')
PAGE_I2C, PAGE_GPIO, PAGE_ADC, PAGE_SYS = 5, 6, 7, 0x0F
EEPROM_DEFAULT = 0x50
BACKUP_DIR = os.environ.get('BITLODE_CAPTURES', os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..', 'captures'))


# ─── port discovery ───────────────────────────────────────────────────────────
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
    return ctrl, data, serial_no


# ─── control transport ────────────────────────────────────────────────────────
class CtrlError(Exception):
    pass


class Ctrl:
    """Packet: u16 LE total length, id, bus(0), page, cmd bytes.
    Success reply: u16 LE DATA length, id, data.
    Error reply:   u16 LE TOTAL length (header included!), id, code [, ascii msg]
                   code 0x10 timeout, 0x11 invalid, 0x12 overflow, 0xFF message (e.g. 'I2C Read Error').
    ⇒ an error is exactly a reply that is SHORTER than its length field says."""

    def __init__(self, port=None):
        import serial
        port = port or find_ports()[0]
        if not port:
            raise SystemExit('bitLode control port not found (lsusb | grep e3be:40e1; try a USB replug)')
        self.ser = serial.Serial(port, 9600, timeout=0.25)
        time.sleep(0.15)
        self.ser.reset_input_buffer()
        self.cid = 0

    def cmd(self, page, body, wait=0.05):
        self.cid = (self.cid % 120) + 1
        payload = bytes([self.cid, 0, page]) + bytes(body)
        self.ser.reset_input_buffer()
        self.ser.write(struct.pack('<H', len(payload) + 2) + payload)
        self.ser.flush()
        time.sleep(wait)
        hdr = self.ser.read(2)
        if len(hdr) < 2:
            raise CtrlError('no reply')
        n = struct.unpack('<H', hdr)[0]
        rest = self.ser.read(1 + n)
        if not rest or rest[0] != self.cid:
            raise CtrlError('reply id mismatch (%r)' % rest[:8])
        d = rest[1:]
        if len(d) < n:
            code = d[0] if d else None
            msg = d[1:].decode(errors='replace') if code == 0xFF else {0x10: 'timeout', 0x11: 'invalid command', 0x12: 'overflow'}.get(code, '?')
            raise CtrlError('device error 0x%02x %s' % (code or 0, msg))
        return d

    # I2C
    def i2c_freq(self, hz=100000):
        return self.cmd(PAGE_I2C, bytes([0x10]) + struct.pack('<I', hz))

    def i2c_read(self, addr, n):
        return self.cmd(PAGE_I2C, bytes([0x30, addr, n]))

    def i2c_write(self, addr, data):
        return self.cmd(PAGE_I2C, bytes([0x20, addr]) + bytes(data))

    def i2c_wr(self, addr, wdata, n):
        return self.cmd(PAGE_I2C, bytes([0x40, addr]) + bytes(wdata) + bytes([n]), wait=0.08)

    def close(self):
        self.ser.close()


def hx(b):
    return ' '.join('%02x' % x for x in b)


# ─── simple commands ──────────────────────────────────────────────────────────
def c_find(_):
    c, d, s = find_ports()
    print('ctrl=%s data=%s serial=%s' % (c, d, s))
    return 0 if c and d else 1


def c_ident(_):
    k = Ctrl()
    try:
        r = k.cmd(PAGE_SYS, [0x00], wait=0.2)
        print(r.decode(errors='replace'))
    except CtrlError as e:
        print('(no SYSTEM page: %s) -> stock/older build; check TXD bit width at a 12 M request' % e)
    return 0


def c_rst(a):
    k = Ctrl()
    act = a.action
    if act == 'get':
        print('RESETN =', k.cmd(PAGE_GPIO, [0x00])[0], '(1 released, 0 asserted)')
    elif act == 'low':
        k.cmd(PAGE_GPIO, [0x00, 0x00]); print('RESETN LOW (chain held in reset)')
    elif act == 'high':
        k.cmd(PAGE_GPIO, [0x00, 0x01]); print('RESETN HIGH')
    else:
        ms = a.ms
        k.cmd(PAGE_GPIO, [0x00, 0x00]); time.sleep(ms / 1000.0); k.cmd(PAGE_GPIO, [0x00, 0x01])
        print('RESETN pulsed %d ms (wait ~15 s before talking to the chain)' % ms)
    return 0


def c_adc(_):
    k = Ctrl()
    for name, code in (('VDD(GPIO26)', 0x50), ('VIN(GPIO27)', 0x51)):
        v = struct.unpack('<H', k.cmd(PAGE_ADC, [code])[:2])[0]
        print('%s raw=%d  %.3f V at the ADC pin (divider/wiring unverified on this rig)' % (name, v, v * 3.3 / 4095))
    return 0


def c_temp(_):
    k = Ctrl(); k.i2c_freq()
    try:
        d = k.i2c_read(0x48, 2)
        print('board_temp=%.2f C' % (d[0] + d[1] / 256))
        return 0
    except CtrlError as e:
        print('board_temp=ERR (%s) -- hashboard I2C unpowered or connector not seated?' % e)
        return 1


def c_scan(_):
    k = Ctrl(); k.i2c_freq()
    found = []
    for addr in range(0x08, 0x78):
        try:
            b = k.i2c_read(addr, 1); found.append(addr)
            print('  0x%02x ACK first=%s' % (addr, hx(b)))
        except CtrlError:
            pass
    print('found: %s   (expected on an M30S: 0x48 temp + one EEPROM in 0x50-0x57)' % [hex(a) for a in found])
    return 0 if found else 1


# ─── EEPROM ───────────────────────────────────────────────────────────────────
def read_eeprom(k, addr, size=256, chunk=16):
    blob = bytearray()
    for off in range(0, size, chunk):
        blob += k.i2c_wr(addr, [off], chunk)[:chunk]
    return bytes(blob)


def dump_verified(k, addr, tries=3):
    """Read `tries` times; all must match. Returns (image, part_size)."""
    k.i2c_freq()
    imgs = [read_eeprom(k, addr) for _ in range(tries)]
    if len({hashlib.md5(i).hexdigest() for i in imgs}) != 1:
        raise SystemExit('EEPROM reads DISAGREE across %d passes -- flaky bus; do not write. md5s: %s'
                         % (tries, [hashlib.md5(i).hexdigest() for i in imgs]))
    img = imgs[0]
    size = 128 if img[:128] == img[128:] else 256   # 24C01 wraps its 7-bit address space
    return img[:size], size


def save_backup(img, addr, tag='bitlode'):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    path = os.path.join(BACKUP_DIR, 'eeprom_0x%02x_%s_%s.bin' % (addr, tag, time.strftime('%Y%m%d_%H%M%S')))
    with open(path, 'wb') as f:
        f.write(img)
    return path


def decode(img):
    """Datasheet §15.1 + RIG_AND_METHOD_NOTES §1. Returns list of (level, text)."""
    out = []
    add = lambda lvl, t: out.append((lvl, t))
    add('info', 'size %d B, md5 %s' % (len(img), hashlib.md5(img).hexdigest()))
    tag, magic = img[0], img[1]
    add('ok' if (tag, magic) == (0xFA, 0x5A) else 'BAD',
        'data_tag %02x %02x  (fa 5a = valid; 00 5a = administratively disabled; ff ff = blank)' % (tag, magic))
    if len(img) < 5:
        return out
    ver, lp, lc, lt = img[2], img[2], img[3], img[4]
    add('info', 'header bytes 02-04: %02x %02x %02x  (good boards: 14 1e 13 = pcb 20 / chip 30 / ts 19)' % (img[2], img[3], img[4]))
    p0 = 5
    fields = (('pcb_data', lp), ('chip_data', lc), ('timestamp', lt))
    end = p0 + lp + lc + lt
    content_end = max((i for i, b in enumerate(img[:0x7C]) if b != 0xFF), default=-1) + 1
    add('ok' if end == content_end else 'BAD',
        'declared record end 0x%02x vs real content end 0x%02x%s' % (end, content_end,
        '' if end == content_end else '  <- btminer ErrorCode[422] "Data offset and length verification failed"'))
    p = p0
    for name, n in fields:
        raw = img[p:p + n]
        bad = [i + p for i, b in enumerate(raw) if not 32 <= b < 127]
        add('ok' if not bad else 'BAD', '%-9s @0x%02x len %2d: %r%s' % (name, p, n, raw.decode('latin-1'),
            '' if not bad else '  non-ASCII at ' + ', '.join('0x%02x' % i for i in bad[:6])
                               + (' (+%d more)' % (len(bad) - 6) if len(bad) > 6 else '')))
        p += n
    chip = img[p0 + lp:p0 + lp + lc].decode('latin-1')
    if 'BINV' in chip:
        tail = chip.split('BINV', 1)[1]           # e.g. "03-195005C"
        add('info', 'bin_version %s  chip %s  bin_type %s  leak_current %s' % (tail[:2], tail[3:7], tail[7:9], tail[9:10]))
    if len(img) >= 0x7E:
        hr = img[0x7C] | img[0x7D] << 8
        add('info' if hr != 0xFFFF else 'warn', 'rated hashrate @0x7c LE16 = %d GH/s' % hr)
    if len(img) >= 0x79 and img[0x77:0x79] != b'\xff\xff':
        add('warn', 'bytes 0x77/0x78 = %s (good boards: ff ff; write-eeprom-data puts a CRC here)' % hx(img[0x77:0x79]))
    return out


def c_dump(a):
    k = Ctrl()
    img, size = dump_verified(k, a.addr)
    for off in range(0, size, 16):
        ch = img[off:off + 16]
        print('  %03x: %-48s |%s|' % (off, hx(ch), ''.join(chr(b) if 32 <= b < 127 else '.' for b in ch)))
    path = a.out or save_backup(img, a.addr)
    if a.out:
        open(a.out, 'wb').write(img)
    print('\nsaved %s (%d B, 3 identical reads)\n' % (path, size))
    for lvl, t in decode(img):
        print('  [%-4s] %s' % (lvl, t))
    return 0


def c_decode(a):
    img = open(a.file, 'rb').read()
    for lvl, t in decode(img):
        print('  [%-4s] %s' % (lvl, t))
    return 0


def write_bytes(k, addr, changes, yes):
    """changes: {offset: value}. Backup -> byte-wise write -> full verify."""
    before, size = dump_verified(k, addr)
    changes = {o: v for o, v in changes.items() if before[o] != v}
    for o in changes:
        if o >= size:
            raise SystemExit('offset 0x%02x beyond %d-B part' % (o, size))
    if not changes:
        print('nothing to do: every requested byte already holds that value'); return 0
    print('planned %d byte change(s) on I2C 0x%02x:' % (len(changes), addr))
    for o in sorted(changes):
        print('  0x%02x: %02x -> %02x' % (o, before[o], changes[o]))
    if not yes:
        print('\nDRY RUN -- nothing written. Re-run with --yes after the user has approved these exact bytes.')
        return 0
    bk = save_backup(before, addr, 'prewrite')
    print('backup (3 identical reads) -> %s  md5 %s' % (bk, hashlib.md5(before).hexdigest()))
    print('revert with: bitlode.py eeprom-restore %s --addr 0x%02x --yes' % (bk, addr))
    for o in sorted(changes):
        k.i2c_write(addr, [o, changes[o]])
        time.sleep(0.012)                    # 24Cxx internal write cycle is <= 5-10 ms
    after, _ = dump_verified(k, addr)
    expect = bytearray(before)
    for o, v in changes.items():
        expect[o] = v
    diff = [o for o in range(size) if after[o] != expect[o]]
    if diff:
        print('*** VERIFY FAILED at %s -- write-protect (WP pin) or wrong part? Backup is %s ***'
              % (['0x%02x' % o for o in diff[:10]], bk))
        return 1
    print('verified: all %d bytes read back as intended (md5 %s)' % (size, hashlib.md5(after).hexdigest()))
    for lvl, t in decode(after):
        print('  [%-4s] %s' % (lvl, t))
    return 0


def c_write(a):
    data = bytes.fromhex(a.hex.replace(' ', ''))
    return write_bytes(Ctrl(), a.addr, {a.offset + i: b for i, b in enumerate(data)}, a.yes)


def c_restore(a):
    img = open(a.file, 'rb').read()
    return write_bytes(Ctrl(), a.addr, dict(enumerate(img)), a.yes)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest='c', required=True)
    anum = lambda s: int(s, 0)
    sp.add_parser('find').set_defaults(f=c_find)
    sp.add_parser('ident').set_defaults(f=c_ident)
    p = sp.add_parser('rst'); p.add_argument('action', nargs='?', default='get', choices=['get', 'low', 'high', 'pulse'])
    p.add_argument('ms', nargs='?', type=int, default=1000); p.set_defaults(f=c_rst)
    sp.add_parser('adc').set_defaults(f=c_adc)
    sp.add_parser('temp').set_defaults(f=c_temp)
    sp.add_parser('i2c-scan').set_defaults(f=c_scan)
    p = sp.add_parser('eeprom-dump'); p.add_argument('--addr', type=anum, default=EEPROM_DEFAULT)
    p.add_argument('--out'); p.set_defaults(f=c_dump)
    p = sp.add_parser('eeprom-decode'); p.add_argument('file'); p.set_defaults(f=c_decode)
    p = sp.add_parser('eeprom-write'); p.add_argument('--addr', type=anum, default=EEPROM_DEFAULT)
    p.add_argument('--offset', type=anum, required=True); p.add_argument('--hex', required=True)
    p.add_argument('--yes', action='store_true'); p.set_defaults(f=c_write)
    p = sp.add_parser('eeprom-restore'); p.add_argument('file'); p.add_argument('--addr', type=anum, default=EEPROM_DEFAULT)
    p.add_argument('--yes', action='store_true'); p.set_defaults(f=c_restore)
    a = ap.parse_args()
    try:
        return a.f(a)
    except CtrlError as e:
        print('control-port error: %s' % e); return 1


if __name__ == '__main__':
    sys.exit(main())
