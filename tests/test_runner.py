"""Checks for runner.py with fake jobs - plain Python, no Blender needed.

    python tests/test_runner.py

Each fake job logs its start/end time, so the test can prove the queue
never runs more than N at a time, keeps list order, reports failures and
deletes the cleanup files even when a job fails.
"""
import glob
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = (sorted(glob.glob(os.path.join(HERE, "..", "V*"))) or [os.path.join(HERE, "..", "cli_render_launcher")])[-1]
sys.path.insert(0, SRC)

import runner  # noqa: E402

FAILS = []


def check(label, got, want):
    ok = got == want
    print("  %s  %-50s %r" % ("PASS" if ok else "FAIL", label, got))
    if not ok:
        FAILS.append(label)
        print("        want %r" % (want,))


def fake(name, log, secs, code=0):
    # One log file per job: concurrent appends to a shared file are not
    # atomic on Windows and lose lines (a test artefact, not a runner bug).
    own = "%s.%s" % (log, name)
    body = ("import time,sys;f=open(%r,'w');f.write('S %s %%f\\n'%%time.time());f.flush();"
            "time.sleep(%s);f.write('E %s %%f\\n'%%time.time());f.close();sys.exit(%d)"
            % (own, name, secs, name, code))
    return {"name": name, "cmd": [sys.executable, "-c", body]}


def _lines(log):
    for path in sorted(glob.glob(log + ".*")):
        yield from open(path)


def max_concurrency(log):
    events = []
    for line in _lines(log):
        kind, name, t = line.split()
        events.append((float(t), 1 if kind == "S" else -1))
    cur = peak = 0
    for _t, d in sorted(events, key=lambda e: (e[0], e[1])):
        cur += d
        peak = max(peak, cur)
    return peak


def start_order(log):
    starts = [l.split() for l in _lines(log) if l.startswith("S")]
    return [name for _k, name, _t in sorted(starts, key=lambda s: float(s[2]))]


tmp = tempfile.mkdtemp(prefix="cli_runner_test_")
quiet = lambda _msg: None  # noqa: E731

for parallel, want_peak in ((1, 1), (2, 2), (5, 4)):
    log = os.path.join(tmp, "log_%d.txt" % parallel)
    snap = os.path.join(tmp, "snap_%d.blend" % parallel)
    open(snap, "w").close()
    spec = {"jobs": [fake(n, log, 0.8) for n in ("a", "b", "c", "d")],
            "parallel": parallel, "cleanup": [snap]}
    t0 = time.time()
    said = []
    res = runner.run(dict(spec, gpu_warn=0, gpu_stop=0), say=said.append, gpu_reader=None)
    took = time.time() - t0
    print("\nparallel=%d  (took %.1fs)" % (parallel, took))
    check("never more than %d at a time" % want_peak, max_concurrency(log), want_peak)
    check("all 4 jobs finished OK", [r[1] for r in res], [0, 0, 0, 0])
    # the queue launches in list order (each job's own start time can jitter
    # by a few ms, since it starts through its own watcher process)
    check("launched in list order", [m.split()[1] for m in said if m.startswith("started")],
          ["a", "b", "c", "d"])
    check("snapshot deleted afterwards", os.path.exists(snap), False)

print("\nfailures")
log = os.path.join(tmp, "log_fail.txt")
snap = os.path.join(tmp, "snap_fail.blend")
open(snap, "w").close()
res = runner.run({"jobs": [fake("ok", log, 0.2), fake("bad", log, 0.2, code=3),
                           {"name": "missing", "cmd": [os.path.join(tmp, "nope.exe")]}],
                  "parallel": 2, "cleanup": [snap], "gpu_warn": 0, "gpu_stop": 0}, say=quiet, gpu_reader=None)
codes = {r[0]: r[1] for r in res}
check("good job OK", codes.get("ok"), 0)
check("failing job reports its exit code", codes.get("bad"), 3)
check("unstartable job reported as failed, queue continues", codes.get("missing") not in (0, None), True)
check("snapshot deleted even after failures", os.path.exists(snap), False)

print("\nwhole script, as the add-on launches it")
log = os.path.join(tmp, "log_main.txt")
jobfile = os.path.join(tmp, "queue.json")
json.dump({"jobs": [fake("x", log, 0.2), fake("y", log, 0.2)], "parallel": 2,
           "pause_on_error": False, "gpu_warn": 0, "gpu_stop": 0}, open(jobfile, "w"))
out = subprocess.run([sys.executable, os.path.join(SRC, "runner.py"), jobfile],
                     capture_output=True, text=True, timeout=60)
check("exit code 0 when all jobs pass", out.returncode, 0)
check("job file removed after the run", os.path.exists(jobfile), False)
check("summary printed", "Done: 2 OK, 0 failed" in out.stdout, True)

if hasattr(runner, "watch"):                                   # 5.4+
    print("\nwatched render (the per-job console)")

    class Capture:
        def __init__(self):
            self.text = ""

        def isatty(self):
            return False

        def write(self, s):
            self.text += s

        def flush(self):
            pass

    log = os.path.join(tmp, "logs", "w.log")
    body = ("print('00:01.000  render | Rendering animation (frames 1..2)');"
            "print('00:01.000  render | Rendering frame 1');"
            "print('Segmentation fault (core dumped)');"
            "import sys; sys.exit(3)")
    cap = Capture()
    code = runner.watch({"name": "w", "cmd": [sys.executable, "-c", body],
                         "frames": [1, 2], "level": "CLEAN", "log": log},
                        writer=runner.ConsoleWriter(cap))
    check("watcher returns Blender's exit code", code, 3)
    check("failure announced with frames done",
          "STOPPED: Blender exited with code 3 after 0/2 frames" in cap.text, True)
    check("crash cause shown (render problem)", "Segmentation fault" in cap.text, True)
    check("full log written, folder created", os.path.isfile(log), True)
    check("log holds the raw lines",
          "Rendering frame 1" in open(log, encoding="utf-8").read(), True)

print()
if FAILS:
    print("%d FAILED: %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("All checks passed.")
