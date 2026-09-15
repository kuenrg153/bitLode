## bitLode V2
<img width="1553" height="914" alt="image" src="https://github.com/kuenrg153/bitLode/artwork/bitLode.jpg" />

bitLode V2 is an RP2040 USB adapter that connects a PC, laptop or Raspberry Pi to a Whatsminer M30 (and similar)
hashboard. It exposes the hashboard's UART, reset and I2C over USB.

It's a fork of [aditBoard v2](https://github.com/skot/aditBoard/tree/v2) by Skot. It changes the
board for Whatsminer hashboards and adds firmware, host tools and an AI diagnostics skill.

| Path | Contents |
|---|---|
| `bitLodeV2.kicad_pro` / `.kicad_sch` / `.kicad_pcb` | KiCad 10 project: schematic and 6-layer PCB |
| `bitLodeV2.kicad_sym`, `bitLodeV2.pretty/`, `bitLodeV2.3dshapes/` | project-local symbols, footprints and 3D models |
| `Manufacturing Files/` | Gerbers, drill files, BOM and component placement for PCBA (e.g. [JLCPCB](https://jlcpcb.com)) |
| [`firmware/`](firmware/) | RP2040 firmware source (Rust/Embassy) with prebuilt `bin/bitlode-fw.elf` and `bin/bitlode-fw.uf2` |
| [`tools/`](tools/) | Python host tools: port discovery, system commands, hashboard init/replay, nonce verification, rig power |
| [`skills/`](skills/) | AI agent skill for diagnosing a Whatsminer M30S (KF1950) hashboard through the bitLode |

### Quick start

1. **Hardware** Send `Manufacturing Files/gerbers/`, the BOM and the placement file to your PCB assembler. Some notes: 

a) Fab houses might try to replace the cyrstal with an oscillator (learned this the hard way) - don't let them!

b) The LED is hard to source - but it works fine with out it!

c) Just do SMT at the fab house - solder the connector yourself and save a little bit. You can even try to sample the connector for free from Samtec - SQT-107-01-L-D-RA
<img width="1553" height="914" alt="image" src="https://github.com/kuenrg153/bitLode/artwork/bitLode_2sides.jpg" />


2. **Firmware** 
The firmware is based on https://github.com/256foundation/emberone-usbserial-fw with a few modifications around baud rates.

If you have trouble flashing it via the regular uf2 over boot loader then you can try using a Pi Pico running Debugprobe
https://www.electronics-lab.com/understanding-the-ways-to-debug-your-raspberry-pi-pico-development-board/

If you go this route then it's really handy to get an adapter ( https://www.aliexpress.com/item/1005009527726103.html ) to bring out the SWD lines.

To build it yourself, see [`firmware/README.md`](firmware/README.md).
   
3. **Plug in USB.** The board appears as `e3be:40e1` with two serial ports: control (interface 0) and ASIC UART (interface 2).
   ```sh
   pip install pyserial
   python3 tools/bitlode_sys.py ident   # -> ember-one kf1950 pio96 +sys-cmds 2026-09-15
   ```

### AI skill
The point of these skills is to help people learn how to communicate with, configure and diagnose Whatsminer M30 hashboards. 

I haven't yet done it but you could pair these skills up with Mujina (https://github.com/256foundation/mujina) and actually hash. Note that the RP2040 canNOT keep up with the 12Mbaud native baud rate required to fully hash but maybe someone can run with this project and figure that out.

`skills/bitlode-hashboard-diag/` is a [Claude Code skill](https://docs.claude.com/en/docs/claude-code/skills).
Link it into your skills directory, then run the agent from the repository root so its
relative paths (`tools/`, `sessions/`, `captures/`) resolve:

```sh
mkdir -p ~/.claude/skills
ln -s "$PWD/skills/bitlode-hashboard-diag" ~/.claude/skills/bitlode-hashboard-diag
```

Before its hashing test (T3), the skill needs:
- the job pool, generated once with `cd tools && python3 make_fresh_jobs.py 165000`
- the `whatsminer_private_api_py` package (its `wm_ssh.py`) for `tools/wm.sh`, pointed to by `WM_API_DIR`
- a known-good nonce capture for the verifier's control, pointed to by `KF_CONTROL_PKL`

### License

Firmware: GPL-3.0 (see `firmware/LICENSE`).
