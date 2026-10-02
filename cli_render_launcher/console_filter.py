"""Turn a render process's raw console output into a readable view.

A 3-frame test render printed 342 lines, of which ~20 were the render: the
rest was other add-ons loading (and failing) in every render process. This
filter keeps what matters:

  * one progress line per frame: frame, position, frames to go, rate, ETA;
  * a live "sample 512/1024" line that updates in place;
  * render problems, each shown once (a missing texture repeats on every
    frame - it is counted, not reprinted);
  * a summary at the end, with the raw tail when Blender exits with an error.

Levels:
  CLEAN     progress + render problems; add-on noise is only counted
  DETAILED  progress + every warning/error, add-on noise included
  FULL      everything Blender prints, untouched

Recognised formats are Blender 5.x ("00:02.453  render   | Rendering frame 1")
and 4.x ("Fra:1 Mem:12M ... | Sample 3/64"). Recorded real output lives in
tests/fixtures. No bpy - tested with plain Python (tests/test_console_filter.py).
"""
import re
import time

# Plain import, not relative: runner.py loads this module as a top level
# module after putting the add-on folder on sys.path, because the whole
# watcher runs OUTSIDE Blender where there is no package context.
import banner  # noqa: E402

LEVELS = ('CLEAN', 'DETAILED', 'FULL')
RATE_WINDOW = 5            # frames averaged for the "at the current rate" ETA
# How often the VRAM gauge is drawn, in rendered frames. Every frame would
# push the progress lines apart for no new information -- VRAM moves slowly
# once a scene is loaded -- and the GPU is only re-read every GPU_POLL_S
# anyway, so a tighter gauge would just redraw the same number.
GAUGE_EVERY = 10

# 5.x core log line:  "00:02.453  render           | Rendering frame 1"
_CLOG = re.compile(r"^\s*\d{2}:\d{2}\.\d{3}\s+(\S+)\s*\|\s?(.*)$")
_READ_BLEND = re.compile(r"Read blend:")
_ANIM_RANGE = re.compile(r"Rendering animation \(frames (-?\d+)\.\.(-?\d+)\)")
_FRAME_START = re.compile(r"Rendering frame (-?\d+)")
_FRA = re.compile(r"Fra:\s*(-?\d+)")
_SAMPLE = re.compile(r"Sample (\d+)/(\d+)|Rendering (\d+) / (\d+) samples")
_FRAME_END = re.compile(r"\bTime: (\d+:\d+(?:\.\d+)?) \(Saving")
_QUIT = re.compile(r"^\s*Blender quit\s*$")
_TRACEBACK = re.compile(r"^(Traceback \(most recent call last\):|Exception ignored in)")
_TB_FILE = re.compile(r'^\s+File "([^"]+)"')
_TB_END = re.compile(r"^[A-Za-z_][\w.]*(Error|Exception|Exit|Interrupt|Warning)\b")
_DRIVER = re.compile(r"^Error in PyDriver: (?:expression failed: )?(.*)$")
_DRIVER_TARGET = re.compile(r'^For target: \(type=(\w+), name="([^"]*)", property=(\w+)(?:, property_index=(\d+))?')

# Plain (non-core) lines that are about the render, wherever they come from.
_RENDER_PROBLEM = re.compile(
    r"out of memory|CUDA error|OptiX error|HIP error|Metal error|oneAPI error|"
    r"illegal (address|instruction)|segmentation fault|EXCEPTION_|"
    r"unable to open|cannot open|can't open|not found|missing|"
    r"failed to (load|open|read|write|save)",
    re.I)
# Tracebacks from these places are add-ons / libraries, not the scene.
_NOISE_PATHS = ("scripts\\addons", "scripts/addons", "\\extensions\\", "/extensions/",
                "\\python\\lib", "/python/lib", "site-packages", "\\addons_core\\",
                "/addons_core/", "\\modules\\", "/modules/")


def fmt_duration(seconds):
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%dh%02dm" % (h, m)
    if m:
        return "%dm%02ds" % (m, s)
    return "%ds" % s


class ConsoleFilter:
    """Feed raw lines in, get display events out.

    Events:
      ("print", text)  a normal line
      ("live", text)   the one line that updates in place (sample progress)
      ("final", text)  replaces the live line for good ("done in 2m09s")
    A console writer draws "live" with a carriage return; a shared terminal
    (several jobs interleaved) skips "live" and prints "final" normally.
    """

    def __init__(self, name, frames, level='CLEAN', log_path=None, clock=time.time,
                 gauge=None, gauge_every=GAUGE_EVERY, colour=False,
                 gpu_warn=66, gpu_stop=90):
        self.name = name
        self.frames = list(frames)
        self.level = level if level in LEVELS else 'CLEAN'
        self.log_path = log_path
        self.clock = clock
        # `gauge` is a zero-argument callable returning the GPU watch's most
        # recent reading, NOT a fresh nvidia-smi call: this runs on the output
        # path, once per rendered frame, and must never block it.
        self.gauge = gauge
        self.gauge_every = max(1, int(gauge_every))
        self.colour = colour
        self.gpu_warn, self.gpu_stop = gpu_warn, gpu_stop

        self.phase = 'startup'          # startup -> loading -> render -> shutdown
        self.started = clock()
        self.current = None             # frame being rendered
        self.frame_t0 = None
        self.durations = []             # seconds per finished frame
        self.done = 0
        self.noise = 0                  # add-on problem lines, only counted
        self.seen = {}                  # problem text -> times seen
        self.raw_tail = []              # last raw lines, shown if Blender fails
        self._tb = None                 # traceback being collected
        self._driver = None             # PyDriver error being collected
        self._noise_announced = False

    # -- helpers ----------------------------------------------------------

    def header(self):
        if self.level == 'FULL':
            return []                   # FULL = exactly what Blender prints
        if not self.frames:
            return [("print", self.name)]
        first, last = self.frames[0], self.frames[-1]
        n = len(self.frames)
        text = "%s · frame%s %d%s (%d frame%s)" % (
            self.name, "s" if n > 1 else "", first,
            "-%d" % last if n > 1 else "", n, "s" if n > 1 else "")
        out = [("print", text)]
        if self.log_path and self.level != 'FULL':
            out.append(("print", "  full log: %s" % self.log_path))
        return out

    def _problem(self, text, relevant):
        """Show a problem once; repeats are only counted."""
        text = text.strip()
        if not relevant and self.level == 'CLEAN':
            self.noise += 1
            return []
        count = self.seen.get(text, 0)
        self.seen[text] = count + 1
        if count:
            return []
        return [("print", "  ! " + text)]

    def _position(self, frame):
        try:
            return self.frames.index(frame) + 1
        except ValueError:
            return self.done + 1

    def _rate(self):
        recent = self.durations[-RATE_WINDOW:]
        return sum(recent) / len(recent) if recent else None

    def _frame_start(self, frame):
        out = []
        if not self._noise_announced and self.noise and self.level == 'CLEAN':
            where = "see the full log" if self.log_path else "not shown"
            out.append(("print", "  (%d add-on message%s while Blender started, not from "
                                 "this render - %s)" % (self.noise, "s" if self.noise > 1 else "", where)))
            self._noise_announced = True
        self.current, self.frame_t0 = frame, self.clock()
        total = len(self.frames) or None
        pos = self._position(frame)
        parts = ["> frame %d" % frame]
        if total:
            parts[0] += " (%d/%d)" % (pos, total)
            parts.append("last %d" % self.frames[-1])
            to_go = total - self.done
            parts.append("%d to go" % to_go)
            rate = self._rate()
            if rate is None:
                parts.append("measuring speed...")
            else:
                left = rate * to_go
                done_at = time.strftime("%H:%M", time.localtime(self.clock() + left))
                parts.append("%s/frame" % fmt_duration(rate))
                parts.append("~%s left (done ~%s)" % (fmt_duration(left), done_at))
        out.append(("print", " · ".join(parts)))
        out.extend(self._gauge_lines())
        return out

    def _gauge_lines(self):
        """A VRAM bar every `gauge_every` frames, one line per GPU.

        FULL is exactly what Blender printed, so nothing is added there.
        A missing reading (no nvidia-smi, or the first frame before the GPU
        thread has sampled) simply draws nothing rather than a row of zeros.
        """
        if self.gauge is None or self.level == 'FULL':
            return []
        if self.done % self.gauge_every:
            return []
        try:
            gpus = self.gauge()
        except Exception:
            return []
        out = []
        for _idx, name, used, total in (gpus or []):
            text = banner.vram_gauge(used, total, name=name,
                                     warn=self.gpu_warn, stop=self.gpu_stop,
                                     colour=self.colour)
            if text:
                out.append(("print", "  " + text))
        return out

    def _frame_end(self):
        if self.current is None:
            return []
        took = self.clock() - self.frame_t0
        self.durations.append(took)
        self.done += 1
        self.current = None
        if self.frames and self.done >= len(self.frames):
            self.phase = 'shutdown'
        return [("final", "    done in %s" % fmt_duration(took))]

    def _flush_traceback(self):
        tb, self._tb = self._tb, None
        if not tb or not any(l.strip() for l in tb["lines"]):
            return []
        files = [m.group(1) for m in (_TB_FILE.match(l) for l in tb["lines"]) if m]
        from_addon = any(any(p in f.lower() for p in _NOISE_PATHS) for f in files)
        last = tb["lines"][-1].strip() if tb["lines"] else "Python error"
        where = ""
        if files and not from_addon:
            where = " (in %s)" % files[-1]
        relevant = (not from_addon) and self.phase in ('loading', 'render')
        return self._problem("Python error%s: %s" % (where, last), relevant)

    def _flush_driver(self):
        drv, self._driver = self._driver, None
        if not drv:
            return []
        target = drv.get("target") or ""
        err = drv.get("error") or ""
        text = "Driver error: %s%s%s" % (drv["expr"], (" on " + target) if target else "",
                                         (" - " + err) if err else "")
        return self._problem(text, True)

    # -- main entry -------------------------------------------------------

    def feed(self, raw):
        line = raw.rstrip("\r\n")
        self.raw_tail.append(line)
        del self.raw_tail[:-40]
        if self.level == 'FULL':
            return [("print", line)]

        out = []
        # multi-line blocks first: a driver error, then a Python traceback
        if self._driver is not None:
            m = _DRIVER_TARGET.match(line)
            if m:
                typ, name, prop, idx = m.groups()
                self._driver["target"] = "%s \"%s\" %s%s" % (
                    typ, name, prop, "[%s]" % idx if idx is not None else "")
                return out
            # Blender prints a blank line between the target and the traceback
            if not line.strip() or _TRACEBACK.match(line) or _TB_FILE.match(line):
                return out
            if _TB_END.match(line):
                self._driver["error"] = line.strip()
                return self._flush_driver()
            out += self._flush_driver()
        if self._tb is not None:
            if line.startswith((" ", "\t")) or not line.strip():
                self._tb["lines"].append(line)
                return out
            if _TB_END.match(line):
                self._tb["lines"].append(line)
                return out + self._flush_traceback()
            out += self._flush_traceback()

        if line.startswith(("The above exception was", "During handling of the above")):
            return out                  # connector between chained tracebacks
        m = _DRIVER.match(line)
        if m:
            self._driver = {"expr": m.group(1).strip()}
            return out
        if _TRACEBACK.match(line):
            self._tb = {"lines": []}
            return out

        clog = _CLOG.match(line)
        msg = clog.group(2) if clog else line.strip()

        if _READ_BLEND.search(msg) and self.phase == 'startup':
            self.phase = 'loading'
            return out
        if _QUIT.match(line):
            self.phase = 'shutdown'
            return out
        m = _ANIM_RANGE.search(msg)
        if m:
            if not self.frames:
                self.frames = list(range(int(m.group(1)), int(m.group(2)) + 1))
            return out

        m = _FRAME_START.search(msg)
        fra = _FRA.search(msg)
        new_frame = None
        if m:
            new_frame = int(m.group(1))
        elif fra and self.current is None and self.phase in ('loading', 'render'):
            new_frame = int(fra.group(1))          # 4.x has no "Rendering frame"
        if new_frame is not None and new_frame != self.current:
            if self.current is not None:           # previous frame never reported its end
                out += self._frame_end()
            self.phase = 'render'
            return out + self._frame_start(new_frame)

        if _FRAME_END.search(msg):
            return out + self._frame_end()

        s = _SAMPLE.search(msg)
        if s and self.current is not None:
            done, total = (s.group(1), s.group(2)) if s.group(1) else (s.group(3), s.group(4))
            return out + [("live", "    sample %s/%s" % (done, total))]

        # problems
        if clog:
            level_word = msg.split(" ", 1)[0]
            if level_word in ("WARNING", "ERROR", "FATAL"):
                return out + self._problem(msg, self.phase != 'startup')
            if self.level == 'DETAILED' and self.phase == 'startup' and "rror" in msg:
                return out + self._problem(msg, False)
            return out                              # routine core status line
        if self.phase in ('loading', 'render') and _RENDER_PROBLEM.search(line):
            return out + self._problem(line, True)
        if re.search(r"\b(error|warning|exception|failed)\b", line, re.I):
            return out + self._problem(line, False)
        return out                                  # routine add-on chatter

    def finish(self, exit_code):
        out = self._flush_driver() + self._flush_traceback()
        if self.level == 'FULL':
            return out
        if self.current is not None and exit_code == 0:
            out += self._frame_end()
        elapsed = self.clock() - self.started
        total = len(self.frames)
        if exit_code == 0:
            rate = sum(self.durations) / len(self.durations) if self.durations else 0
            out.append(("print", "Finished: %d frame%s in %s%s" % (
                self.done, "s" if self.done != 1 else "", fmt_duration(elapsed),
                " (%s/frame)" % fmt_duration(rate) if self.durations else "")))
        else:
            out.append(("print", "STOPPED: Blender exited with code %s after %d/%s frames"
                        % (exit_code, self.done, total or "?")))
            out.append(("print", "  last lines Blender printed:"))
            out += [("print", "    " + l) for l in self.raw_tail[-15:] if l.strip()]
        repeated = [(t, n) for t, n in self.seen.items() if n > 1]
        for text, n in repeated:
            out.append(("print", "  ! repeated %d times: %s" % (n, text)))
        if self.noise and self.level == 'CLEAN':
            out.append(("print", "  (%d add-on message%s hidden%s)" % (
                self.noise, "s" if self.noise != 1 else "",
                " - full log: %s" % self.log_path if self.log_path else "")))
        return out
