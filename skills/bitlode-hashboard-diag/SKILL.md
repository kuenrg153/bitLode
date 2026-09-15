---
name: bitlode-hashboard-diag
description: Diagnose a Whatsminer M30S (111 × KF1950) hashboard driven by a bitLode V2 (RP2040, USB e3be:40e1). Covers pre-flight, powering and holding the shared WM rail, board identity, I2C temp, hashboard EEPROM dump/decode/repair, a per-ASIC comms/register census, a full SHA256d-verified per-chip hashing test, frequency/thermal margin, symptom-to-cause tables and the harness traps that produce false results. Use for any bitLode + hashboard health check, bad-chip hunt or EEPROM work.
---

# bitLode × WM hashboard — diagnostics

The **bitLode V2** (RP2040, firmware in `firmware/` of this repo) drives a Whatsminer
M30S VE2x hashboard (111 × KF1950 in 37 × 3 series-parallel) over **TXD / RXD / RST + I2C**. The
WM control board stays in the loop **only as the PSU**: the hashboard rail is the WM's
`/sys/bitmicro/power` output.

Paths below are relative to the root of this repository. Protocol claims not marked as measured
on the wire or verified by SHA are hypotheses.

## 0. Ground rules (non-negotiable)

1. **Ask the user before energising the hashboard**, every session. They manage power manually to avoid overheating. Power the rig only for the run, and turn it off as soon as the run ends.
2. **Power-off is the FIRST statement of any abort path.** Only `kill` a PID that is set and > 1: `kill 0` kills the runner before `off` runs, and the rig stays on.
3. **Never run `rigpower.py state` as a check. It POWERS THE RIG OFF**, because opening `/dev/ttyUSB0` resets the Arduino. To check power, ask the chain instead: chip-ID broadcast, or WM sysfs.
4. **EEPROM writes need the user's explicit approval of the exact bytes**, plus a verified backup. `bitlode.py` enforces the backup and a dry run. Never write the WM's own config over SSH without approval.
5. Write every run into `sessions/<name>_<YYYYMMDD>/` with a README (asked / ran / came back). Run `python -u`, with bounded drains, a `write_timeout` on serial ports, and a `timeout` wrapper around everything.

## 1. Rig map (verify each session; things move)

| Item | Typical value | How to confirm |
|---|---|---|
| bitLode ctrl / data | USB interface 00 / 02 of `e3be:40e1` (e.g. `/dev/ttyACM0` / `/dev/ttyACM1`) | `bitlode.py find`. With several bitLodes attached, set `BITLODE_CTRL` / `BITLODE_DATA` |
| bitLode serial | unique per board (hash of the flash unique ID) | `bitlode.py find` |
| Firmware | `ember-one kf1950 pio96 +sys-cmds 2026-09-15` | `bitlode.py ident` |
| Rig power | Arduino `/dev/ttyUSB0`, **SSR on GPIO8: `8H` = ON, `8L` = OFF** | `tools/rigpower.py on\|off` only |
| WM control board | DHCP address on the rig LAN (it moves; `wm_ssh.py` hardcodes the IP) | `ping`; API `{"cmd":"summary"}` on :4028 |
| WM shell | SSH is off after every boot. Enable it with `enable_ssh_verbose.py <IP>` from whatsminer_private_api_py, then `tools/wm.sh '<cmd>'` (set `WM_API_DIR`) | `wm.sh 'echo ok'` |
| Rail sysfs | `/sys/bitmicro/power/{enable,vout_set(cV),vout,iout(mA),common_status}` | |
| Logic analyser (DSLogic) | Ch0 RXD · Ch1 RST · Ch2 TXD · Ch3 RST_DET (3.3 V) · Ch4-7 ASIC0/1 RXD·TXD·CTSI·RST (1.8 V) · **vth 0.9 V** | buffer mode silently REPEATS past depth |
| Python | python3 with pyserial (+ paramiko for `wm.sh`) | |

**Shared PSU:** with the WM rail off, the hashboard is dead even though the bitLode enumerates
fine on USB. A 0-chip result means nothing until you've confirmed the rail was on.

## 2. Pre-flight (hashboard unpowered, nothing risky)

```bash
S=skills/bitlode-hashboard-diag/scripts; PY=python3
lsusb | grep -E 'e3be:40e1|1a86:7523|2a0e:0034'   # bitLode, Arduino, DSLogic
$PY $S/bitlode.py find
$PY $S/bitlode.py ident      # want: "ember-one kf1950 pio96 +sys-cmds 2026-09-15"
$PY $S/bitlode.py rst get    # 1 = released (idle HIGH)
```
- A **wedged bitLode** (data port silent, ctrl port times out) needs a USB replug. Cutting VBUS does not clear it.
- **Wrong VID:PID or no SYSTEM page reply** means the board isn't running this firmware. Flash `firmware/bin/bitlode-fw.elf` over SWD (see `firmware/README.md`). On a board already running it, use `tools/bitlode_sys.py bootsel` and copy `firmware/bin/bitlode-fw.uf2` to RPI-RP2.
- Close every other holder of the bitLode ports first (`fuser /dev/ttyACM*`). Control commands collide with a running replay.

## 3. Power up and HOLD the rail (the #1 source of false "dead board")

btminer on the WM cycles and **drops the shared rail whenever it exits**. A bitLode enumeration
that lands in a rail-off window returns **0 chips**. Sequence:

1. With the user's OK: `python3 tools/rigpower.py on`, then wait for ping. **Wait until WM uptime is ≥ 180 s**; a lockdown applied earlier gets undone by the boot sequence.
2. Enable SSH (above).
3. Lockdown, all in one `wm.sh` call:
   ```sh
   mv /tmp/miner-state.log /tmp/miner-state.log.held   # keepalive.sh only respawns daemons if this exists
   /etc/init.d/system-monitor stop; /etc/init.d/btminer stop; killall btminer; sleep 2
   ini set /tmp/fan_runtime.cfg Config speed_auto_adjust 0
   echo 100 > /sys/bitmicro/fan/pwm_duty; echo 100 > /sys/class/fanspeed/pwm_duty   # fan auto mode only sees the WM board
   echo 1 > /sys/bitmicro/power/enable; echo 1181 > /sys/bitmicro/power/vout_set   # PIN the voltage; never inherit it
   ```
4. Gate numerically before any chain work: `en=1`, `common_status=0x0`, `vout` within ±25 cV of the set point, no btminer or system-monitor PID, and fan tach `speed0/1` ≥ 200.
5. Log the rail every 1–5 s for the whole run, and abort (power off first) if `en=0`, `iout > 75 A`, board temp ≥ 70 °C, or btminer or system-monitor comes back.

⚠ **Known open problem (2026-09-15):** some unidentified launcher still restarts btminer and drops `en` within about 45 s, and repeated lockdowns failed in that session. If the rail won't hold, stop and tell the user. **Do not toggle `enable` in a loop.** Repeated toggling can latch a PSU fault (`enable=1`, `vout=0`, `common_status≠0`) that only a **full AC cycle at the relay** clears.

Voltage: 1181 cV ≈ 0.318 V/chip (strings of 37). Bring-up needs **≳ 0.30 V/chip**. Initialising below about 0.27 V "succeeds" but leaves a board that echoes jobs and never hashes. Once a good init is done, hashing is fine down to 0.259 V.

## 4. Test ladder

Run in order and stop at the first failure. Each tier's pass is only meaningful if the tier before it passed.

### T1: Board identity (I2C: EEPROM + temperature sensor)
```bash
$PY $S/bitlode.py i2c-scan        # expect 0x48 (temp) + one EEPROM in 0x50-0x57
$PY $S/bitlode.py temp            # idle ~30 °C; hashing guard 70 °C
$PY $S/bitlode.py eeprom-dump     # 3 identical reads, auto-saved to captures/, decoded
```
If the I2C bus is silent unpowered, retry with the rail held, since the connector's 3.3 V may come from the board. The I2C bus never reaches the ASICs, so **EEPROM content cannot affect hashing under the bitLode**. It only configures a WM controller. See §5.

### T2: Per-ASIC comms and register census (no work, ~30 s, 198 MHz)
```bash
$PY -u $S/chain_census.py --freq 198 --reps 3 --json <session>/census.json
```
The tool resets the chain (1 s), enumerates at 375000, sends burst0/burst1, follows the switch to 2 Mbaud, writes
the per-chip nonce-base sweep + a PLL frame + a ticket mask, and reads back per chip: `0x100` chip ID,
`0x800` own nonce base (per-chip write path), `0x031` PLL, `0x051` mask, `0x24A` die temp, and `0x236`
PVT monitor. It leaves the mask parked.

| Result | Meaning |
|---|---|
| 111 enum, 111 @2M, no flags | Comms and config are good on every chip. Go to T3. |
| enum = N < 111 | Chips 0..N-1 are reachable; **chip N or the link into it** is at fault (dead die, cracked joint, a failed string/LDO) |
| 0 enum | Rail not held · bitLode wedged · board killed by over-frequency (**rail power-cycle**; soft reset returns 0) · power-up baud ≈170 k |
| enum 111 but fewer @2M | Marginal fast link, or the chain was left half-dead by a previous arm (**rail cycle**, not reset) |
| one chip silent/intermittent mid-chain, others beyond it answer | That chip's addressing/logic; the link passes through it fine |
| `W` wrong value | Per-chip write didn't land (addressing or corruption) |
| `P` wrong on all | Reading a STALE PLL: link was saturated or the write never landed. Readback must match what you *intended* this run. |
| `T`/`S` outlier, `S` = 1023 | Thermal hot spot / poor heatsink contact · saturated or bad PVT monitor. Die temp is a board profile (hottest ~40 % along), not a gradient |

⚠ Census verifies comms and register state only. **It cannot prove the cores hash**: a dead
24 MHz crystal answers UART normally and never hashes. That's what T3 is for.

### T3: Full function test: SHA256d-verified hashing, attributed per chip
Use the best-proven configuration (paced, fresh work, jobs advancing, 400 MHz fixed, n=2, 380 j/s).
It ran at 2.56 TH/s on a healthy board (2026-09-10 job-rate sweep). `KF_RESTORE_CE=1` adds about 7 % on its own, but it has never been combined with `KF_CE_FULL`.
```bash
cd tools
env KF_JOBS=fresh KF_REPEAT_BATCH=0 KF_CE_FULL=1 \
    KF_PLL_MODE=fixed KF_FREQ=400 KF_TICKET_N=2 \
    KF_N_X81=694 KF_N_LEG=320 KF_PACE_HZ=379.8 KF_RUN_S=150 KF_TAG=diag_<board> \
    timeout 400 $PY -u kf1950_rearm_replay.py | tee <session>/diag.log
#   ~33 s job-pool load, 1 s reset, 15 s wait, gates: >=100 chips enum AND >=100 @2M
#   RX -> sessions/rx_diag_<board>.bin (flushed live; override with KF_OUT_DIR)
$PY verify_nonces.py ../sessions/rx_diag_<board>.bin \
    --jobs jobpool/fresh_x81.bin --per-chip <session>/perchip.json
$PY $S/perchip_flag.py <session>/perchip.json
```
**Pass criteria (healthy reference):**
- Verifier **CONTROL PASS** (mandatory; if it fails, interpret nothing). The control needs a capture of two known-good stock-controller nonces (`x81b.pkl`); point `KF_CONTROL_PKL` at it.
- ≥ 97 % of distinct nonce events verify at ≥ 34 bits; deepest tail reaches 40+.
- Hashrate = verified/s × 2^(32+n) ≈ **2.4–2.6 TH/s** at 400 MHz, n=2, 380 j/s. This is a bitLode-drive figure, not the board rating (29–30 TH/s under the stock controller at ~668 MHz).
- `perchip_flag.py` PASS: every chip within 5σ of the board mean, and observed sd ≈ Poisson (a healthy run gives sd 10.3 vs √mean 11.0). Chips 0 and 110 hash normally.
- The post-run liveness block still reads 111 chips (`reg 0x100`, `0x051`, `0x24A`).

**Interpreting failures:**
- **Gate FATAL**: go back to T2.
- **Echoes (`byte9 ≥ 0xC0`) but 0 nonce candidates**: frequency or PLL frame, mask never unparked, or a bad init voltage.
- **Candidates but 0 verified**: almost always the **wrong `--jobs` pool**. Run the verifier against the pool the run actually sent.
- **A DEAD or WEAK chip in `perchip_flag`** while it passed T2: that chip's cores or clock are bad (comms fine, hashing not). Confirm by repeating. `KF_SOLO=<hex>` makes one chip hash alone, but the rest of the chain still clocks and draws power. ⚠ SOLO was running when a board went 111→1 on 2026-09-11, so use it briefly.
- **Board-wide low rate with a uniform spread**: voltage, temperature, job delivery (blob writes cost 100×; check `KF_PACE_HZ` is set), or baud.

### T4: Margin and stress (optional; ask first; can kill the chain)
- **Frequency ceiling:** `KF_PLL_MODE=ramp KF_FREQ_TARGET=<MHz>` (+6 MHz per 2.67 s cycle). **Never jump**: a jump to 618 kills the chain, while a ramp to it is fine. The reference board was productive to about 720 MHz and silent by 768. You can tell the chain has died when `reg 0x031` returns 0 B. **Recovery is a rail power-cycle only.**
- **Thermal soak:** at hashing load (~50 A rail) watch the `0x48` temp and `iout`. Healthy ΔI per chip over baseline is about **6 A at init/198 MHz, rising ≈3.4 A per 100 MHz** (18 A at 564). A die drawing a flat, frequency-independent current points to leakage or damage.
- A run that goes silent progressively (RX lost at 96 s, then 56 s, then no enumeration) is **cumulative heating**: fans at idle, or voltage pinned too high. Cool it and re-test before calling the silicon bad.

## 5. Hashboard EEPROM

**Part:** 24C01 (128 B) or 24C02 (256 B). Both have been seen; `eeprom-dump` detects which by the
128-B wrap. Address `0x50` through the bitLode connector. On the WM control board the slot-N
view is `/sys/devices/platform/soc/twi0/i2c-0/0-005N/eeprom` (slot 2 = `0-0052`). Scan before assuming an address.

**Layout (known-good):**
```
0x00  data_tag  fa = valid, 00 = administratively disabled (btminer asserts), ff = blank
0x01  magic 5a
0x02  pcb_len 0x14(20)   0x03  chip_len 0x1e(30)   0x04  ts_len 0x13(19)
0x05  pcb_data   "AEM1ES6F211C10X10422"
0x19  chip_data  "H3AP04-21111103 BINV03-195004D"  -> bin_version 03, chip 1950, bin_type 04, leak D
0x37  timestamp  "2021-12-14 12:30:43"          record ends exactly at 0x4a
0x77-78  ff ff (no CRC on good boards)
0x7c-7d  rated hashrate LE16 GH/s (e.g. 0x752b = 29995)
```
No checksum. What the WM checks is `tag == fa 5a` plus declared lengths that exactly match the content.

**Procedure:**
1. `eeprom-dump` (3 identical reads, saved) and **copy the file somewhere durable** before anything else.
2. `eeprom-decode FILE` shows `[BAD]` for tag, length/overrun, or non-ASCII bytes.
3. Build the fix **from the board's own values**. Never invent serials or bins. Then dry-run:
   `bitlode.py eeprom-write --offset 0x03 --hex 1e`. It prints old→new per byte and writes nothing.
4. Show the user the exact byte plan and get approval. Then add `--yes`. The tool re-backs-up to `captures/eeprom_0x50_prewrite_*.bin`, writes byte-wise (12 ms each), re-reads 3×, verifies all bytes and re-decodes. A verify failure usually means WP is tied high or it's the wrong part.
5. Revert: `bitlode.py eeprom-restore <backup.bin> --yes` (writes only the differing bytes).
6. Log it in `captures/EEPROM_CHANGELOG_<SN>.md` (before/after bytes, md5, revert command).

**Status:** bitLode I2C *reads* of the EEPROM are proven (2026-07-29). The bitLode *write* path follows the
firmware source (`0x20 addr offset byte`) and standard 24Cxx practice, but **has not yet been run on this rig**.
Treat its first use as a test: a one-byte, easily reverted change, verified afterwards. The WM-side path is proven:
`printf '\x1e' | dd of=/sys/.../0-0052/eeprom bs=1 seek=3 count=1 conv=notrunc`.

**Learned the hard way:**
- `write-eeprom-data -s N -d/-c` on the WM **silently skips the string fields**. It writes only the hashrate plus a CRC at 0x77/0x78, which then have to be restored to `ff ff`. Use `dd` for strings and the tag.
- **A record that parses is not a correct record.** A one-byte length fix made the 09-08 board parse as `M30S_V21`, 156 chips, 4 columns, leak A. All plausible, all wrong. Cross-check the derived chip count (111) and column count (3) against the hardware.
- EEPROM faults show up as **PSU errors** on the WM (`No power matched! need power: P221C, vender: 1`, `ErrorCode[422] parser eeprom error`). Five PSU hypotheses were falsified before the EEPROM was found to be the cause.
- Corruption seen: `0x14` where `A`/`0` belong, `0x29` where `V`/`2` belong, a missing space before `BIN`, and length byte `0x53` instead of `0x1e`. It was a different programming tool, not an encoding.

## 6. Symptom → first suspect

| Symptom | First suspect → check |
|---|---|
| 0 chips, adapter enumerates | Rail off (btminer cycling) → WM sysfs `en/vout`; then replug the bitLode |
| 0 chips at every baud, alternating with power cycles | Relay-op parity / Arduino DTR reset → one relay op per cycle (`run_retry.sh` pattern) |
| 1 chip, reply CRC-valid | Chip 0 and its link are fine; the chain beyond chip 0 is dead. Stress history? A rail cycle doesn't fix it ⇒ board damage |
| Chain answers no register reads after a ramp | Over-frequency death → rail power-cycle; cap the frequency below the board's ceiling |
| 111 enum, ~half answer at 2M | Degraded by the previous arm → rail cycle |
| Replay hangs, no log lines | Dead chain + `write_timeout=None` → always set `write_timeout` |
| "Hashes" but the nonce count is identical across different arms | **Censored metric** (`KF_REPEAT_BATCH=1` exhausts a fixed job set) |
| 0 verified on a good-looking run | Verifier `--jobs` doesn't match `KF_JOBS` |
| Static DSLogic line during known traffic | Upper-chip common mode 23–25 V → probe at ASIC 0/1; or `vth` wrong; or a floating channel chattering |
| `pgrep -f` wait loop never ends | The pattern matches itself → use `name[x]` or a PID |
| Temp OK on WM, bitLode board cooking | Fan auto mode only sees the WM board → force the fans, guard on `0x48` |

## 7. Protocol quick reference (for ad-hoc probes)

- Frame: `FF FF [reg>>4] [((reg&0xF)<<4)|4] [len] [data] [CRC8]`. READ = low nibble 0, 6 B: `FF FF [b2] [b3] [resp_len] [CRC]`.
- CRC-8: poly 0x31, init 0xFF, no reflection, covering all bytes including `FF FF`. No exceptions.
- Replies are addr-FIRST: `[addr] 00 00 [len] [data] [CRC]`. Never parse by `bytes // size`; scan for CRC-valid records.
- Chip ID `FF FF 10 00 06 D5` → pre-address reply `FF FF 00 06 19 50 00 00 00 00 F4` per chip, at **375000** baud (not 363636).
- After burst1 the chain is at **2 Mbaud** (this WM's config; factory is 12 M) and the host **must follow**. bitLode USB is full-speed, so link capacity plateaus around 550 KB/s (6 M is the useful ceiling).
- PLL frequency: `03 14 06 [fbHi fbLo] 04 [pd] 03 AA`, with pd = 2 below 300 MHz, 1 below 600, 0 otherwise, and fb = round(2^pd·f/6). The `04 05 [domain]` form is NOT a frequency write.
- Ticket mask `reg 0x051`: n=32 parks (no reports, echoes included); threshold = 32+n bits.
- `04 06` 11-B frames are mostly job **echoes** (byte9 ≥ 0xC0). Real nonces can only be told apart by SHA256d.
- Control port: `u16 total_len, id, 0, page, cmd`. Pages: 5 = I2C (`10` freq, `20` write, `30` read, `40` write-read), 6 = GPIO (`00` RESETN; `01` PWR_EN does nothing on M30S), 7 = ADC (not characterised here), 0x0F = SYSTEM (`00` ident, `01 B0 07` BOOTSEL). An error reply is shorter than its length field (codes 0x10/0x11/0x12/0xFF+msg).

## 8. Files

| Path | Use |
|---|---|
| `scripts/bitlode.py` | ports, ident, RESETN, ADC, temp, I2C scan, EEPROM dump/decode/write/restore |
| `scripts/chain_census.py` | T2 per-ASIC comms and register census (no power switching) |
| `scripts/perchip_flag.py` | T3 dead/weak chip detection from `verify_nonces.py --per-chip` JSON |
| `tools/kf1950_rearm_replay.py` | the hashing driver; every `KF_*` knob is documented in its header |
| `tools/verify_nonces.py` | SHA256d verifier with a mandatory control; `--per-chip`, `--shuffle` (derangement; use a ~1k-event file) |
| `tools/rigpower.py`, `wm.sh`, `bitlode_board_temp.py`, `bitlode_sys.py` | power, WM shell, temp, firmware |
| `tools/make_fresh_jobs.py` | builds `tools/jobpool/fresh_x81.bin` from `x81_jobs.bin` (run `make_fresh_jobs.py 165000` once before T3) |
| `captures/eeprom_*.bin`, `EEPROM_CHANGELOG_*.md` | EEPROM backups written by `bitlode.py` (git-ignored) |
