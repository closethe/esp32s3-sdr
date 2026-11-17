#!/usr/bin/env python3
"""Host-side transmit client for the ESP32-S3 burst SDR firmware.

The board plays I/Q straight out of the dump aperture at 40 or 80 MS/s, so a
single frame is at most 16380 samples (205 us at 80 MS/s). REPLAY loops one
uploaded frame inside the modem for a continuous carrier.

Transmit only inside a band you are allowed to use (2.4 GHz ISM in most
jurisdictions) and at the lowest power that works.
"""
import argparse
import math
import struct
import sys
import time
import zlib

import serial

RATES = {"80M": 80000000, "40M": 40000000}
MAX_SAMPLES = 16380


def sample_count(length, fmt):
    return length // 2 if fmt == 16 else (length * 8) // 20


class Sdr:
    def __init__(self, port, baud=2000000, timeout=2.0):
        self.sp = serial.Serial(port, baud, timeout=timeout)
        self.sp.reset_input_buffer()

    def close(self):
        self.sp.close()

    def cmd(self, line, timeout=2.0):
        self.sp.timeout = timeout
        self.sp.write(line.encode() + b"\n")
        return self.sp.readline().decode(errors="replace").strip()

    def sync(self, nonce=0x5A5A):
        for _ in range(20):
            self.sp.write(b"SYNC %d\n" % nonce)
            if self.sp.readline().decode(errors="replace").strip() == f"SYNC {nonce}":
                return
        raise RuntimeError("no SYNC reply")

    def tune(self, mhz, fofs_khz=0):
        if self.cmd(f"FREQ {mhz}") != "OK":
            raise RuntimeError(f"FREQ {mhz} rejected")
        if fofs_khz and self.cmd(f"FOFS {fofs_khz}") != "OK":
            raise RuntimeError(f"FOFS {fofs_khz} rejected")

    def gain(self, index):
        if self.cmd(f"GAIN MANUAL {index}") != "OK":
            raise RuntimeError(f"GAIN MANUAL {index} rejected")

    def upload(self, head, samples, rate, fmt, payload):
        crc = zlib.crc32(payload) & 0xFFFFFFFF
        self.sp.write(f"{head} {samples} {RATES[rate]} {crc:08x}\n".encode())
        reply = self.sp.readline().decode(errors="replace").strip()
        if reply != "READY":
            raise RuntimeError(f"expected READY, got {reply!r}")
        self.sp.write(payload)
        self.sp.flush()
        return self.sp.readline().decode(errors="replace").strip()


def encode_iq(samples, fmt=16):
    """samples: iterable of (i, q) pairs; IQ8 range -128..127, IQ10 -512..511."""
    if fmt == 16:
        return bytes(b & 0xFF for s in samples for b in (s[0], s[1]))
    out = bytearray()
    it = iter(samples)
    for a in it:
        b = next(it, (0, 0))
        w = ((a[0] & 0x3FF) | ((a[1] & 0x3FF) << 10) |
             ((b[0] & 0x3FF) << 20) | ((b[1] & 0x3FF) << 30)) & ((1 << 40) - 1)
        out += struct.pack("<Q", w)[:5]
    return bytes(out)


def tone(n, offset_hz, rate, amplitude=100):
    step = 2 * math.pi * offset_hz / rate
    for k in range(n):
        yield (int(round(amplitude * math.cos(step * k))),
               int(round(amplitude * math.sin(step * k))))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", default="COM6")
    ap.add_argument("--freq", type=int, default=2442, help="LO in whole MHz")
    ap.add_argument("--fofs", type=int, default=0, help="PLL offset in kHz")
    ap.add_argument("--gain", type=int, default=0, help="manual gain index")
    ap.add_argument("--rate", choices=sorted(RATES), default="80M")
    ap.add_argument("--samples", type=int, default=MAX_SAMPLES)
    ap.add_argument("--offset", type=float, default=1000.0, help="baseband tone offset, kHz")
    ap.add_argument("--fmt", type=int, choices=(16, 20), default=16)
    ap.add_argument("--file", help="raw IQ file instead of a generated tone")
    ap.add_argument("--hold", type=float, default=3.0, help="seconds for cw/replay")
    ap.add_argument("mode", choices=("caps", "tone", "tx", "replay", "cw", "stop"))
    args = ap.parse_args()

    sdr = Sdr(args.port)
    try:
        sdr.sync()
        sdr.tune(args.freq, args.fofs)
        if args.gain:
            sdr.gain(args.gain)

        if args.mode == "caps":
            print(sdr.cmd("INFO"))
            print(sdr.cmd("CAPS"))
            return 0
        if args.mode == "stop":
            print(sdr.cmd("REPLAY STOP"), sdr.cmd("CW STOP"))
            return 0
        if args.mode == "cw":
            if sdr.cmd("CW START") != "OK":
                raise RuntimeError("CW START rejected")
            end = time.time() + args.hold
            while time.time() < end:
                time.sleep(max(0.1, min(3.0, end - time.time())))
                sdr.cmd("CW KEEP")
            print(sdr.cmd("CW STOP"))
            return 0

        if args.file:
            payload = open(args.file, "rb").read()
        else:
            payload = encode_iq(tone(args.samples, args.offset * 1e3, RATES[args.rate]), args.fmt)
        n = sample_count(len(payload), args.fmt)
        if n > MAX_SAMPLES:
            raise RuntimeError(f"{n} samples exceed the {MAX_SAMPLES}-sample aperture")
        if n * 10 > RATES[args.rate]:
            raise RuntimeError(f"{n} samples exceed one {args.rate} frame (rate/10)")

        head = ("TX" if args.mode == "tx" else "REPLAY") + str(args.fmt)
        reply = sdr.upload(head, n, args.rate, args.fmt, payload)
        if args.mode == "tx":
            if not reply.startswith("SENT"):
                raise RuntimeError(reply)
            print(reply)
        else:
            if reply != "PLAYING":
                raise RuntimeError(reply)
            print(f"PLAYING {n} samples at {args.rate}")
            time.sleep(args.hold)
            print(sdr.cmd("REPLAY STOP"))
    finally:
        sdr.close()


if __name__ == "__main__":
    sys.exit(main())
