# bitLode firmware

RP2040 firmware for the bitLode board. It turns one USB connection into two serial ports:
a **control port** (I2C, GPIO, ADC, LED, system commands) and a **data port** that passes
UART traffic straight through to the hashboard ASIC chain.

| | |
|---|---|
| USB VID:PID | `e3be:40e1` |
| Manufacturer / product | `256F` / `EmberOne00` |
| Serial number | murmur3 hash of the SPI flash's 64-bit unique ID (different on every board) |
| Identity string | `ember-one kf1950 pio96 +sys-cmds 2026-09-15` (control page `0x0F`, cmd `0x00`) |
| System clock | 96 MHz from the 12 MHz crystal, so the PIO UART divides exactly to 12 Mbaud |

Prebuilt images of this source are in [`bin/`](bin/): `bitlode-fw.elf` for SWD and `bitlode-fw.uf2` for the USB bootloader.

## Building

```sh
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
rustup target add thumbv6m-none-eabi

cargo build --release --locked
# output: target/thumbv6m-none-eabi/release/firmware (ELF)
```

## Flashing

**SWD** (any debug probe, e.g. a Pico running Debugprobe, on SWCLK / SWDIO / GND):

```sh
openocd -f interface/cmsis-dap.cfg -f target/rp2040.cfg -c "adapter speed 1000" \
        -c "program bin/bitlode-fw.elf verify reset exit"
# or: cargo install probe-rs-tools --locked && cargo flash --release --chip RP2040
```

**USB bootloader.** If the board already runs this firmware, reboot it into the RP2040 ROM
bootloader with `tools/bitlode_sys.py bootsel`. On a blank board, hold BOOTSEL while plugging in USB.
Then copy `bin/bitlode-fw.uf2` onto the `RPI-RP2` drive.

## Pinout

| RP2040 pin | Function |
|---|---|
| GPIO8 / GPIO9 | ASIC UART TX / RX (PIO UART) |
| GPIO11 | `asic_resetn` (active low, idles high) |
| GPIO0 | `asic_pwr_en` (active high, idles low) |
| GPIO18 | `asic_io_pwr_en` (active high, idles low) |
| GPIO14 / GPIO15 | I2C SDA / SCL |
| GPIO26 / GPIO27 | ADC VDD / VIN |
| GPIO1 | RGB status LED (WS2812) |

## Data port (USB interface 2)

- Every byte is passed through in both directions.
- The host's baud rate is applied to the ASIC UART, including the rate set when the port is first opened.

## Control port (USB interface 0)

The baud rate doesn't matter.

**Request**

| 0 | 1 | 2 | 3 | 4 | 5 | 6… |
|---|---|---|---|---|---|---|
| LEN LO | LEN HI | ID | BUS | PAGE | CMD | DATA |

- `LEN` is the total packet length in bytes, including itself.
- `ID` is any byte; the reply echoes it.
- `BUS` is always `0x00`.

**Reply**

- **Success:** `u16 LE data length`, `ID`, `data`.
- **Error:** `u16 LE total length`, `ID`, `code`, optionally followed by an ASCII message. The codes are `0x10` timeout, `0x11` invalid command, `0x12` buffer overflow and `0xFF` message. An error reply is shorter than its length field says.

### I2C, page `0x05`

| Cmd | Data | Action |
|---|---|---|
| `0x10` | u32 LE Hz | set bus frequency |
| `0x20` | addr, bytes… | write |
| `0x30` | addr, n | read n bytes |
| `0x40` | addr, bytes…, n | write then read n bytes |

Example, reading one byte from `0x4C`: `08 00 01 00 05 30 4C 01`

### GPIO, page `0x06`

Commands are `0x00` for `asic_resetn`, `0x01` for `asic_pwr_en` and `0x02` for `asic_io_pwr_en`. To set a pin, send one data byte (`0` = low, `1` = high). To read its level, send no data.

Examples:
- Release ASIC reset: `07 00 00 00 06 00 01`
- Read ASIC reset: `06 00 00 00 06 00`

### ADC, page `0x07`

`0x50` reads VDD and `0x51` reads VIN. The reply is a raw u16 LE (12 bit).

### LED, page `0x08`

`0x10 R G B` sets the colour. Example, magenta: `09 00 00 00 08 10 FF 00 FF`

### System, page `0x0F`

| Cmd | Reply |
|---|---|
| `0x00` | firmware identity string |
| `0x01 B0 07` | ack `01`, then the board reboots into the RP2040 USB bootloader. The 2-byte key stops a garbled packet from triggering it. |

## License

GPL-3.0. See [LICENSE](LICENSE). Based on the Ember One usbserial firmware by the 256 Foundation.
