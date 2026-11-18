"""Fake-ffmpeg fuer Tests (kein echtes ffmpeg). Aufruf: python fake_decoder.py --mode M [ffmpeg-Argumente...]"""
import argparse
import struct
import sys
import time

ap = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
ap.add_argument("--mode", default="sine")
ap.add_argument("--frames", type=int, default=4800)
ns, rest = ap.parse_known_args()
url = rest[rest.index("-i") + 1] if "-i" in rest else ""
out = sys.stdout.buffer
if ns.mode == "sine":
    out.write(struct.pack("<%df" % (ns.frames * 2), *([0.25, -0.25] * ns.frames)))
    out.flush()
elif ns.mode == "fail":
    sys.stderr.write(f"Error opening input {url}: Server returned 404\n")
    sys.stderr.flush()
    sys.exit(3)
elif ns.mode == "hang":
    sys.stderr.write("started\n")
    sys.stderr.flush()
    time.sleep(60)
elif ns.mode == "flood":
    for _ in range(4000):
        sys.stderr.write("w" * 500 + " " + url + "\n")
    sys.stderr.flush()
    out.write(b"\x00" * 8)
    out.flush()
elif ns.mode == "args":
    sys.stderr.write("ARGS " + " ".join(rest) + "\n")
    sys.stderr.flush()
