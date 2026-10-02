"""Checks for webui.py file-name handling - plain Python.

    python tests/test_webui.py
"""
import glob
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = (sorted(glob.glob(os.path.join(HERE, "..", "V*")), key=lambda p: [int(x) for x in os.path.basename(p)[1:].split(".") if x.isdigit()]) or [os.path.join(HERE, "..", "cli_render_launcher")])[-1]
sys.path.insert(0, SRC)

import webui as W  # noqa: E402

FAILS = []


def check(label, got, want):
    ok = got == want
    print("  %s  %-54s %r" % ("PASS" if ok else "FAIL", label, got))
    if not ok:
        FAILS.append(label)
        print("        want %r" % (want,))


print("Frame number in a file name")
check("frame at the end", W.frame_of("base0900.jpg", "base"), 900)
check("digits inside the stem ignored", W.frame_of("shot2_0481.png", "shot2_"), 481)
check("'####beauty' puts the frame first (5.2.2)", W.frame_of("0905beauty.exr", ""), 905)
check("no digits: no frame", W.frame_of("beauty.exr", ""), None)

print("\nAn empty stem lists the node's whole folder")
tmp = tempfile.mkdtemp()
try:
    for name in ("0905beauty.exr", "0906beauty.exr"):
        open(os.path.join(tmp, name), "wb").close()
    check("both files found", sorted(W._scan(os.path.join(tmp, ""))), ["0905beauty.exr", "0906beauty.exr"])
finally:
    shutil.rmtree(tmp)

print()
if FAILS:
    print("%d FAILED: %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("All checks passed.")
