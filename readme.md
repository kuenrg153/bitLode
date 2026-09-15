## bitLode V2

bitLode V2 is an RP2040 USB adapter that connects a PC, laptop or Raspberry Pi to a miner
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

1. **Build the board.** Send `Manufacturing Files/gerbers/`, the BOM and the placement file to your PCB assembler.
2. **Flash the firmware** over SWD (SWCLK / SWDIO / GND), for example with a Pico running Debugprobe:
   ```sh
   openocd -f interface/cmsis-dap.cfg -f target/rp2040.cfg -c "adapter speed 1000" \
           -c "program firmware/bin/bitlode-fw.elf verify reset exit"
   ```
   To build it yourself, see [`firmware/README.md`](firmware/README.md).
3. **Plug in USB.** The board appears as `e3be:40e1` with two serial ports: control (interface 0) and ASIC UART (interface 2).
   ```sh
   pip install pyserial
   python3 tools/bitlode_sys.py ident   # -> ember-one kf1950 pio96 +sys-cmds 2026-09-15
   ```

### AI skill

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
