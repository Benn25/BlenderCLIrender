"""Checks for gpu_monitor.py - plain Python.

    python tests/test_gpu_monitor.py
"""
import glob
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = (sorted(glob.glob(os.path.join(HERE, "..", "V*")), key=lambda p: [int(x) for x in os.path.basename(p)[1:].split(".") if x.isdigit()]) or [os.path.join(HERE, "..", "cli_render_launcher")])[-1]
sys.path.insert(0, SRC)

import gpu_monitor as G  # noqa: E402
import runner  # noqa: E402

FAILS = []


def check(label, got, want):
    ok = got == want
    print("  %s  %-54s %r" % ("PASS" if ok else "FAIL", label, got if len(repr(got)) < 60 else "..."))
    if not ok:
        FAILS.append(label)
        print("        got  %r\n        want %r" % (got, want))


print("Parsing (real nvidia-smi line from the RTX 5080)")
gpus = G.parse("0, NVIDIA GeForce RTX 5080, 7517, 16303\n")
check("one GPU parsed", gpus, [(0, "NVIDIA GeForce RTX 5080", 7517.0, 16303.0)])
check("junk ignored", G.parse("No devices were found\n"), [])
check("two GPUs", len(G.parse("0, A, 1, 2\n1, B, 3, 4\n")), 2)


def scripted(percents, total=16000.0):
    seq = iter(percents)
    return lambda: [(0, "RTX", total * next(seq) / 100.0, total)]


print("\nAlarm logic (threshold 66, again +10, re-armed below 60)")
w = G.GpuWatch(threshold=66, reader=scripted([46, 50, 70, 72, 81, 84, 55, 68, 69]))
w.describe()                         # consumes the 46 reading
seen = [w.check() for _ in range(8)]
flat = ["%d:%s" % (i, t.split(" (")[0]) for i, msgs in enumerate(seen) for t in msgs]
check("below threshold: silent", seen[0], [])
check("first crossing alarms", seen[1][0].startswith("!! GPU MEMORY 70% FULL (10.9/15.6 GB, RTX)"), True)
check("small rise: no repeat alarm", seen[2], [])
check("+10 points: alarm again", seen[3][0].startswith("!! GPU MEMORY 81% FULL"), True)
check("84 < 81+10: quiet", seen[4], [])
check("drop below 60: all-clear", seen[5], ["   GPU memory back to 55% (RTX)"])
check("re-armed: alarms again at 68", seen[6][0].startswith("!! GPU MEMORY 68% FULL"), True)
check("peak reported, flagged", w.summary(), ["!! GPU memory peak: 84% (13.1/15.6 GB, RTX)"])

print("\nLow threshold (10%) with steady 25% - found on a real render")
low = G.GpuWatch(threshold=10, reader=scripted([25] * 20))
low.describe()
msgs = [m for _ in range(15) for m in low.check()]
check("one alarm, no alarm/all-clear loop", len(msgs), 1)

print("\nOff / unavailable")
# 5.12+: check() still samples with the alarm off (the VRAM gauge needs the
# reading), so the scripted reader must have more than one value.
off = G.GpuWatch(threshold=0, reader=scripted([99, 99, 99]))
check("threshold 0: no header, no alarm", (off.describe(), off.check()), (None, []))
none = G.GpuWatch(threshold=66, reader=lambda: None)
check("no nvidia-smi: says so", none.describe().startswith("GPU memory: not watched"), True)
check("no nvidia-smi: silent after", none.check(), [])

print("\nIn a watched render (scripted GPU, fast polling)")


class Capture:
    text = ""

    def isatty(self):
        return False

    def write(self, s):
        Capture.text += s

    def flush(self):
        pass


runner.GPU_POLL_S = 0.2
body = "import time; print('00:01.000  render | Rendering frame 1'); time.sleep(1.5)"
code = runner.watch({"name": "g", "cmd": [sys.executable, "-c", body], "frames": [1],
                     "level": "CLEAN", "log": None, "gpu_warn": 66},
                    writer=runner.ConsoleWriter(Capture()),
                    gpu_reader=scripted([40] + [75] * 50))
check("render finished", code, 0)
check("header says what is watched", "alarm above 66%" in Capture.text, True)
check("alarm printed during the render", "!! GPU MEMORY 75% FULL" in Capture.text, True)
check("alarm printed once, not every poll", Capture.text.count("!! GPU MEMORY"), 1)
check("peak in the summary", "GPU memory peak: 75%" in Capture.text, True)

print("\nLive reading on this machine")
live = G.read()
print("   ", live)
check("nvidia-smi readable here", bool(live), True)

print()
if FAILS:
    print("%d FAILED: %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("All checks passed.")
