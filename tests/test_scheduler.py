"""Checks for scheduler.py with simulated renders and a simulated GPU.

    python tests/test_scheduler.py

Each fake render loads for LOAD seconds, then renders frames of FRAME
seconds; it holds GPU memory while alive (more while loading). The card's
fill = base + the renders' memory, so starting / stopping renders really
changes what the scheduler reads. Time is simulated - the test is instant.
"""
import glob
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, (sorted(glob.glob(os.path.join(HERE, "..", "V*")), key=lambda p: [int(x) for x in os.path.basename(p)[1:].split(".") if x.isdigit()]) or [os.path.join(HERE, "..", "cli_render_launcher")])[-1])

import scheduler as S  # noqa: E402

FAILS = []


def check(label, got, want):
    ok = got == want
    print("  %s  %-60s %r" % ("PASS" if ok else "FAIL", label, got if len(repr(got)) < 60 else "..."))
    if not ok:
        FAILS.append(label)
        print("        got  %r\n        want %r" % (got, want))


class World:
    def __init__(self, base=20.0, total=16000.0):
        self.t = 0.0
        self.base = base
        self.total = total
        self.alive = []
        self.rendered = {}          # job name -> [frames, in render order]
        self.starts = []            # (time, name, first frame)
        self.log = []
        self.max_parallel = 0
        self.extra = 0.0            # memory used by "other programs" (%)

    def clock(self):
        return self.t

    def sleep(self, s):
        self.t += s

    def gpu(self):
        pct = self.base + self.extra + sum(r.mem() for r in self.alive if r.code is None)
        return [(0, "SimGPU", self.total * min(pct, 100) / 100.0, self.total)]


class FakeRender:
    """A render: LOAD s loading, then FRAME s per frame. Optional out-of-
    memory at a given frame (on the first, not-alone attempt)."""

    def __init__(self, world, job, load=10, frame=30, mem_load=30, mem_render=25,
                 oom_at=None, oom_even_alone=False, gap=0, mem_gap=None):
        self.w, self.job = world, job
        # Cycles frees memory between frames: the first GAP s of each frame
        # use only MEM_GAP (the dip that fooled admission in 5.12.1).
        self.gap, self.mem_gap = gap, mem_gap
        self.load, self.frame_s = load, frame
        self.mem_load, self.mem_render = mem_load, mem_render
        self.oom_at = oom_at if (oom_even_alone or "alone" not in job.get("note", "")) else None
        self.t0 = world.t
        self.code = None
        self.stop_req = False
        self.frames = list(job["frames"])
        world.alive.append(self)
        live = sum(1 for r in world.alive if r.code is None)
        world.starts.append((world.t, job["name"], self.frames[0], live))
        world.max_parallel = max(world.max_parallel, live)

    def _progress(self):
        el = self.w.t - self.t0
        if el < self.load:
            return None, 0
        idx = int((el - self.load) // self.frame_s)
        return idx, (el - self.load) - idx * self.frame_s

    def mem(self):
        idx, into = self._progress()
        if idx is None:
            return self.mem_load
        if self.mem_gap is not None and into < self.gap:
            return self.mem_gap
        return self.mem_render

    def status(self):
        idx, into = self._progress()
        if self.code is not None and hasattr(self, "_final"):
            return self._final
        if idx is None:
            return {"phase": "loading", "frame": None, "since": None, "resume": self.frames[0]}
        idx = min(idx, len(self.frames) - 1)
        return {"phase": "render", "frame": self.frames[idx], "since": self.w.t - into,
                "resume": self.frames[idx]}

    def poll(self):
        if self.code is not None:
            return self.code
        idx, _ = self._progress()
        done_n = 0 if idx is None else min(idx, len(self.frames))
        got = self.w.rendered.setdefault(self.job["name"], [])
        for f in self.frames[:done_n]:
            if f not in got[-len(self.frames):] or f not in got:
                if f not in got:
                    got.append(f)
        if self.oom_at is not None and idx is not None and idx < len(self.frames) \
                and self.frames[idx] == self.oom_at:
            self._final = {"phase": "render", "frame": self.oom_at, "since": None,
                           "resume": self.oom_at}
            self.code = S.OOM_CODE
            return self.code
        if self.stop_req:
            self._final = self.status()
            self.code = S.STOP_CODE
            return self.code
        if idx is not None and idx >= len(self.frames):
            self.code = 0
        return self.code

    def request_stop(self):
        self.stop_req = True


def run_queue(jobs, parallel, profiles=None, base=20.0, gpu=True, extra_at=None):
    w = World(base=base)
    profiles = profiles or {}
    spec = {"jobs": [{"name": n, "cmd": ["B", "-b", "x", "-s", str(fr[0]), "-e", str(fr[-1]), "-a"],
                      "frames": fr} for n, fr in jobs],
            "parallel": parallel, "gpu_warn": 66, "gpu_stop": 90}
    launched = []

    def launch(job):
        launched.append(job)
        return FakeRender(w, job, **profiles.get(job["name"], {}))

    reader = w.gpu if gpu else None
    if extra_at:
        orig = w.gpu

        def reader():
            for t, pct in extra_at:
                if w.t >= t:
                    w.extra = pct
            return orig()
    sch = S.Scheduler(spec, launch, gpu_reader=reader, say=w.log.append, clock=w.clock, sleep=w.sleep)
    res = sch.run()
    return w, res, launched


F = lambda a, b: list(range(a, b + 1))  # noqa: E731

print("Helpers")
check("with_start replaces -s", S.with_start(["B", "-s", "1", "-e", "9", "-a"], 5),
      ["B", "-s", "5", "-e", "9", "-a"])
check("remaining frames with a step", S.remaining_from([1, 3, 5, 7], 5), [5, 7])

print("\nAdmission: small renders, room for 3")
w, res, _ = run_queue([("a", F(1, 3)), ("b", F(1, 3)), ("c", F(1, 3))], parallel=3,
                      profiles={n: dict(mem_load=10, mem_render=8) for n in "abc"})
check("all finished OK", sorted((n, c) for n, c, _t, _e in res), [("a", 0), ("b", 0), ("c", 0)])
check("they did overlap", w.max_parallel, 3)
gaps = [w.starts[i + 1][0] - w.starts[i][0] for i in range(len(w.starts) - 1)]
check("starts spaced by the settle time (>=15 s)", all(g >= 15 for g in gaps), True)

print("\nAdmission: heavy renders wait below 66%")
w, res, _ = run_queue([("a", F(1, 4)), ("b", F(1, 4)), ("c", F(1, 4))], parallel=3,
                      profiles={n: dict(mem_load=30, mem_render=28) for n in "abc"})
check("all finished OK", [c for _n, c, _t, _e in res], [0, 0, 0])
check("never 3 at once (20+28+28 >= 66)", w.max_parallel <= 2, True)
check("the wait was announced", any(l.startswith("waiting") for l in w.log), True)
check("no render ever stopped", any("stopping" in l for l in w.log), False)

print("\nStop at 90%: another program fills the card mid-render")
w, res, launched = run_queue([("a", F(1, 6)), ("b", F(1, 6))], parallel=2,
                             profiles={n: dict(mem_load=20, mem_render=20) for n in "ab"},
                             extra_at=[(60, 35), (200, 0)])
check("both finished OK", sorted((n, c) for n, c, _t, _e in res), [("a", 0), ("b", 0)])
stops = [l for l in w.log if "stopping" in l]
check("exactly one render stopped", len(stops), 1)
check("the newer frame was chosen (b started later)", "stopping b" in stops[0], True)
resumed = [j for j in launched if j.get("log_append")]
check("stopped render resumed, log appended", len(resumed), 1)
first_resume = resumed[0]["frames"][0]
check("it resumed at the frame it was on, not from the start", first_resume > 1, True)
check("resume command starts there (-s)", resumed[0]["cmd"][resumed[0]["cmd"].index("-s") + 1],
      str(first_resume))
check("every frame of b rendered exactly once", sorted(w.rendered["b"]), F(1, 6))

print("\nAdmission ignores the dips between frames (5.12.1 thrashed on a real card)")
dips = dict(mem_load=20, mem_render=28, gap=4, mem_gap=2, frame=30)
w, res, _ = run_queue([("a", F(1, 12)), ("b", F(1, 12)), ("c", F(1, 12))], parallel=3,
                      profiles={n: dict(dips) for n in "abc"})
check("all finished OK", [c for _n, c, _t, _e in res], [0, 0, 0])
check("never 3 at once (20+28+28 >= 66, dips or not)", w.max_parallel <= 2, True)
check("no render ever stopped", any("stopping" in l for l in w.log), False)
check("the wait names the recent peak",
      any("in the last" in l for l in w.log if l.startswith("waiting")), True)

print("\nAfter a memory stop: one fewer at a time for the rest of the queue")
w, res, launched = run_queue([(n, F(1, 10)) for n in "abcd"], parallel=3,
                             profiles={n: dict(mem_load=14, mem_render=14) for n in "abcd"},
                             extra_at=[(100, 30), (130, 0)])
check("all finished OK", [c for _n, c, _t, _e in res], [0, 0, 0, 0])
stops = [l for l in w.log if "stopping" in l]
check("exactly one render stopped", len(stops), 1)
check("the new limit is announced",
      any(l.startswith("limit") and "at most 2" in l for l in w.log), True)
later = [live for t, _n, _f, live in w.starts if t > 100]
check("never 3 at once after the stop", bool(later) and max(later) <= 2, True)
check("every frame rendered exactly once",
      all(sorted(w.rendered[n]) == F(1, 10) for n in "abcd"), True)

print("\nNever stops the last running render")
w, res, _ = run_queue([("solo", F(1, 3))], parallel=1, base=85,
                      profiles={"solo": dict(mem_load=10, mem_render=10)})
check("finished OK at 95%", [c for _n, c, _t, _e in res], [0])
check("no stop attempted", any("stopping" in l for l in w.log), False)

print("\nOut of GPU memory: retried alone after the others")
w, res, launched = run_queue([("a", F(1, 5)), ("b", F(1, 5)), ("c", F(1, 3))], parallel=3,
                             profiles={"a": dict(mem_load=10, mem_render=8, oom_at=3),
                                       "b": dict(mem_load=10, mem_render=8),
                                       "c": dict(mem_load=10, mem_render=8)})
check("all three end OK", sorted((n, c) for n, c, _t, _e in res), [("a", 0), ("b", 0), ("c", 0)])
check("the out-of-memory was announced",
      any("a ran out of GPU memory at frame 3" in l for l in w.log), True)
retry = [s for s in w.starts if s[1] == "a"][-1]
check("retry started at frame 3", retry[2], 3)
check("retry ran truly alone (1 render alive when it started)", retry[3], 1)
check("nothing started after it while it ran",
      [s[1] for s in w.starts if s[0] > retry[0]], [])
check("frames 1-2 of a not re-rendered", w.rendered["a"].count(1) + w.rendered["a"].count(2), 2)
check("a complete", sorted(set(w.rendered["a"])), F(1, 5))

print("\nOut of memory even alone: reported, no endless loop")
w, res, _ = run_queue([("big", F(1, 4))], parallel=1,
                      profiles={"big": dict(oom_at=2, oom_even_alone=True)})
check("one retry, then failure", [(n, c) for n, c, _t, _e in res], [("big", S.OOM_CODE)])
check("explained", any("even alone" in l for l in w.log), True)

print("\nNo GPU information: plain parallel queue")
w, res, _ = run_queue([("a", F(1, 2)), ("b", F(1, 2)), ("c", F(1, 2))], parallel=2, gpu=False,
                      profiles={n: dict(mem_load=60, mem_render=60) for n in "abc"})
check("all OK", [c for _n, c, _t, _e in res], [0, 0, 0])
check("still capped at 2", w.max_parallel, 2)

print()
if FAILS:
    print("%d FAILED: %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("All checks passed.")
