#!/usr/bin/env python3
"""Capture the over-the-air signal on the second board and plot it.

Three cases, all received by the RX board at 2442 MHz:
  * idle   - transmitter quiet
  * tone   - COM6 replays a +1 MHz I/Q tone (modulated frame)
  * cw     - COM6 emits an unmodulated carrier; RX tuned 1 MHz lower so the
             carrier lands at +1 MHz instead of on the DC notch
"""
import argparse
import math
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, __file__.rsplit("\\", 1)[0])
from s3_tx import RATES, Sdr, encode_iq, tone  # noqa: E402
from tx_selftest import decode, read_capture  # noqa: E402

FS = RATES["80M"]


def grab(rx):
    rx.sp.write(b"CAP20 16380 0\n")
    line, n, raw = read_capture(rx, 20)
    pairs = decode(raw, 20)
    x = np.array([complex(a, b) for a, b in pairs], dtype=np.complex128)
    return line, x


def spectrum(x):
    win = np.hanning(len(x))
    spec = np.fft.fftshift(np.fft.fft(x * win)) / (len(x) / 2)
    freq = np.fft.fftshift(np.fft.fftfreq(len(x), 1 / FS))
    return freq, 20 * np.log10(np.maximum(np.abs(spec) / 512.0, 1e-12))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tx-port", default="COM6")
    ap.add_argument("--rx-port", default="COM7")
    ap.add_argument("--freq", type=int, default=2442)
    ap.add_argument("--offset", type=float, default=1000.0)
    ap.add_argument("--gain", type=int, default=20)
    ap.add_argument("--out", default=r"D:\test\esp-sdr\docs\tx-ota.png")
    args = ap.parse_args()

    tx = Sdr(args.tx_port)
    rx = Sdr(args.rx_port)
    cases = {}
    try:
        tx.sync()
        rx.sync()
        rx.gain(args.gain)
        tx.tune(args.freq)
        rx.tune(args.freq)

        line, x = grab(rx)
        cases["idle"] = (line, x)
        print("idle ", line, f"rms={np.sqrt(np.mean(np.abs(x) ** 2)):.1f}")

        payload = encode_iq(tone(16380, args.offset * 1e3, FS, 200), 16)
        print("tx", tx.upload("REPLAY16", 16380, "80M", 16, payload))
        tx.cmd("REPLAY KEEP")
        line, x = grab(rx)
        cases["tone"] = (line, x)
        print("tone ", line, f"rms={np.sqrt(np.mean(np.abs(x) ** 2)):.1f}")
        tx.cmd("REPLAY STOP")

        tx.cmd("CW START")
        rx.tune(args.freq - 1)
        line, x = grab(rx)
        cases["cw"] = (line, x)
        print("cw   ", line, f"rms={np.sqrt(np.mean(np.abs(x) ** 2)):.1f}")
        tx.cmd("CW STOP")
        rx.tune(args.freq)
    finally:
        try:
            tx.cmd("REPLAY STOP")
            tx.cmd("CW STOP")
        except Exception:
            pass
        tx.close()
        rx.close()

    fig, axes = plt.subplots(2, 1, figsize=(11, 7), sharex=True)
    labels = {"idle": "idle (no transmission)", "tone": "REPLAY +1 MHz I/Q tone",
              "cw": "CW carrier, RX 1 MHz low"}
    colors = {"idle": "#999999", "tone": "#1f77b4", "cw": "#d62728"}
    for ax, key in zip(axes, ("tone", "cw")):
        f, s = spectrum(cases[key][1])
        f0, s0 = spectrum(cases["idle"][1])
        ax.plot(f0 / 1e6, s0, color="#cccccc", lw=0.8, label=labels["idle"])
        ax.plot(f / 1e6, s, color=colors[key], lw=0.9, label=labels[key])
        ax.set_ylim(-110, 0)
        ax.set_ylabel("dBFS")
        ax.grid(alpha=0.3)
        ax.legend(loc="upper right", fontsize=9)
        ax.set_title(f"{labels[key]} - {cases[key][0].split()[-1]} us capture")
    axes[-1].set_xlabel(f"baseband offset from {args.freq} MHz (MHz)")
    axes[-1].set_xlim(-2.5, 2.5)
    fig.suptitle("ESP32-S3 TX -> second board RX, over the air", fontsize=12)
    fig.tight_layout()
    fig.savefig(args.out, dpi=140)
    print("saved", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
