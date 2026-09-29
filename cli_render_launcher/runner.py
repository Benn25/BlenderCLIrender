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
     "cleanup": ["...snapshot.blend"]}  # deleted once every job has ended

Every render runs under a watcher (its own console on Windows, the shared
terminal elsewhere) which filters the output (console_filter), keeps the raw
log, raises GPU alarms, reports its current frame in a status file, stops
on request, and kills Blender the moment it runs out of GPU memory. What
starts, stops and retries is decided by scheduler.Scheduler.

No bpy imports - tested with plain Python (tests/test_runner.py).
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from console_filter import ConsoleFilter  # noqa: E402
import banner  # noqa: E402
import gpu_monitor  # noqa: E402
import scheduler  # noqa: E402

GPU_POLL_S = 5.0
STOP_POLL_S = 0.3
_print_lock = threading.Lock()

# Cycles / Blender wording for running out of GPU memory (CUDA, OptiX, HIP,
# Metal, oneAPI all end up in one of these).
_OOM = re.compile(r"out of (gpu |device |video )?memory|OUT_OF_MEMORY|"
                  r"System is out of GPU", re.I)


def _say(text):
    with _print_lock:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()


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
        with open(path, encoding="utf-8") as fh:
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
    flt = ConsoleFilter(job["name"], job.get("frames") or [],
                        level=job.get("level", "CLEAN"),
                        log_path=job.get("log") if log else None)

    def status(**extra):
        data = {"phase": "render" if flt.current is not None else "loading",
                "frame": flt.current, "since": frame_since[0],
                "resume": _resume_frame(flt), "done": flt.done}
        data.update(extra)
        _write_json(status_path, data)

    frame_since = [None]
    status()
    # The Blender logo, only in a render's own console window (a shared
    # terminal would get one logo per job, interleaved).
    if writer.tty and job.get("banner", True):
        colour = banner.enable_colour(writer.stream)
        title = "CLI RENDER  v%s  ·  %s" % (banner.addon_version(), job["name"])
        for text in banner.lines(title, colour=colour):
            writer.emit("print", text)
    for kind, text in flt.header():
        writer.emit(kind, text)
    if job.get("note"):
        writer.emit("print", "  (%s)" % job["note"])
    gpu = gpu_monitor.GpuWatch(threshold=job.get("gpu_warn", 66), reader=gpu_reader)
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
                    status=base + ".status.json", stop=base + ".stop",
                    prefix=not own_console,
                    pause_on_error=spec.get("pause_on_error", True))
        jobfile = base + ".job.json"
        _write_json(jobfile, full)
        flags = subprocess.CREATE_NEW_CONSOLE if own_console else 0
        proc = subprocess.Popen(_watcher_command(jobfile), creationflags=flags)
        return WatcherHandle(proc, full["status"], full["stop"])
    return launch


def run(spec, say=_say, gpu_reader=gpu_monitor.read):
    """Run the whole queue; returns [(name, exit_code, seconds, note)].
    Files in spec["cleanup"] are deleted once all jobs have ended."""
    workdir = tempfile.mkdtemp(prefix="cli_render_queue_")
    use_gpu = bool(spec.get("gpu_warn", 66) or spec.get("gpu_stop", 90))
    sch = scheduler.Scheduler(spec, make_launcher(spec, workdir),
                              gpu_reader=gpu_reader if use_gpu else None, say=say)
    try:
        return sch.run()
    finally:
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


def main(argv):
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
