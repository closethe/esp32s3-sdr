# Transmit (ESP32-S3, experimental)

This fork restores the transmit half of the S3 backend. The upstream
receive-only build removed it in `ab76232 "Cleanups"`; the reference
implementation is still in history at `e605684` (`main/s3_main.c`,
`main/continuous_replay.h`, `main/continuous_tone.h`).

## Why it works at all

The block behind burst capture is bidirectional. `adctrig` fills SRAM from the
ADC; its mirror image reads SRAM back out into the transmit baseband/DAC:

```
RX: ADC ──dump engine──▶ SRAM ──CPU──▶ host        (0x60033d5c)
TX: host ──CPU──▶ SRAM ──emit engine──▶ TX baseband (0x60033d64)
```

Both directions hand the 64 KiB aperture at `0x3fcd0000` to the engine through
`SRAM_OWNER_REG` (`0x600c101c`, low nibble = 4). A word is still
`I[9:0] | Q[19:10] | gain[27:20]`, and the transmit control register accepts:

| field | meaning |
|---|---|
| bit 31 | enable |
| bits 13:0 | sample count (words) |
| bit 15 | 1 = 80 MS/s, 0 = 40 MS/s |
| bit 19 | repeat enable |
| bits 27:20 | repeat count (0 + bit 19 = loop forever) |
| bit 18 | completion (read-only) |

`librftest.a` also exports `dactrig(n, rate)`, a factory DAC ramp trigger, but
it generates its own test pattern; arbitrary I/Q playback is done by filling
the aperture from the CPU and writing `0x60033d64`, as above.

The TX chain has to be powered explicitly: `esp_phy_wifi_tx_tone(1, 1, 80)`
turns it on, the LO is retuned, `stop_tx_tone(1)` removes the calibration tone
but leaves the chain enabled, and the frame is emitted. When done,
`esp_phy_wifi_tx_tone(0, 1, 80)` powers it down. Receive state is invalidated
afterwards and the next RX command re-prepares the radio.

## Commands

Single frame (≤ 16380 samples, ≤ rate/10):

```
TX <n> <rate> <crc32>      # IQ10, five bytes per two samples
TX16 <n> <rate> <crc32>    # IQ8, two bytes per sample
TX20 <n> <rate> <crc32>    # explicit IQ10
```

The host answers `READY`, uploads `n` samples, and gets `SENT <samples>
<cycles>`, `ERR crc` or `ERR upload_timeout`.

Repeat upload per frame:

```
TXRUN <n> <rate> <repeats> <fmt 16|20>
```

Answer `RUN`, then for each frame upload four bytes of CRC32 followed by the
payload; each frame is acknowledged with `SENT`. Ends with `END`.

Continuous carrier from one uploaded frame (modem-local loop):

```
REPLAY16 <n> <rate> <crc32>
REPLAY20 <n> <rate> <crc32>
REPLAY KEEP | REPLAY STOP | REPLAY?
```

`REPLAY` holds a five second lease; send `REPLAY KEEP` at least every five
seconds or it stops itself. Any other radio command also stops it.

Single tone (no I/Q upload; frequency is the current LO):

```
CW START | CW KEEP | CW STOP | CW?
```

Also lease-bound to five seconds.

Loopback self test (transmit one frame, then capture):

```
LOOP16 <n> <rate> <repeats=1> <crc32>
LOOP20 <n> <rate> <repeats=1> <crc32>
```

The `repeats` field may be omitted when it is 1. The capture runs *after* the
frame has finished, so a clean loopback capture only proves the capture path
still works; it does not show the frame on the air.

`CAPS` advertises `TX16 TX20 TXRUN REPLAY CW`.

## Host tool

```sh
python tools/s3_tx.py --port COM6 caps
python tools/s3_tx.py --port COM6 --freq 2442 --offset 1000 tone
python tools/s3_tx.py --port COM6 --freq 2442 replay --hold 5
python tools/s3_tx.py --port COM6 --freq 2442 cw --hold 3
python tools/s3_tx.py --port COM6 stop
```

`--file` uploads a raw IQ8 or IQ10-packed file instead of the generated tone.

## Limits

- Only 40 and 80 MS/s; no intermediate dump clocks for TX.
- One frame ≤ 16380 words because the count field and aperture are 14 bits.
- `REPLAY` loops a fixed buffer; changing content means stopping and re-uploading.
- No power control: the output level follows the gain index and board variance,
  and there is no calibrated dBm.
- Half duplex: RX and TX share the front end; state is rebuilt between them.
- Spectral mask, harmonics and LO leakage are uncharacterized.
- Calibration/retune still emits through the antenna even in receive-only builds.

Transmit only where you are licensed to (2.4 GHz ISM in most jurisdictions) and
at the lowest level that works; modifying certified firmware voids the module
approval.

## Building on a memory-constrained host

A full debug build of the C++ parts of `esp-dsp` peaks above 1 GB in a single
`cc1plus` process. On hosts with a tight per-process limit the build dies with
`cc1plus.exe: out of memory allocating 65536 bytes`. Configure once with the
slim option, which drops `-g0` into every compile line and shrinks the peak:

```
cmake ... -DESP_SDR_SLIM_BUILD=ON     # or: idf.py -B build-s3 -DESP_SDR_SLIM_BUILD=ON build
```

Parallelism matters too: `ninja -j 3` keeps two or three compilers inside the
budget. Symbols are lost, so panic backtraces are less useful; build without
this option once the host has room.

## Flashing

The board on this bench is a native USB Serial/JTAG device (`VID_303A PID_1001`)
on COM6 with a 16 MB flash. Back up the whole chip before the first write:

```
python -m esptool --chip esp32s3 -p COM6 -b 921600 read_flash 0 0x1000000 backup\com6_original_16mb.bin
python <venv>\Scripts\python.exe %IDF_PATH%\tools\idf.py -B build-s3 -DSDKCONFIG=build-s3/sdkconfig -p COM6 -b 921600 flash
```

`sdkconfig` must exist in the build directory (copy `sdkconfig.defaults.esp32s3`
into `build-s3\sdkconfig`); passing `-DSDKCONFIG_DEFAULTS` gets rewritten to
`-DSDKCONFIG_DEFAULTS=sdkconfig` and fails. `ESP_ROM_ELF_DIR` has to be set to
`%IDF_TOOLS_PATH%\tools\esp-rom-elfs\<version>` or the bootloader configure step
aborts.

## Verification (2026-11-17, this fork)

`tools/tx_selftest.py` walks the whole command set; both IQ8 and IQ10 paths pass
with `FAILURES: 0`:

| Step | Firmware answer |
| --- | --- |
| `CAPS` | `... TX16 TX20 TXRUN REPLAY CW` |
| `CAP20 16380 0` before | `DATA 16380 <crc> 213` |
| `TX16 4096 80000000 <crc>` | `SENT 4096 14218 0` |
| corrupted CRC | `ERR crc` |
| `REPLAY16 ...` / `KEEP` / `STOP` | `PLAYING` / `REPLAY ON 4096 80000000` / `OK` |
| `CW START` / `KEEP` / `STOP` | `OK` / `OK` / `OK` |
| `LOOP16 ...` | `DATA 4096 <crc> ~60` |
| `CAP20` after | `DATA 16380 <crc> 207` |

The `SENT` cycle count is the useful number: 4096 samples at 80 MS/s are
4096 × 3 = 12 288 core cycles, and the hardware completion bit came back after
14 218, i.e. the dump engine consumed the frame in real time instead of timing
out. `tools/loop_verify.py` compares a tone frame with a silent frame through
`LOOP`; it reports no difference, which is expected because the capture starts
after the frame ended.

Not verified here: absolute radiated power in dBm, the regulatory spectral mask
and harmonics far from the carrier. The link itself was proven over the air with
a second ESP32-S3 board (`COM7`, receive-only firmware) on the same desk:

| Case (RX at 2442 MHz, manual gain 20) | RX RMS | Line at +1 MHz |
| --- | --- | --- |
| idle, transmitter quiet | 2.6 LSB | 0.04 LSB |
| `REPLAY` all-zero frame | 22.3 LSB | 0.09 LSB |
| `REPLAY` +1 MHz tone frame | 670 LSB | 313 LSB (**+78.5 dB**) |
| `CW` carrier, RX tuned 1 MHz low | 654 LSB | ~0 dBFS |

`tools/ota_check.py` reproduces the numbers, `tools/ota_spectrum.py` draws
`docs/tx-ota.png`. Two things the plot makes obvious:

* the `REPLAY` frame is looped at its 205 us boundary, so a tone played this way
  has a raised noise skirt in both directions - feed continuous data over
  `IQS`-style streaming or shorten the lease if you need a clean spectrum;
* harmonics sit at +-1, +-2 MHz from the carrier (DAC/front-end products), so
  keep the I/Q amplitude well below full scale (200/512 was used here).

Absolute power still needs a calibrated receiver or a spectrum analyser; an
unmodified module is also only type-approved with its certified firmware.

## Measured transmit window

Swept with `REPLAY` (the real I/Q path, not the PHY test tone), +1 MHz tone,
60/512 amplitude, receiver on 2442 MHz +-0 dBm gain index 20
(`tools/ota_range.py`):

| LO | +1 MHz line (LSB) | Note |
| --- | --- | --- |
| 2100, 2200 MHz | 0.17 / 0.03 | transmit chain muted |
| 2250 MHz | 84 | usable, ~-17 dB from peak |
| 2280 MHz | 161 | |
| 2320-2360 MHz | 370-520 | |
| 2400-2520 MHz | 535-563 | flat top |
| 2550 MHz | 275 | |
| 2600 MHz | 81 | edge |
| 2650 MHz | 12 | almost gone |
| 2700, 2750 MHz | 5-7 | transmit chain muted |

So the transmit window is roughly **2250-2650 MHz, best 2360-2520 MHz**. This
is narrower than the receive range (1842-2750 MHz): the LO and the receive
chain tune that widely, but the transmit chain only gets its 2.4 GHz
configuration (PA bias, matching, band select) from the PHY, so outside that
window `emit()` still completes but almost nothing leaves the antenna.

Modulation bandwidth (`REPLAY` tones at increasing offset, receiver kept wide so
this is the transmit side; `BANDWIDTH 0` and `BANDWIDTH 20` behave the same):

| Offset | Level |
| --- | --- |
| 0.1-10 MHz | flat, 52-55 dB (relative) |
| 12 MHz | -6 dB |
| 16 MHz | -26 dB |
| 20 MHz | -38 dB |
| 25 MHz | -48 dB |

Practically **+-10 MHz of usable modulation bandwidth** (~20 MHz wide) at
80 MS/s; the 40 MHz Nyquist is not reachable because the DAC reconstruction and
baseband filter roll off first. The `BANDWIDTH` command does not change the
transmit response measurably.

## Which receiver can hear it

At 2.4 GHz the usual hobby receivers mostly cannot:

| Receiver | Tuning range | Works? |
| --- | --- | --- |
| RTL-SDR (R820T2 dongle) | 24-1766 MHz | No - far out of band; needs an external downconverter |
| RTL-SDR + downconverter (e.g. Ham-It-Up) | downconverted into 24-1766 MHz | Yes, narrowband only; 2.4 MS/s cannot see the 20 MHz-wide signal |
| HackRF One | 1 MHz-6 GHz | Yes, 20 MS/s covers the useful bandwidth |
| ADALM-PLUTO / LimeSDR / USRP | up to 3.8-6 GHz | Yes |
| Another ESP32-S3 with this firmware | 1842-2750 MHz | Yes, this is what the tests above used |

RTL-SDR also tops out around 2.4-3.2 MS/s (8 MS/s with hacks), so even with a
downconverter it can only observe a CW carrier or a narrow slice, not the full
modulated signal.
