"""Checks for console_filter.py against REAL recorded Blender 5.2 output.

    python tests/test_console_filter.py

fixtures/cycles_problems_5.2.log  Cycles, 3 frames, a missing texture and a
                                  failing driver, plus the user's add-on noise
fixtures/workbench_clean_5.2.log  Workbench, 3 frames, nothing wrong
"""
import glob
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, (sorted(glob.glob(os.path.join(HERE, "..", "V*"))) or [os.path.join(HERE, "..", "cli_render_launcher")])[-1])

from console_filter import ConsoleFilter, fmt_duration  # noqa: E402

FAILS = []


def check(label, got, want):
    ok = got == want
    print("  %s  %-58s %r" % ("PASS" if ok else "FAIL", label, got if len(repr(got)) < 70 else "..."))
    if not ok:
        FAILS.append(label)
        print("        got  %r\n        want %r" % (got, want))


def run(fixture, frames, level, exit_code=0, frame_secs=120.0):
    """Replay a recording; the fake clock advances like a real render:
    `frame_secs` pass between a frame's start and its "Time:" line."""
    clock = [0.0]
    f = ConsoleFilter("closeup", frames, level=level, log_path="LOG", clock=lambda: clock[0])
    events = f.header()
    for line in open(os.path.join(HERE, "fixtures", fixture), encoding="utf-8", errors="replace"):
        if "Time: " in line and "(Saving" in line:
            clock[0] += frame_secs
        events += f.feed(line)
    events += f.finish(exit_code)
    printed = [t for k, t in events if k in ("print", "final")]
    return events, printed


print("CLEAN on the problem recording (477 raw lines)")
ev, out = run("cycles_problems_5.2.log", [1, 2, 3], "CLEAN")
text = "\n".join(out)
check("output is short", len(out) <= 22, True)
check("header names the job and range", out[0], "closeup · frames 1-3 (3 frames)")
check("missing texture shown exactly once",
      sum(1 for l in out if l.startswith("  ! WARNING Image file")), 1)
check("...and reported as repeated 3 times", "repeated 3 times: WARNING Image file" in text, True)
drv = [l for l in out if "Driver error" in l]
check("driver error shown once, condensed", len(drv), 1)
check("driver error names object, property and cause",
      all(s in drv[0] for s in ('undefined_function(frame)', 'Object "Cube" location[2]',
                                "NameError")), True)
check("no separate duplicate of the driver traceback",
      any("<bpy driver>" in l for l in out), False)
for noise in ("Megascans", "UMI", "blendkit", "Pillow", "bgl", "imp'", "unregister"):
    check("add-on noise hidden: %s" % noise, any(noise in l for l in out), False)
check("add-on noise counted, not lost", "add-on messages while Blender started" in text, True)
starts = [l for l in out if l.startswith("> frame")]
check("one progress line per frame", len(starts), 3)
check("first frame: no rate yet", starts[0], "> frame 1 (1/3) · last 3 · 3 to go · measuring speed...")
check("second frame: rate + ETA from frame 1",
      starts[1].startswith("> frame 2 (2/3) · last 3 · 2 to go · 2m00s/frame · ~4m00s left"), True)
check("third frame: 1 to go, 2m left", "1 to go · 2m00s/frame · ~2m00s left" in starts[2], True)
check("each frame reports its time", sum(1 for k, t in ev if k == "final" and "done in 2m00s" in t), 3)
check("live sample lines emitted", sum(1 for k, t in ev if k == "live" and t.strip().startswith("sample")) >= 3, True)
check("finish line", "Finished: 3 frames" in text and "(2m00s/frame)" in text, True)

print("\nDETAILED shows the add-on errors too")
_, out_d = run("cycles_problems_5.2.log", [1, 2, 3], "DETAILED")
check("add-on errors visible", any("No module named 'bgl'" in l for l in out_d), True)
check("still no routine status lines", any("Synchronizing object" in l for l in out_d), False)
check("still one progress line per frame", sum(1 for l in out_d if l.startswith("> frame")), 3)
check("no empty 'Python error: Python error'", any(l.strip() == "! Python error: Python error" for l in out_d), False)
check("no chained-traceback connector lines", any("direct cause" in l for l in out_d), False)

print("\nFULL is untouched")
ev_f, out_f = run("cycles_problems_5.2.log", [1, 2, 3], "FULL")
raw = open(os.path.join(HERE, "fixtures", "cycles_problems_5.2.log"), encoding="utf-8", errors="replace").read().splitlines()
check("every raw line passes through", out_f, raw)

print("\nA clean render stays quiet")
_, out_c = run("workbench_clean_5.2.log", [1, 2, 3], "CLEAN", frame_secs=5)
check("no problem lines at all", [l for l in out_c if l.startswith("  !")], [])
check("three progress lines", sum(1 for l in out_c if l.startswith("> frame")), 3)

print("\nA crash shows the end of the raw output")
_, out_x = run("cycles_problems_5.2.log", [1, 2, 3, 4, 5], "CLEAN", exit_code=11)
text_x = "\n".join(out_x)
check("stop is announced with the exit code", "STOPPED: Blender exited with code 11 after 3/5 frames" in text_x, True)
check("raw tail included", "last lines Blender printed:" in text_x and "Blender quit" in text_x, True)

print("\nFrame numbers come from the output when not given")
_, out_n = run("cycles_problems_5.2.log", [], "CLEAN")
check("total taken from 'Rendering animation (frames 1..3)'",
      [l for l in out_n if l.startswith("> frame")][0].startswith("> frame 1 (1/3)"), True)

print("\nDurations")
check("seconds", fmt_duration(42), "42s")
check("minutes", fmt_duration(133), "2m13s")
check("hours", fmt_duration(3600 * 14 + 120), "14h02m")

print()
if FAILS:
    print("%d FAILED: %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("All checks passed.")
