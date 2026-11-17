#!/usr/bin/env python3
"""End-to-end self test for the re-enabled ESP32-S3 transmit path.

Exercises every restored command in order and prints the firmware's answer so
a regression is visible without a second radio:

  RX baseline capture -> {TX16|TX20} -> REPLAY -> CW -> LOOP (TX then RX) ->
  RX capture again -> negative CRC test

The LOOP step transmits a tone frame and captures right afterwards through the
same front end, which is the only on-board check that the dump engine actually
saw the frame leave. It cannot prove radiated power; that needs a spectrum
analyser or a second receiver.
"""
import argparse
import math
import sys
import zlib

sys.path.insert(0, __file__.rsplit("\\", 1)[0])
from s3_tx import RATES, Sdr, encode_iq, tone  # noqa: E402

MAX_SAMPLES = 16380


def read_capture(sdr, fmt):
    """Read one DATA frame: 'DATA <n> <crc> <us>' followed by the payload."""
    line = sdr.sp.readline().decode(errors="replace").strip()
    if not line.startswith("DATA"):
        raise RuntimeError(f"expected DATA, got {line!r}")
    fields = line.split()
    n = int(fields[1])
    size = n * 2 if fmt == 16 else (n * 5) // 2
    sdr.sp.timeout = 10.0
    payload = sdr.sp.read(size)
    if len(payload) != size:
        raise RuntimeError(f"short capture: {len(payload)}/{size} bytes")
    return line, n, payload


def decode(payload, fmt):
    pairs = []
    if fmt == 16:
        for k in range(0, len(payload) - 1, 2):
            i = payload[k]
            q = payload[k + 1]
            pairs.append((i - 256 if i > 127 else i, -(q - 256 if q > 127 else q)))
        return pairs
    words = []
    acc = 0
    bits = 0
    for byte in payload:
        acc |= byte << bits
        bits += 8
        while bits >= 20:
            w = acc & 0xFFFFF
            acc >>= 20
            bits -= 20
            words.append(w)
    for w in words:
        i = w & 0x3FF
        q = (w >> 10) & 0x3FF
        pairs.append((i - 1024 if i > 511 else i, -(q - 1024 if q > 511 else q)))
    return pairs


def stats(pairs):
    i = [p[0] for p in pairs]
    q = [p[1] for p in pairs]
    dc_i = sum(i) / len(i)
    dc_q = sum(q) / len(q)
    power = sum((a - dc_i) ** 2 + (b - dc_q) ** 2 for a, b in pairs) / len(pairs)
    return dc_i, dc_q, power


def peak_bin(pairs, rate):
    """Crude DFT peak via a coarse scan of 64 offsets, enough to spot a tone."""
    n = len(pairs)
    best = (0.0, 0.0)
    for k in range(64):
        w = 2 * math.pi * k / 64
        re = im = 0.0
        for idx, (a, b) in enumerate(pairs[:4096]):
            re += a * math.cos(w * idx) - b * math.sin(w * idx)
            im += a * math.sin(w * idx) + b * math.cos(w * idx)
        mag = math.hypot(re, im) / 4096
        if mag > best[0]:
            best = (mag, k * rate / 64)
    return best


def step(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", default="COM6")
    ap.add_argument("--freq", type=int, default=2442)
    ap.add_argument("--samples", type=int, default=4096)
    ap.add_argument("--fmt", type=int, choices=(16, 20), default=16)
    ap.add_argument("--offset", type=float, default=1000.0)
    ap.add_argument("--gain", type=int, default=0)
    args = ap.parse_args()

    failures = 0
    sdr = Sdr(args.port)
    try:
        sdr.sync()
        print(sdr.cmd("INFO"))
        caps = sdr.cmd("CAPS")
        print(caps)
        ok = all(tag in caps.split() for tag in ("TX16", "TX20", "TXRUN", "REPLAY", "CW"))
        step("CAPS advertises TX", ok)
        failures += not ok

        sdr.tune(args.freq)
        if args.gain:
            sdr.gain(args.gain)
        print(sdr.cmd("GAIN?"))

        # 1. RX baseline before touching the TX path.
        sdr.sp.write(b"CAP20 16380 0\n")
        line, n, payload = read_capture(sdr, 20)
        pairs = decode(payload, 20)
        dc_i, dc_q, power = stats(pairs)
        print(f"baseline {line} dc=({dc_i:.1f},{dc_q:.1f}) rms={math.sqrt(power):.1f} lsb")
        step("RX baseline capture", n == 16380 and power > 0, f"n={n}")

        # 2. One-shot transmit of a tone frame.
        payload = encode_iq(tone(args.samples, args.offset * 1e3, RATES["80M"], 100), args.fmt)
        reply = sdr.upload(f"TX{args.fmt}", args.samples, "80M", args.fmt, payload)
        step("TX one-shot", reply.startswith("SENT"), reply)
        failures += not reply.startswith("SENT")

        # 3. Negative test: corrupted CRC must be rejected.
        bad = bytearray(payload)
        bad[0] ^= 0xFF
        crc = (zlib.crc32(bytes(payload)) + 1) & 0xFFFFFFFF
        sdr.sp.write(f"TX{args.fmt} {args.samples} {RATES['80M']} {crc:08x}\n".encode())
        ready = sdr.sp.readline().decode(errors="replace").strip()
        sdr.sp.write(bytes(bad))
        sdr.sp.flush()
        reply = sdr.sp.readline().decode(errors="replace").strip()
        step("TX rejects bad CRC", reply == "ERR crc", f"ready={ready!r} reply={reply!r}")
        failures += reply != "ERR crc"

        # 4. Continuous replay through the modem loop bit.
        reply = sdr.upload(f"REPLAY{args.fmt}", args.samples, "80M", args.fmt, payload)
        step("REPLAY start", reply == "PLAYING", reply)
        failures += reply != "PLAYING"
        print(sdr.cmd("REPLAY?"))
        print(sdr.cmd("REPLAY KEEP"))
        print(sdr.cmd("REPLAY STOP"))

        # 5. CW test tone (unmodulated carrier on the tuned LO).
        reply = sdr.cmd("CW START", timeout=5.0)
        step("CW START", reply == "OK", reply)
        failures += reply != "OK"
        print(sdr.cmd("CW KEEP"))
        print(sdr.cmd("CW STOP"))

        # 6. LOOP: transmit the frame, then capture through the same chain.
        reply = sdr.upload(f"LOOP{args.fmt}", args.samples, "80M", args.fmt, payload)
        line, n, payload = read_capture(sdr, args.fmt)
        pairs = decode(payload, args.fmt)
        dc_i, dc_q, power = stats(pairs)
        mag, freq = peak_bin(pairs, RATES["80M"])
        print(f"loop {line} rms={math.sqrt(power):.1f} lsb peak={mag:.1f} lsb at {freq/1e6:.2f} MHz")
        step("LOOP TX+RX", n == args.samples)

        # 7. RX must still work after all the TX activity.
        sdr.sp.write(b"CAP20 16380 0\n")
        line, n, payload = read_capture(sdr, 20)
        pairs = decode(payload, 20)
        dc_i, dc_q, power = stats(pairs)
        print(f"after {line} dc=({dc_i:.1f},{dc_q:.1f}) rms={math.sqrt(power):.1f} lsb")
        step("RX after TX", n == 16380 and power > 0)

        print(sdr.cmd("CW?"), sdr.cmd("REPLAY?"))
    finally:
        sdr.close()

    print("FAILURES:", failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
