"""The GPU-aware queue with REAL watcher processes and a fake Blender.

    python tests/test_queue_integration.py

The GPU readings are scripted over time (the real card cannot be made to
hit 95% on demand); everything else is real: watcher processes, status and
stop files, killing, resuming, log appending.
"""
import glob
import os
import shutil
import sys
import tempfile
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, (sorted(glob.glob(os.path.join(HERE, "..", "V*")), key=lambda p: [int(x) for x in os.path.basename(p)[1:].split(".") if x.isdigit()]) or [os.path.join(HERE, "..", "cli_render_launcher")])[-1])

import runner  # noqa: E402
import scheduler  # noqa: E402

# Fast timings for the test (the real ones are seconds apart).
scheduler.TICK_S = 0.1
scheduler.GPU_POLL_S = 0.2
scheduler.SETTLE_S = 1.0
scheduler.STOP_COOLDOWN_S = 3.0

FAILS = []
FAKE = os.path.join(HERE, "fake_blender.py")


def check(label, got, want):
    ok = got == want
    print("  %s  %-58s %r" % ("PASS" if ok else "FAIL", label, got if len(repr(got)) < 60 else "..."))
    if not ok:
        FAILS.append(label)
        print("        got  %r\n        want %r" % (got, want))


def job(name, out, first, last, *extra, step=1):
    cmd = [sys.executable, FAKE] + list(extra) + ["-b", "fake.blend", "-s", str(first),
                                                  "-e", str(last)]
    if step > 1:
        cmd += ["-j", str(step)]
    cmd += ["-o", os.path.join(out, name + "_"), "-a"]
    return {"name": name, "cmd": cmd, "frames": list(range(first, last + 1, step)),
            "log": os.path.join(out, "logs", name + ".log")}


def gpu_script(steps):
    """steps: [(seconds_from_start, percent)] -> a reader for the scheduler."""
    t0 = time.time()

    def read():
        pct = [p for t, p in steps if time.time() - t0 >= t][-1]
        return [(0, "ScriptedGPU", 160.0 * pct, 16000.0)]
    return read


def renders(out, name):
    """frame -> how many times it was rendered."""
    got = {}
    for path in glob.glob(os.path.join(out, name + "_*.png")):
        frame = int(os.path.basename(path)[len(name) + 1:-4])
        got[frame] = len(open(path).read())
    return got


tmp = tempfile.mkdtemp(prefix="cli_queue_it_")

print("Stop at 90% and resume at the same frame")
out = os.path.join(tmp, "stop")
said = []
t0 = time.time()
res = runner.run({"jobs": [job("a", out, 1, 8), job("b", out, 1, 8)], "parallel": 2,
                  "gpu_warn": 66, "gpu_stop": 90, "pause_on_error": False},
                 say=said.append, gpu_reader=gpu_script([(0, 40), (4, 95), (7, 30)]))
print("   (%.1fs)  %s" % (time.time() - t0, " | ".join(said)))
check("both jobs end OK", sorted((n, c) for n, c, _t, _x in res), [("a", 0), ("b", 0)])
stops = [m for m in said if "stopping" in m]
check("exactly one render stopped at 95%", len(stops), 1)
victim = stops[0].split()[5] if stops else "?"
check("the stopped render was resumed", any(m.startswith("started   %s (resuming at frame" % victim)
                                            for m in said), True)
for name in ("a", "b"):
    check("%s: every frame rendered exactly once" % name, renders(out, name),
          {f: 1 for f in range(1, 9)})
log = open(os.path.join(out, "logs", victim + ".log"), encoding="utf-8").read()
check("resumed run appended to the same log", "===== resumed" in log, True)

print("\nEvery 3rd frame (SubScene override), stopped and resumed")
out = os.path.join(tmp, "step")
said = []
t0 = time.time()
# a's frames are slow, so b - the job with the newest frame - is the one
# stopped, and its resume must keep the step.
res = runner.run({"jobs": [job("a", out, 1, 3, "--frame-s", "3"), job("b", out, 1, 40, step=3)],
                  "parallel": 2,
                  "gpu_warn": 66, "gpu_stop": 90, "pause_on_error": False},
                 say=said.append, gpu_reader=gpu_script([(0, 40), (4, 95), (7, 30)]))
print("   (%.1fs)  %s" % (time.time() - t0, " | ".join(said)))
check("both jobs end OK", sorted((n, c) for n, c, _t, _x in res), [("a", 0), ("b", 0)])
check("b was stopped and resumed",
      (sum("stopping b" in m for m in said), any("started   b (resuming at frame" in m for m in said)),
      (1, True))
check("b: only every 3rd frame, each exactly once", renders(out, "b"),
      {f: 1 for f in range(1, 41, 3)})
check("a: untouched by b's step", renders(out, "a"), {f: 1 for f in range(1, 4)})

print("\nOut of GPU memory: killed at once, retried alone")
out = os.path.join(tmp, "oom")
said = []
t0 = time.time()
res = runner.run({"jobs": [job("a", out, 1, 5, "--oom-at", "3"), job("b", out, 1, 6),
                           job("c", out, 1, 6)], "parallel": 3,
                  "gpu_warn": 66, "gpu_stop": 90, "pause_on_error": False},
                 say=said.append, gpu_reader=gpu_script([(0, 30)]))
print("   (%.1fs)  %s" % (time.time() - t0, " | ".join(said)))
check("all three end OK", sorted((n, c) for n, c, _t, _x in res), [("a", 0), ("b", 0), ("c", 0)])
check("announced", any("a ran out of GPU memory at frame 3" in m for m in said), True)
check("killed before saving a broken frame", glob.glob(os.path.join(out, "*.BROKEN")), [])
order = [m for m in said if m.startswith(("started", "finished"))]
retry_i = next(i for i, m in enumerate(order) if "resuming at frame 3, alone" in m)
check("retry started only after b and c finished",
      {"finished  b  (OK)", "finished  c  (OK)"} <= set(order[:retry_i]), True)
check("a: frames 1-2 not redone, 3-5 rendered once", renders(out, "a"),
      {1: 1, 2: 1, 3: 1, 4: 1, 5: 1})

print("\nSingle job: an out-of-memory is retried (the fail-twice case is in test_scheduler)")
out = os.path.join(tmp, "oom2")
said = []
big = job("big", out, 1, 4, "--oom-at", "2")
big["cmd"][big["cmd"].index(FAKE) + 1:big["cmd"].index(FAKE) + 1]  # (keep args)
# make the fake fail again on the retry: start frame 2 == oom frame would
# normally skip it, so aim the second failure one frame later
res = runner.run({"jobs": [big], "parallel": 1, "gpu_warn": 66, "gpu_stop": 90,
                  "pause_on_error": False},
                 say=said.append, gpu_reader=gpu_script([(0, 30)]))
codes = [(n, c) for n, c, _t, _x in res]
check("first failure retried, retry succeeded (fake only fails once)",
      codes, [("big", 0)])

shutil.rmtree(tmp, ignore_errors=True)
print()
if FAILS:
    print("%d FAILED: %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("All checks passed.")
