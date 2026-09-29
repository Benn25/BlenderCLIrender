"""GPU-aware render queue: decides what starts, what stops, what retries.

Rules (GPU memory = the whole card, read through gpu_monitor):

  Admission  A job starts only while the card is below the alarm level
             (default 66%), no other job is still loading its first frame,
             and the last start was SETTLE_S ago - memory jumps while a
             render loads, so a reading taken too early would lie.
             With nothing running, a job always starts (other programs
             using the card must not deadlock the queue).
  Stop       Above the stop level (default 90%) with 2+ jobs running, one
             job is stopped - a still-loading one first (no work lost),
             else the one whose current frame started last - and put back
             at the FRONT of the queue to resume at that frame. Never the
             last running job; STOP_COOLDOWN_S between stops.
  Out of     The job's console kills Blender on the out-of-memory message
  memory     (so no broken frame gets saved). The job goes to the END of
             the queue, marked "alone": it starts only when nothing else
             runs, and nothing starts beside it. One alone retry; a second
             out-of-memory is reported as a failure.

Launching is injected (`launch(job) -> handle`), as are the GPU reader and
the clock, so every rule is tested with simulated renders
(tests/test_scheduler.py). No bpy, no subprocess here.

A handle offers:  poll() -> None while running, else the exit code
                  status() -> {"phase": "loading"|"render", "frame",
                               "since", "resume"}
                  request_stop()
"""
import time

STOP_CODE = 75          # the job's console stopped Blender on request
OOM_CODE = 76           # the job's console killed Blender: out of GPU memory

TICK_S = 0.5
GPU_POLL_S = 2.0
SETTLE_S = 15.0
STOP_COOLDOWN_S = 20.0
OOM_ALONE_RETRIES = 1


def with_start(cmd, frame):
    """The same render command, starting at `frame` (replaces -s)."""
    cmd = list(cmd)
    i = cmd.index("-s")
    cmd[i + 1] = str(frame)
    return cmd


def remaining_from(frames, frame):
    """Frames still to render when resuming at `frame`."""
    if frame in frames:
        return frames[frames.index(frame):]
    return [f for f in frames if f >= frame]


class Job:
    def __init__(self, spec):
        self.name = spec["name"]
        self.cmd = spec["cmd"]
        self.frames = list(spec.get("frames") or [])
        self.log = spec.get("log")
        self.alone = False
        self.oom_retries = 0
        self.stops = 0
        self.resumed = False
        self.note = ""

    def as_launch(self):
        cmd = self.cmd
        if self.resumed and self.frames:
            cmd = with_start(cmd, self.frames[0])
        return {"name": self.name, "cmd": cmd, "frames": self.frames,
                "log": self.log, "log_append": self.resumed, "note": self.note}


class Scheduler:
    def __init__(self, spec, launch, gpu_reader=None, say=print,
                 clock=time.time, sleep=time.sleep):
        self.pending = [Job(j) for j in spec["jobs"]]
        self.parallel = max(1, int(spec.get("parallel", 1)))
        self.admit_pct = spec.get("gpu_warn", 66) or 66
        self.stop_pct = spec.get("gpu_stop", 90)
        self.launch = launch
        self.gpu_reader = gpu_reader
        self.say = say
        self.clock = clock
        self.sleep = sleep
        self.running = []           # [(job, handle, started_at)]
        self.results = []           # (name, code, seconds, note)
        self.last_start = -1e9
        self.last_stop = -1e9
        self.last_gpu_read = -1e9
        self.gpu_pct = None
        self.waiting_said = None

    # -- GPU ---------------------------------------------------------------

    def _read_gpu(self):
        now = self.clock()
        if self.gpu_reader is None or now - self.last_gpu_read < GPU_POLL_S:
            return self.gpu_pct
        self.last_gpu_read = now
        gpus = self.gpu_reader()
        if not gpus:
            self.gpu_pct = None
        else:
            self.gpu_pct = max(100.0 * u / t for _i, _n, u, t in gpus if t > 0)
        return self.gpu_pct

    # -- decisions ---------------------------------------------------------

    def _can_admit(self, pct):
        if not self.pending or len(self.running) >= self.parallel:
            return False
        if any(job.alone for job, _h, _t in self.running):
            return False                       # an "alone" retry runs by itself
        nxt = self.pending[0]
        if nxt.alone:
            return not self.running
        if not self.running:
            return True
        if pct is None:
            return True                        # no GPU info: plain parallel queue
        if self.clock() - self.last_start < SETTLE_S:
            return False
        if any(h.status().get("phase") != "render" for _j, h, _t in self.running):
            return False                       # someone is still loading
        if pct >= self.admit_pct:
            if self.waiting_said != nxt.name:
                self.say("waiting   %s: GPU memory at %d%%, starts below %d%%"
                         % (nxt.name, pct, self.admit_pct))
                self.waiting_said = nxt.name
            return False
        return True

    def _start_next(self):
        job = self.pending.pop(0)
        try:
            handle = self.launch(job.as_launch())
        except OSError as exc:
            self.results.append((job.name, None, 0.0, "could not start: %s" % exc))
            self.say("FAILED to start %s: %s" % (job.name, exc))
            return
        self.running.append((job, handle, self.clock()))
        self.last_start = self.clock()
        self.waiting_said = None
        extra = ""
        if job.resumed and job.frames:
            extra = " (resuming at frame %d%s)" % (job.frames[0], ", alone" if job.alone else "")
        self.say("started   %s%s" % (job.name, extra))

    def _maybe_stop_one(self, pct):
        if pct is None or not self.stop_pct or pct < self.stop_pct:
            return
        if len(self.running) < 2 or self.clock() - self.last_stop < STOP_COOLDOWN_S:
            return
        candidates = [(j, h, t) for j, h, t in self.running if not getattr(h, "stopping", False)]
        if len(candidates) < 2:
            return

        def work_at_risk(item):
            st = item[1].status()
            if st.get("phase") != "render" or st.get("since") is None:
                return -1e18                   # still loading: nothing to lose
            return -st["since"]                # newest frame start = least lost
        job, handle, _t = min(candidates, key=work_at_risk)
        handle.stopping = True
        handle.request_stop()
        self.last_stop = self.clock()
        st = handle.status()
        where = "at frame %s" % st.get("frame") if st.get("frame") is not None else "while loading"
        self.say("!! GPU memory %d%%: stopping %s %s to free memory - it will resume later"
                 % (pct, job.name, where))

    def _finished(self, job, handle, started, code):
        took = self.clock() - started
        st = handle.status()
        resume = st.get("resume")
        if resume is not None:
            left = remaining_from(job.frames, resume)
        else:                                  # nothing left to resume
            left = [] if st.get("done", 0) >= len(job.frames) else job.frames
        if code in (STOP_CODE, OOM_CODE) and not left:
            code = 0                           # it had finished every frame anyway
        if code == STOP_CODE and left:
            job.frames, job.resumed, job.stops = left, True, job.stops + 1
            job.note = "resuming at frame %d (stopped earlier to free GPU memory)" % left[0]
            self.pending.insert(0, job)
            self.say("paused    %s - resumes at frame %d" % (job.name, left[0]))
            return
        if code == OOM_CODE and left:
            if job.oom_retries < OOM_ALONE_RETRIES:
                job.oom_retries += 1
                job.frames, job.resumed, job.alone = left, True, True
                job.note = ("retrying alone at frame %d after running out of GPU memory"
                            % left[0])
                self.pending.append(job)
                self.say("!! %s ran out of GPU memory at frame %d - retrying alone "
                         "once the others are done" % (job.name, left[0]))
                return
            self.results.append((job.name, code, took,
                                 "out of GPU memory at frame %d, even alone" % left[0]))
            self.say("FAILED    %s: out of GPU memory at frame %d even alone - the scene "
                     "needs more GPU memory than the card has" % (job.name, left[0]))
            return
        self.results.append((job.name, code, took, None))
        self.say("finished  %s  (%s)" % (job.name, "OK" if code == 0 else "exit code %d" % code))

    # -- main loop ---------------------------------------------------------

    def run(self):
        self.say("CLI Render queue: %d job(s), up to %d at a time" % (len(self.pending), self.parallel))
        while self.pending or self.running:
            pct = self._read_gpu()
            while self._can_admit(pct):
                self._start_next()
            self.sleep(TICK_S)
            still = []
            for job, handle, started in self.running:
                code = handle.poll()
                if code is None:
                    still.append((job, handle, started))
                else:
                    self._finished(job, handle, started, code)
            self.running = still
            self._maybe_stop_one(self._read_gpu())
        return self.results
