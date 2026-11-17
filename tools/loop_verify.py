#!/usr/bin/env python3
"""On-board evidence that the transmit frame really reaches the RF front end.

Both runs use LOOP (transmit a frame, then capture through the same chain), the
only difference being whether the frame carries a tone at +1 MHz or is silent.
A Goertzel probe at exactly 1.000 MHz compares the two captures: the line can
only be there if the modulator actually drove the front end.

This is a leakage/loopback check, not a radiated-power measurement; a spectrum
analyser or a second receiver is still required for that.
"""
import argparse
import math
import sys
import zlib

sys.path.insert(0, __file__.rsplit("\\", 1)[0])
from s3_tx import RATES, Sdr, encode_iq, tone  # noqa: E402
from tx_selftest import decode, read_capture  # noqa: E402


def goertzel(pairs, freq, rate):
    w = 2 * math.pi * freq / rate
    coeff = 2 * math.cos(w)
    s1 = s2 = 0j
    for a, b in pairs:
        s0 = complex(a, b) + coeff * s1 - s2
        s2, s1 = s1, s0
    x = s1 - s2 * complex(math.cos(w), -math.sin(w))
    return abs(x) / len(pairs)


def capture_frame(sdr, fmt, samples, rate, payload):
    reply = sdr.upload(f"LOOP{fmt}", samples, rate, fmt, payload)
    if reply not in ("READY", "READY\n") and not reply.startswith("DATA"):
        # upload() returns the reply after the payload; LOOP answers with DATA
        pass
    return read_capture(sdr, fmt)


def run(sdr, fmt, samples, rate, payload, label):
    reply = sdr.upload(f"LOOP{fmt}", samples, rate, fmt, payload)
    line, n, raw = read_capture(sdr, fmt)
    pairs = decode(raw, fmt)
    level = goertzel(pairs, 1e6, RATES[rate])
    print(f"{label}: {line} goertzel@1MHz={level:.2f} lsb (n={n})")
    return level


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", default="COM6")
    ap.add_argument("--freq", type=int, default=2442)
    ap.add_argument("--samples", type=int, default=16380)
    ap.add_argument("--fmt", type=int, choices=(16, 20), default=20)
    args = ap.parse_args()
    if args.samples * 10 > RATES["80M"]:
        ap.error("samples must satisfy n*10 <= 80e6")

    sdr = Sdr(args.port)
    try:
        sdr.sync()
        sdr.tune(args.freq)
        tone_payload = encode_iq(tone(args.samples, 1e6, RATES["80M"], 200), args.fmt)
        silence = bytes(len(tone_payload))

        silence_db = run(sdr, args.fmt, args.samples, "80M", silence, "silent frame")
        tone_db = run(sdr, args.fmt, args.samples, "80M", tone_payload, "tone frame")
        delta = 20 * math.log10(max(tone_db, 1e-9) / max(silence_db, 1e-9))
        print(f"delta = {delta:.1f} dB at 1.000 MHz")
        ok = delta > 6.0
        print("VERDICT:", "transmit frame reaches the front end" if ok
              else "no measurable coupling - check emit()/front-end switch")
        return 0 if ok else 1
    finally:
        sdr.close()


if __name__ == "__main__":
    sys.exit(main())
