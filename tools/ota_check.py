#!/usr/bin/env python3
"""Over-the-air check: COM6 transmits with the restored TX path, COM7 receives.

The two boards sit on the same desk, so even without antennas the receiver
sees the frame through near-field coupling. The test compares three RX
captures taken at the same LO:

  * idle            - nothing transmitted (noise reference)
  * silent frame    - REPLAY running, all-zero I/Q
  * tone frame      - REPLAY running, a tone at +1 MHz

A tone line at +1 MHz that only appears in the third case is the proof that the
I/Q really reaches the antenna.
"""
import argparse
import math
import sys
import time

sys.path.insert(0, __file__.rsplit("\\", 1)[0])
from s3_tx import RATES, Sdr, encode_iq, tone  # noqa: E402
from tx_selftest import decode, read_capture  # noqa: E402


def probe(pairs, rate, centre, span):
    """Best Goertzel magnitude in centre +- span, returns (level, freq)."""
    best = (0.0, 0.0)
    step = max(1000.0, span / 40.0)
    f = centre - span
    while f <= centre + span:
        w = 2 * math.pi * f / rate
        coeff = 2 * math.cos(w)
        s1 = s2 = 0j
        for a, b in pairs:
            s0 = complex(a, b) + coeff * s1 - s2
            s2, s1 = s1, s0
        x = s1 - s2 * complex(math.cos(w), -math.sin(w))
        level = abs(x) / len(pairs)
        if level > best[0]:
            best = (level, f)
        f += step
    return best


def rms(pairs):
    return math.sqrt(sum(a * a + b * b for a, b in pairs) / len(pairs))


def capture(rx):
    rx.sp.write(b"CAP20 16380 0\n")
    line, n, raw = read_capture(rx, 20)
    return line, decode(raw, 20)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tx-port", default="COM6")
    ap.add_argument("--rx-port", default="COM7")
    ap.add_argument("--freq", type=int, default=2442)
    ap.add_argument("--samples", type=int, default=16380)
    ap.add_argument("--offset", type=float, default=1000.0, help="tone offset, kHz")
    ap.add_argument("--gain", type=int, default=20, help="manual RX gain index")
    args = ap.parse_args()

    tx = Sdr(args.tx_port)
    rx = Sdr(args.rx_port)
    try:
        for name, dev in (("tx", tx), ("rx", rx)):
            dev.sync()
            print(name, dev.cmd("INFO"))
        rx.tune(args.freq)
        If = rx.cmd("GAIN?")
        print("rx gain:", If)
        rx.gain(args.gain)
        tx.tune(args.freq)

        line, pairs = capture(rx)
        idle = probe(pairs, RATES["80M"], args.offset * 1e3, 30e3)
        print(f"idle   {line} rms={rms(pairs):.1f} lsb  "
              f"line@1MHz={idle[0]:.2f} lsb ({idle[1]/1e6:.3f} MHz)")

        # Silent frame through the transmitter.
        silence = bytes(args.samples * 2)
        reply = tx.upload("REPLAY16", args.samples, "80M", 16, silence)
        print("replay silent:", reply)
        tx.cmd("REPLAY KEEP")
        line, pairs = capture(rx)
        quiet = probe(pairs, RATES["80M"], args.offset * 1e3, 30e3)
        print(f"silent {line} rms={rms(pairs):.1f} lsb  "
              f"line@1MHz={quiet[0]:.2f} lsb ({quiet[1]/1e6:.3f} MHz)")
        tx.cmd("REPLAY STOP")

        # Tone frame at +1 MHz.
        payload = encode_iq(tone(args.samples, args.offset * 1e3, RATES["80M"], 200), 16)
        reply = tx.upload("REPLAY16", args.samples, "80M", 16, payload)
        print("replay tone:", reply)
        tx.cmd("REPLAY KEEP")
        line, pairs = capture(rx)
        loud = probe(pairs, RATES["80M"], args.offset * 1e3, 30e3)
        print(f"tone   {line} rms={rms(pairs):.1f} lsb  "
              f"line@1MHz={loud[0]:.2f} lsb ({loud[1]/1e6:.3f} MHz)")
        tx.cmd("REPLAY STOP")

        d_loud = 20 * math.log10(max(loud[0], 1e-9) / max(idle[0], 1e-9))
        d_quiet = 20 * math.log10(max(quiet[0], 1e-9) / max(idle[0], 1e-9))
        print(f"tone vs idle   = {d_loud:+.1f} dB")
        print(f"silent vs idle = {d_quiet:+.1f} dB")
        ok = d_loud > 10.0 and d_loud > d_quiet + 10.0
        print("VERDICT:", "I/Q reaches the air and is demodulated by the second board" if ok
              else "no OTA tone seen - check antennas/positioning, gain or board pairing")
        return 0 if ok else 1
    finally:
        try:
            tx.cmd("REPLAY STOP")
        except Exception:
            pass
        tx.close()
        rx.close()


if __name__ == "__main__":
    sys.exit(main())
