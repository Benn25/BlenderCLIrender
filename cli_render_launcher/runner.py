"""Render queue runner - runs OUTSIDE Blender.

The add-on starts this script as its own process (with Blender's bundled
Python), so the queue keeps going when Blender is closed.

    python runner.py <queue.json>              the queue
    python runner.py --watch <job.json>        one watched render
    blender -b --factory-startup --python runner.py -- <queue.json>

queue.json:
    {"jobs": [{"name": "intro", "cmd": [...], "frames": [1, 2, ...],
               "log": "...intro.log" or null}, ...],
     "parallel": 2,                  # renders running at the same time (max)
     "level": "CLEAN",               # CLEAN / DETAILED / FULL console
     "gpu_warn": 66,                 # alarm + admission level (%), 0 = off
     "gpu_stop": 90,                 # stop one render above this (%), 0 = off
     "console_per_job": true,        # Windows: one console window per render
     "web_ui": false,                # also serve a queue window on localhost
     "web_ui_open": true,            # ...and open the browser at it
     "cleanup": ["...snapshot.blend"],  # deleted once every job has ended
     "blend": "D:/.../shot.blend"}      # the ORIGINAL .blend: later launches
                                        # of it join this queue (see Inbox)

Every render runs under a watcher (its own console on Windows, the shared
terminal elsewhere) which filters the output (console_filter), keeps the raw
log, raises GPU alarms, reports its current frame in a status file, stops
on request, and kills Blender the moment it runs out of GPU memory. What
starts, stops and retries is decided by scheduler.Scheduler.

No bpy imports - tested with plain Python (tests/test_runner.py).
"""
import collections
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from console_filter import ConsoleFilter  # noqa: E402
import banner  # noqa: E402
import gpu_monitor  # noqa: E402
import jobs  # noqa: E402
import scheduler  # noqa: E402
import webui  # noqa: E402

GPU_POLL_S = 5.0
# The queue window outlives the queue by this long, so a page polling when the
# last frame lands gets one more answer and settles on the finished state
# instead of a connection error. Deliberately short: the timer is NOT a daemon
# (a daemon one would die with the process and linger for nothing), so this is
# also how long the queue console stays open after its last job. One poll
# interval plus margin is all that is needed - after it the page says the queue
# has finished rather than showing an error.
WEB_UI_LINGER_S = 5.0
STOP_POLL_S = 0.3
_print_lock = threading.Lock()

# Cycles / Blender wording for running out of GPU memory (CUDA, OptiX, HIP,
# Metal, oneAPI all end up in one of these).
_OOM = re.compile(r"out of (gpu |device |video )?memory|OUT_OF_MEMORY|"
                  r"System is out of GPU", re.I)


def _say(text):
    # A hidden queue (web_ui on) has its stdout redirected to a log file by the
    # add-on. If that failed it is DEVNULL, and on Windows a console-less
    # process can still end up with a broken handle -- so printing must never
    # be able to take the queue down with it.
    with _print_lock:
        try:
            sys.stdout.write(text + "\n")
            sys.stdout.flush()
        except (OSError, ValueError, AttributeError):
            pass


def fmt_duration(seconds):
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return "%dh%02dm%02ds" % (h, m, s) if h else "%dm%02ds" % (m, s)


def _child_env():
    # Python inside the render (add-ons, drivers) must not hold its output
    # back in a buffer, or problems would show up long after they happen.
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _open_log(path, append=False):
    if not path:
        return None
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        log = open(path, "a" if append else "w", encoding="utf-8", errors="replace")
        if append:
            log.write("\n===== resumed %s =====\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
        return log
    except OSError:
        return None


def _write_json(path, data):
    if not path:
        return
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except OSError:
        pass


def _read_json(path):
    try:
        # utf-8-sig, not utf-8: a job file saved by a Windows editor arrives
        # with a BOM. Plain utf-8 raised, this function swallowed it and
        # returned {}, and the queue then died on a KeyError with no console
        # to report it in -- a silent no-start.
        with open(path, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


# --------------------------------------------------------------------------
# One watched render
# --------------------------------------------------------------------------

class ConsoleWriter:
    """Shows filter events: one "live" line updated in place on a real
    console. In a shared terminal (prefix set) or a pipe, live updates are
    skipped and every other event is a plain, prefixed line."""

    def __init__(self, stream=None, prefix=""):
        self.stream = stream or sys.stdout
        self.prefix = prefix
        try:
            self.tty = self.stream.isatty() and not prefix
        except Exception:
            self.tty = False
        self.live = ""
        self.lock = threading.Lock()     # the GPU watch writes from a thread

    def _w(self, text):
        self.stream.write(text)
        self.stream.flush()

    def emit(self, kind, text):
        with self.lock if not self.prefix else _print_lock:
            self._emit(kind, text)

    def _emit(self, kind, text):
        if kind == "live":
            if self.tty:
                pad = " " * max(0, len(self.live) - len(text))
                self._w("\r" + text + pad)
                self.live = text
            return
        was_live = self.live
        if was_live:
            self._w("\r" + " " * len(was_live) + "\r")
            self.live = ""
        self._w(self.prefix + text + "\n")
        if kind == "print" and was_live:        # redraw the progress under it
            self._w(was_live)
            self.live = was_live


def _gpu_thread(gpu, emit, stop):
    """Poll GPU memory until `stop`; alarms go straight to the console.
    Shown at every console level: running out of GPU memory is as serious
    as an error."""
    while not stop.wait(GPU_POLL_S):
        for text in gpu.check():
            emit(text)


def _resume_frame(flt):
    """Where this job should restart if stopped now: the frame in progress,
    else the next one not yet rendered."""
    if flt.current is not None:
        return flt.current
    if flt.done < len(flt.frames):
        return flt.frames[flt.done]
    return None


def watch(job, writer=None, gpu_reader=gpu_monitor.read):
    """Run one render with its output filtered.

    Returns Blender's exit code, or scheduler.STOP_CODE (stopped on request)
    or scheduler.OOM_CODE (killed after running out of GPU memory).
    """
    writer = writer or ConsoleWriter(prefix="[%s] " % job["name"] if job.get("prefix") else "")
    status_path, stop_path = job.get("status"), job.get("stop")
    log = _open_log(job.get("log"), append=job.get("log_append", False))
    # Worked out once: the gauge needs it per frame, and enable_colour() pokes
    # the Windows console mode, which is not something to do on a hot path.
    colour = banner.enable_colour(writer.stream) if writer.tty else False
    gpu = gpu_monitor.GpuWatch(threshold=job.get("gpu_warn", 66), reader=gpu_reader)
    flt = ConsoleFilter(job["name"], job.get("frames") or [],
                        level=job.get("level", "CLEAN"),
                        log_path=job.get("log") if log else None,
                        # latest(), not read(): the gauge draws from the value
                        # the GPU thread already sampled, so a frame boundary
                        # never waits on an nvidia-smi subprocess.
                        gauge=gpu.latest, colour=colour,
                        gpu_warn=job.get("gpu_warn", 66),
                        gpu_stop=job.get("gpu_stop", 90))

    def status(**extra):
        # The filter has measured every frame; before 5.8.0 only the count
        # escaped this process. The queue window wants the timings and the
        # problem tally too, and they are free - just read them out.
        d = flt.durations
        data = {"phase": "render" if flt.current is not None else "loading",
                "frame": flt.current, "since": frame_since[0],
                "resume": _resume_frame(flt), "done": flt.done,
                "avg_s": (sum(d) / len(d)) if d else None,
                "fast_s": min(d) if d else None,
                "slow_s": max(d) if d else None,
                "last_s": d[-1] if d else None,
                "problems": sum(flt.seen.values()),
                "problem_kinds": len(flt.seen),
                "noise": flt.noise}
        data.update(extra)
        _write_json(status_path, data)

    frame_since = [None]
    status()
    # The Blender logo, only in a render's own console window (a shared
    # terminal would get one logo per job, interleaved).
    # Still gated on tty, not on `colour`: a console without VT sequences gets
    # the plain half-block logo rather than no logo at all.
    if writer.tty and job.get("banner", True):
        title = "CLI RENDER  v%s  ·  %s" % (banner.addon_version(), job["name"])
        for text in banner.lines(title, colour=colour):
            writer.emit("print", text)
    for kind, text in flt.header():
        writer.emit(kind, text)
    if job.get("note"):
        writer.emit("print", "  (%s)" % job["note"])
    line = gpu.describe()
    if line:
        writer.emit("print", "  " + line)
    for text in gpu.check():                  # already full before starting?
        writer.emit("print", text)

    try:
        proc = subprocess.Popen(job["cmd"], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace", bufsize=1,
                                env=_child_env())
    except OSError as exc:
        writer.emit("print", "Could not start the render: %s" % exc)
        if log:
            log.close()
        return 1

    stop = threading.Event()
    stopped = []
    threading.Thread(target=_gpu_thread, daemon=True,
                     args=(gpu, lambda t: writer.emit("print", t), stop)).start()

    def stop_watch():
        while not stop.wait(STOP_POLL_S):
            if stop_path and os.path.exists(stop_path):
                stopped.append(_resume_frame(flt))
                status(resume=stopped[0])
                proc.kill()
                return
    threading.Thread(target=stop_watch, daemon=True).start()

    oom = None
    for raw in proc.stdout:
        if log:
            log.write(raw)
            log.flush()
        if oom is not None:
            continue                            # killed: keep the recorded frame
        if _OOM.search(raw):
            # Kill at once: left alone, Blender carries on and may save a
            # broken frame. The queue retries from this frame, alone.
            oom = _resume_frame(flt)
            status(resume=oom, oom=True)
            proc.kill()
            writer.emit("print", "!! OUT OF GPU MEMORY%s: %s" % (
                " on frame %s" % oom if oom is not None else "", raw.strip()))
            continue
        before = flt.current
        for kind, text in flt.feed(raw):
            writer.emit(kind, text)
        if flt.current != before:
            frame_since[0] = time.time() if flt.current is not None else None
            status()
    code = proc.wait()
    stop.set()
    if log:
        log.close()

    if stopped:
        where = stopped[0]
        writer.emit("print", "Stopped by the queue%s to free GPU memory - it resumes "
                             "from there later." % (" at frame %s" % where if where is not None else ""))
        return scheduler.STOP_CODE
    if oom is not None:
        writer.emit("print", "The queue will retry from frame %s, alone, once the other "
                             "renders are done." % oom)
        for text in gpu.summary() or []:
            writer.emit("print", "  " + text)
        return scheduler.OOM_CODE
    status(finished=True)
    for kind, text in flt.finish(code):
        writer.emit(kind, text)
    for text in gpu.summary() or []:
        writer.emit("print", "  " + text)
    return code


# --------------------------------------------------------------------------
# The queue
# --------------------------------------------------------------------------

class WatcherHandle:
    """The scheduler's view of one running watcher process."""

    def __init__(self, proc, status_path, stop_path):
        self.proc = proc
        self.status_path = status_path
        self.stop_path = stop_path
        self.stopping = False

    def poll(self):
        return self.proc.poll()

    def status(self):
        return _read_json(self.status_path)

    def request_stop(self):
        try:
            open(self.stop_path, "w").close()
        except OSError:
            pass


def _watcher_command(jobfile):
    py = sys.executable
    if py and os.path.isfile(py) and "python" in os.path.basename(py).lower():
        return [py, os.path.abspath(__file__), "--watch", jobfile]
    return [py, "-b", "--factory-startup", "--python", os.path.abspath(__file__),
            "--", "--watch", jobfile]


def make_launcher(spec, workdir):
    """launch(job) -> WatcherHandle: a watcher per render - its own console
    on Windows, the shared terminal elsewhere."""
    own_console = bool(spec.get("console_per_job")) and sys.platform == "win32"
    counter = [0]

    def launch(job):
        counter[0] += 1
        base = os.path.join(workdir, "job%03d" % counter[0])
        full = dict(job, level=spec.get("level", "CLEAN"),
                    gpu_warn=spec.get("gpu_warn", 66),
                    gpu_stop=spec.get("gpu_stop", 90),
                    status=base + ".status.json", stop=base + ".stop",
                    prefix=not own_console,
                    pause_on_error=spec.get("pause_on_error", True))
        jobfile = base + ".job.json"
        _write_json(jobfile, full)
        flags = subprocess.CREATE_NEW_CONSOLE if own_console else 0
        proc = subprocess.Popen(_watcher_command(jobfile), creationflags=flags)
        return WatcherHandle(proc, full["status"], full["stop"])
    return launch


class Inbox:
    """Where later launches of the same .blend hand their jobs to this queue.

    Advertised in a registry file named after the .blend (jobs.registry_path),
    kept fresh by a heartbeat so a queue that died is ignored. The protocol and
    why it cannot lose or double a render are described in jobs.py. A file
    drop, never an HTTP route: the queue window stays read-only.

    `prepare(batch)` turns a handed-over batch into job specs for the
    scheduler (uids, snapshots, cleanup) - see run().
    """

    def __init__(self, blend, workdir, url=None, prepare=None, clock=time.time):
        self.dir = os.path.join(workdir, "inbox")
        os.makedirs(self.dir, exist_ok=True)
        self.path = jobs.registry_path(blend)
        self.prepare = prepare or (lambda batch: batch.get("jobs") or [])
        self.clock = clock
        self.closed = False
        self.beat = 0.0
        os.makedirs(jobs.REGISTRY_DIR, exist_ok=True)
        tmp = "%s.%d.tmp" % (self.path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"pid": os.getpid(), "inbox": self.dir, "url": url,
                       "blend": blend, "started": clock()}, fh)
        os.replace(tmp, self.path)
        self.beat = clock()

    def _heartbeat(self):
        now = self.clock()
        if now - self.beat >= jobs.HEARTBEAT_S:
            self.beat = now
            try:
                os.utime(self.path)
            except OSError:
                pass

    def _claim(self):
        batches = []
        try:
            names = sorted(n for n in os.listdir(self.dir) if n.endswith(".json"))
        except OSError:
            return batches
        for name in names:
            offer = os.path.join(self.dir, name)
            taken = offer[:-5] + ".taken"
            try:
                os.replace(offer, taken)    # exactly one side wins this rename
            except OSError:
                continue                    # withdrawn by Blender in the meantime
            try:
                with open(taken, encoding="utf-8-sig") as fh:
                    batch = json.load(fh)
                specs = self.prepare(batch)
                batches.append((specs, batch.get("parallel", 1)))
                # the ack is what tells Blender its jobs are in this queue
                open(offer[:-5] + ".ok", "w").close()
            except (OSError, ValueError) as exc:
                _say("could not read a joined launch (%s) - it was not added" % exc)
            finally:
                try:
                    os.remove(taken)
                except OSError:
                    pass
        return batches

    def take(self):
        if self.closed:
            return []
        self._heartbeat()
        return self._claim()

    def _unregister(self):
        # Only our own entry: a newer queue for the same file may have
        # replaced it, and must keep advertising.
        try:
            with open(self.path, encoding="utf-8-sig") as fh:
                ours = json.load(fh).get("inbox") == self.dir
            if ours:
                os.remove(self.path)
        except (OSError, ValueError):
            pass

    def close(self):
        """Stop advertising, THEN claim what arrived in between."""
        if self.closed:
            return []
        self.closed = True
        self._unregister()
        return self._claim()

    def discard(self):
        """Stop advertising without taking anything (the queue is ending)."""
        self.closed = True
        self._unregister()


def run(spec, say=_say, gpu_reader=gpu_monitor.read):
    """Run the whole queue; returns [(name, exit_code, seconds, note)].
    Files in spec["cleanup"] are deleted once all jobs have ended."""
    workdir = tempfile.mkdtemp(prefix="cli_render_queue_")
    use_gpu = bool(spec.get("gpu_warn", 66) or spec.get("gpu_stop", 90))

    # Each job keeps the position it had in the queue. The viewer addresses
    # logs and output files by it, so a request always resolves against the
    # job that produced them however the running/pending lists shuffle.
    for position, job in enumerate(spec.get("jobs") or []):
        job.setdefault("uid", position)

    # The scheduler only keeps a percentage; the window wants GB, the card's
    # name and a little history, so the reader is wrapped to remember what it
    # sees. The wrapper adds no polling of its own - it only observes.
    last_reading = [None]
    history = collections.deque(maxlen=webui.GPU_HISTORY)

    def remembering_reader():
        value = gpu_reader()
        last_reading[0] = value or None
        if value:
            worst = max(value, key=lambda g: (g[3] and g[2] / g[3]) or 0)
            if worst[3]:
                history.append(round(100.0 * worst[2] / worst[3], 1))
        return value

    sch = scheduler.Scheduler(spec, make_launcher(spec, workdir),
                              gpu_reader=remembering_reader if use_gpu else None,
                              say=say)

    stop_ui = None
    url = None
    started_at = time.time()
    # What every output folder held BEFORE the first job starts. Taken here,
    # ahead of sch.run(), so frames left over from an earlier render can be told
    # apart from frames this queue makes. Snapshot, not a clock comparison: the
    # output is often on a network share whose clock need not match this one.
    baselines = webui.snapshot(spec.get("jobs")) if spec.get("web_ui") else {}
    if spec.get("web_ui"):
        url, stop_ui = webui.serve(
            lambda: webui.state(sch, spec, gpu_cache=lambda: last_reading[0],
                                gpu_history=history, started_at=started_at,
                                gpu_interval=scheduler.GPU_POLL_S,
                                baselines=baselines),
            webui.page("CLI Render queue"),
            port=int(spec.get("web_ui_port") or 0),
            # The raw job dicts, in queue order: they carry the command (hence
            # the output path) and the log path, so a /file or /api/log request
            # resolves without needing a live Job object.
            jobs_fn=lambda: spec.get("jobs") or [],
            baselines=baselines)
        if url:
            say("Queue window: %s" % url)
            if spec.get("web_ui_open", True):
                try:
                    webbrowser.open(url)
                except Exception:
                    pass                      # no browser is not a render problem
        else:
            say("Queue window: could not open a local port - console only")

    def prepare(batch):
        """A joined launch's jobs, made ready for this queue."""
        specs = list(batch.get("jobs") or [])
        first = len(spec["jobs"])
        for offset, job in enumerate(specs):
            job["uid"] = first + offset          # = its index in spec["jobs"]
        if spec.get("web_ui"):
            # Same rule as at start: what its folders held BEFORE it writes.
            # A folder this queue already watches keeps its original snapshot.
            for prefix, files in webui.snapshot(specs).items():
                baselines.setdefault(prefix, files)
        # Appended before the scheduler sees them, so the window can resolve
        # a job's files the moment the job appears in the state.
        spec["jobs"].extend(specs)
        spec.setdefault("cleanup", []).extend(batch.get("cleanup") or [])
        return specs

    inbox = None
    if spec.get("blend"):
        try:
            inbox = Inbox(spec["blend"], workdir, url=url if spec.get("web_ui") else None,
                          prepare=prepare)
        except OSError as exc:
            say("Another launch of this file will start its own queue (%s)" % exc)
    sch.intake = inbox
    try:
        return sch.run()
    finally:
        if inbox is not None:
            inbox.discard()
        if stop_ui:
            # Held open briefly so a page mid-poll sees the final state rather
            # than a connection error the instant the last frame lands.
            threading.Timer(WEB_UI_LINGER_S, stop_ui).start()
        for path in spec.get("cleanup", []):
            try:
                os.remove(path)
            except OSError:
                pass
        shutil.rmtree(workdir, ignore_errors=True)


def _set_title(title):
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW(title)
        except Exception:
            pass


def _pause(message):
    try:
        if sys.stdin and sys.stdin.isatty():
            input(message)
    except (EOFError, OSError):
        pass


def _never_crash_on_output():
    """Characters the output cannot show are replaced, never fatal.

    The VRAM gauge and the logo print block characters. A real Windows console
    shows them, but a pipe, a log file or a non-UTF-8 terminal (macOS/Linux
    with a C locale) cannot encode them, and the UnicodeEncodeError killed the
    watcher - so the render failed over a progress bar. Measured: every job
    exited with code 1 under a cp1252 pipe before this (5.12.1).
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def main(argv):
    _never_crash_on_output()
    args = argv[argv.index("--") + 1:] if "--" in argv else argv[1:]

    if args and args[0] == "--watch":
        job = _read_json(args[1])
        try:
            os.remove(args[1])
        except OSError:
            pass
        _set_title("CLI Render - %s" % job["name"])
        code = watch(job)
        # A failed render keeps its window open, so the reason can be read.
        # Stops and out-of-memory kills are handled by the queue: no pause.
        if code not in (0, scheduler.STOP_CODE, scheduler.OOM_CODE) \
                and job.get("pause_on_error", True) and not job.get("prefix"):
            _pause("\nPress Enter to close...")
        return code

    jobfile = args[0]
    spec = _read_json(jobfile)
    if not spec.get("jobs"):
        # Say so rather than dying on a KeyError: with the queue window on
        # there is no console here, so an unreadable job file would otherwise
        # look like "the render simply never started".
        _say("Could not read the render queue from %s - nothing was started."
             % jobfile)
        _pause("Press Enter to close...")
        return 1
    _set_title("CLI Render queue")
    results = run(spec)
    try:
        os.remove(jobfile)
    except OSError:
        pass

    failed = [r for r in results if r[1] != 0]
    _say("")
    _say("Done: %d OK, %d failed" % (len(results) - len(failed), len(failed)))
    for name, code, _took, note in failed:
        _say("   %s: %s" % (name, note or "exit code %s" % code))
    if failed and spec.get("pause_on_error", True):
        _pause("Press Enter to close...")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
