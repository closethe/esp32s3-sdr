#!/usr/bin/env python3
"""Measure what the restored TX path can actually put on the air.

Two sweeps, both observed on the second board (COM7):

  freq  - CW carrier at f, RX tuned to f-1 MHz so the carrier lands on +1 MHz
          instead of on the DC notch. Any frequency where the PLL does not lock
          shows up as noise only.
  bw    - REPLAY a tone at +offset and read its level; repeated with the analog
          baseband filter wide open (BANDWIDTH 0) and at 20 MHz, which is what
          transmit() inherits from rx_filter_apply().
"""
import argparse
import math
import sys

sys.path.insert(0, __file__.rsplit("\\", 1)[0])
from s3_tx import RATES, Sdr, encode_iq, tone  # noqa: E402
from tx_selftest import decode, read_capture  # noqa: E402


def capture(rx):
    rx.sp.write(b"CAP20 16380 0\n")
    line, n, raw = read_capture(rx, 20)
    return line, decode(raw, 20)


def line_level(pairs, centre, span=15e3, rate=RATES["80M"]):
    best = 0.0
    step = span / 10.0
    f = centre - span
    while f <= centre + span:
        w = 2 * math.pi * f / rate
        coeff = 2 * math.cos(w)
        s1 = s2 = 0j
        for a, b in pairs:
            s0 = complex(a, b) + coeff * s1 - s2
            s2, s1 = s1, s0
        x = s1 - s2 * complex(math.cos(w), -math.sin(w))
        best = max(best, abs(x) / len(pairs))
        f += step
    return best


def sweep_freq(tx, rx, freqs, gain, amp=60, rate="80M", fmt=16):
    """Drive the real I/Q path: REPLAY a +1 MHz tone at each candidate LO."""
    rx.gain(gain)
    rx.cmd("BANDWIDTH 0")
    rows = []
    payload = encode_iq(tone(16380, 1e6, RATES[rate], amp), fmt)
    for f in freqs:
        tuned = tx.cmd(f"FREQ {f}")
        if tuned != "OK":
            rows.append((f, "FREQ rejected", 0.0))
            print(f"{f:5d} MHz  FREQ rejected")
            continue
        rx.tune(f)
        reply = tx.upload(f"REPLAY{fmt}", 16380, rate, fmt, payload)
        if reply != "PLAYING":
            rows.append((f, reply, 0.0))
            print(f"{f:5d} MHz  {reply}")
            continue
        tx.cmd("REPLAY KEEP")
        line, pairs = capture(rx)
        lvl = line_level(pairs, 1e6)
        tx.cmd("REPLAY STOP")
        rows.append((f, line, lvl))
        print(f"{f:5d} MHz  {line:32s} +1 MHz level {lvl:8.2f} lsb")
    rx.tune(2442)
    return rows


def sweep_bw(tx, rx, freq, bw, offsets, gain, amp=40):
    tx.cmd(f"FREQ {freq}")
    rx.tune(freq)
    rx.gain(gain)
    rx.cmd("BANDWIDTH 0")              # keep the receiver wide; sweep TX only
    tx.cmd(f"BANDWIDTH {bw}")          # transmit() inherits this filter
    level = {}
    for off in offsets:
        payload = encode_iq(tone(16380, off * 1e6, RATES["80M"], amp), 16)
        reply = tx.upload("REPLAY16", 16380, "80M", 16, payload)
        if reply != "PLAYING":
            level[off] = None
            continue
        tx.cmd("REPLAY KEEP")
        line, pairs = capture(rx)
        level[off] = line_level(pairs, off * 1e6, span=30e3)
        tx.cmd("REPLAY STOP")
        print(f"bw {bw:>3}  offset {off:5.2f} MHz  level {level[off]:8.2f} lsb")
    return level


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tx-port", default="COM6")
    ap.add_argument("--rx-port", default="COM7")
    ap.add_argument("--gain", type=int, default=0)
    args = ap.parse_args()

    tx = Sdr(args.tx_port)
    rx = Sdr(args.rx_port)
    try:
        tx.sync()
        rx.sync()
        print("freq sweep")
        sweep_freq(tx, rx, [2100, 2200, 2250, 2280, 2300, 2320, 2340, 2360,
                            2400, 2442, 2470, 2500, 2520, 2550, 2600, 2650,
                            2700, 2750], args.gain)

        offsets = [0.1, 0.5, 1, 2, 4, 6, 8, 10, 12, 16, 20, 25, 30]
        print("bandwidth sweep at 2442 MHz")
        wide = sweep_bw(tx, rx, 2442, 0, offsets, args.gain)
        narrow = sweep_bw(tx, rx, 2442, 20, offsets, args.gain)
        print("\noffset  wide(69)  20MHz")
        for off in offsets:
            w = wide.get(off) or 0.0
            nn = narrow.get(off) or 0.0
            db_w = 20 * math.log10(max(w, 1e-9))
            db_n = 20 * math.log10(max(nn, 1e-9))
            print(f"{off:5.2f}  {db_w:7.1f}  {db_n:7.1f}")
    finally:
        for dev in (tx, rx):
            for cmd in ("REPLAY STOP", "CW STOP"):
                try:
                    dev.cmd(cmd)
                except Exception:
                    pass
            dev.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
