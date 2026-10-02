"""GPU-aware render queue: decides what starts, what stops, what retries.

Rules (GPU memory = the whole card, read through gpu_monitor):

  Admission  A job starts only while the card is below the alarm level
             (default 66%), no other job is still loading its first frame,
             and the last start was SETTLE_S ago - memory jumps while a
             render loads, so a reading taken too early would lie.
             With nothing running, a job always starts (other programs
             using the card must not deadlock the queue).
             The level compared is the PEAK of the last PEAK_WINDOW_S: Cycles
             frees memory between frames, and a dip read at that moment let
             a job start into a card that was full a second later (5.12.2).
  Stop       Above the stop level (default 90%) with 2+ jobs running, one
             job is stopped - a still-loading one first (no work lost),
             else the one whose current frame started last - and put back
             at the FRONT of the queue to resume at that frame. Never the
             last running job; STOP_COOLDOWN_S between stops.
             After a stop with N jobs running, the queue runs at most N-1 at
             a time from then on: N did not fit, so trying N again would
             only stop a job again and reload its scene (5.12.2).
  Out of     The job's console kills Blender on the out-of-memory message
  memory     (so no broken frame gets saved). The job goes to the END of
             the queue, marked "alone": it starts only when nothing else
             runs, and nothing starts beside it. One alone retry; a second
             out-of-memory is reported as a failure.

Joining     A second launch from the same .blend joins the running queue
             (see runner.Inbox) as its own BATCH, with its own parallel limit:
             each launch gets the slots it asked for, so a joined set is not
             stuck behind the first set's limit. Whether it starts NOW is the
             admission rule above - the GPU decides - and from then on one
             stop rule watches every job of that file.

Launching is injected (`launch(job) -> handle`), as are the GPU reader and
the clock, so every rule is tested with simulated renders
(tests/test_scheduler.py). No bpy, no subprocess here.

A handle offers:  poll() -> None while running, else the exit code
                  status() -> {"phase": "loading"|"render", "frame",
                               "since", "resume"}
                  request_stop()
"""
import collections
import time

STOP_CODE = 75          # the job's console stopped Blender on request
OOM_CODE = 76           # the job's console killed Blender: out of GPU memory
OVERRIDE_CODE = 77      # a SubScene override failed (= jobs.OVERRIDE_FAILED_CODE)

TICK_S = 0.5
GPU_POLL_S = 2.0
SETTLE_S = 15.0
STOP_COOLDOWN_S = 20.0
PEAK_WINDOW_S = 60.0    # admission looks at the highest reading of this long
OOM_ALONE_RETRIES = 1


def with_start(cmd, frame):
    """The same render command, starting at `frame` (replaces -s).

    The -s that is followed by `<n> -e`: a scene called "-s" (passed as -S -s)
    must not be mistaken for it.
    """
    cmd = list(cmd)
    i = next((k for k in range(len(cmd) - 2)
              if cmd[k] == "-s" and cmd[k + 2] == "-e"), None)
    if i is None:
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
        # Position in the original queue. The viewer addresses jobs by this,
        # so a log or an output file is always looked up against the job that
        # actually produced it, however the running/pending lists shuffle.
        self.uid = spec.get("uid", 0)
        self.result = None              # (code, seconds, note) once it has ended
        # The watcher's last status, kept when the job ends. Per-frame timings
        # are measured in the watcher process and die with it, and they are
        # exactly what you want to read AFTER a render, not only during it.
        self.final_status = {}
        self.name = spec["name"]
        self.cmd = spec["cmd"]
        self.frames = list(spec.get("frames") or [])
        self.log = spec.get("log")
        # Where the compositor's File Output nodes write, captured by the
        # add-on from the live scene. The queue never opens the .blend, so it
        # could not find these itself.
        self.extra_outputs = list(spec.get("extra_outputs") or [])
        # Which launch this job came from; each launch keeps its own
        # parallel limit (see Scheduler.add_batch).
        self.batch = spec.get("batch", 0)
        # What scene this job renders (engine, resolution, view layers ...),
        # captured in Blender: joined launches can come from another scene.
        self.scene = spec.get("scene")
        # This SubScene's overrides in words ("16 samples · every 4th frame"),
        # already applied in its command line; shown, never re-applied.
        self.overrides = spec.get("overrides") or ""
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
                "log": self.log, "log_append": self.resumed, "note": self.note,
                "overrides": self.overrides}


class Scheduler:
    def __init__(self, spec, launch, gpu_reader=None, say=print,
                 clock=time.time, sleep=time.sleep, intake=None):
        self.pending = [Job(j) for j in spec["jobs"]]
        for job in self.pending:
            job.batch = 0
        # {batch: parallel limit}. Batch 0 is the launch that started the queue.
        self.batch_parallel = {0: max(1, int(spec.get("parallel", 1)))}
        self.parallel = self.batch_parallel[0]      # total, for display
        # intake.take() -> [(job_specs, parallel)] handed over by later launches;
        # intake.close() stops accepting and returns anything that slipped in.
        self.intake = intake
        self.admit_pct = spec.get("gpu_warn", 66) or 66
        self.stop_pct = spec.get("gpu_stop", 90)
        self.launch = launch
        self.gpu_reader = gpu_reader
        self.say = say
        self.clock = clock
        self.sleep = sleep
        self.running = []           # [(job, handle, started_at)]
        self.results = []           # (name, code, seconds, note)
        # The Job objects themselves, kept after they end: a finished render is
        # exactly when its output files are most worth listing, and `results`
        # holds only names.
        self.finished = []
        self.last_start = -1e9
        self.last_stop = -1e9
        self.last_gpu_read = -1e9
        self.gpu_pct = None
        self.gpu_recent = collections.deque()   # (time, pct) inside PEAK_WINDOW_S
        # Set by a memory stop: at most this many jobs at once, for the rest
        # of the queue (None = only the launches' own limits).
        self.mem_cap = None
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
            self.gpu_recent.append((now, self.gpu_pct))
        while self.gpu_recent and now - self.gpu_recent[0][0] > PEAK_WINDOW_S:
            self.gpu_recent.popleft()
        return self.gpu_pct

    def _peak_pct(self, pct):
        """The highest reading of the last PEAK_WINDOW_S (at least `pct`)."""
        if pct is None:
            return None
        return max([pct] + [p for _t, p in self.gpu_recent])

    # -- batches -----------------------------------------------------------

    def add_batch(self, job_specs, parallel):
        """Append the jobs of a later launch. Returns the new Job objects."""
        batch = max(self.batch_parallel) + 1
        self.batch_parallel[batch] = max(1, int(parallel or 1))
        self.parallel = sum(self.batch_parallel.values())
        added = []
        for spec in job_specs:
            job = Job(spec)
            job.batch = batch
            added.append(job)
        self.pending.extend(added)
        self.say("joined    %d job(s) from another launch of this file, "
                 "up to %d at a time" % (len(added), self.batch_parallel[batch]))
        return added

    def _take_in(self, closing=False):
        if self.intake is None:
            return
        try:
            batches = self.intake.close() if closing else self.intake.take()
        except Exception as exc:              # a bad hand-over must not stop renders
            self.say("could not read a joined launch: %s" % exc)
            return
        for job_specs, parallel in batches or []:
            if job_specs:
                self.add_batch(job_specs, parallel)

    def _next_candidate(self):
        """The first pending job whose launch still has a free slot."""
        busy = {}
        for job, _h, _t in self.running:
            busy[job.batch] = busy.get(job.batch, 0) + 1
        for job in self.pending:
            if busy.get(job.batch, 0) < self.batch_parallel.get(job.batch, 1):
                return job
        return None

    # -- decisions ---------------------------------------------------------

    def _can_admit(self, pct):
        if not self.pending:
            return False
        if any(job.alone for job, _h, _t in self.running):
            return False                       # an "alone" retry runs by itself
        nxt = self._next_candidate()
        if nxt is None:
            return False                       # every launch is at its limit
        if nxt.alone:
            return not self.running
        if not self.running:
            return True
        if self.mem_cap is not None and len(self.running) >= self.mem_cap:
            return False                       # N did not fit: N-1 from now on
        if pct is None:
            return True                        # no GPU info: plain parallel queue
        if self.clock() - self.last_start < SETTLE_S:
            return False
        if any(h.status().get("phase") != "render" for _j, h, _t in self.running):
            return False                       # someone is still loading
        peak = self._peak_pct(pct)
        if peak >= self.admit_pct:
            if self.waiting_said != nxt.name:
                self.say("waiting   %s: GPU memory up to %d%% in the last %ds, starts below %d%%"
                         % (nxt.name, peak, PEAK_WINDOW_S, self.admit_pct))
                self.waiting_said = nxt.name
            return False
        return True

    def _start_next(self):
        job = self._next_candidate()
        self.pending.remove(job)
        try:
            handle = self.launch(job.as_launch())
        except OSError as exc:
            job.result = (None, 0.0, "could not start: %s" % exc)
            self.finished.append(job)
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
        cap = max(1, len(candidates) - 1)
        if self.mem_cap is None or cap < self.mem_cap:
            self.mem_cap = cap
            self.say("limit     %d job(s) did not fit in GPU memory: at most %d at a time "
                     "for the rest of this queue" % (len(candidates), cap))

    def _finished(self, job, handle, started, code):
        took = self.clock() - started
        st = handle.status()
        job.final_status = st or {}
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
            job.result = (code, took,
                          "out of GPU memory at frame %d, even alone" % left[0])
            self.finished.append(job)
            self.results.append((job.name, code, took,
                                 "out of GPU memory at frame %d, even alone" % left[0]))
            self.say("FAILED    %s: out of GPU memory at frame %d even alone - the scene "
                     "needs more GPU memory than the card has" % (job.name, left[0]))
            return
        note = ("its overrides could not be applied - see its console"
                if code == OVERRIDE_CODE else None)
        job.result = (code, took, note)
        self.finished.append(job)
        self.results.append((job.name, code, took, note))
        self.say("finished  %s  (%s)" % (job.name, "OK" if code == 0 else note or "exit code %d" % code))

    # -- main loop ---------------------------------------------------------

    def run(self):
        self.say("CLI Render queue: %d job(s), up to %d at a time" % (len(self.pending), self.parallel))
        while True:
            self._take_in()
            if not (self.pending or self.running):
                # Stop accepting FIRST, then take whatever arrived in between:
                # a launch that dropped its jobs just before the close is run,
                # never lost (runner.Inbox claims each file by renaming it).
                self._take_in(closing=True)
                if not self.pending:
                    break
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
