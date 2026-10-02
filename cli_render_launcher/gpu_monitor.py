"""GPU memory watch for the render consoles.

GPU memory is what runs out first when several SubScenes render at once, and
a render that runs out either crashes or crawls. The WHOLE card is watched
(Blender's UI, browsers etc. use it too - 46% was taken before any render on
the development machine), through nvidia-smi, which ships with NVIDIA drivers
on Windows and Linux. No nvidia-smi (AMD/Intel): the watch says so and stays off.

No bpy imports - tested with plain Python (tests/test_gpu_monitor.py).
"""
import os
import shutil
import subprocess
import sys

QUERY = ["--query-gpu=index,name,memory.used,memory.total",
         "--format=csv,noheader,nounits"]


def _nvidia_smi():
    found = shutil.which("nvidia-smi")
    if found:
        return found
    if sys.platform == "win32":
        path = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                            "System32", "nvidia-smi.exe")
        if os.path.isfile(path):
            return path
    return None


def parse(text):
    """'0, NVIDIA GeForce RTX 5080, 7517, 16303' -> [(0, name, used_mb, total_mb)]"""
    gpus = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 4:
            continue
        try:
            gpus.append((int(parts[0]), parts[1], float(parts[2]), float(parts[3])))
        except ValueError:
            continue
    return gpus


def read():
    """Current memory of every NVIDIA GPU, or None if it cannot be read."""
    exe = _nvidia_smi()
    if not exe:
        return None
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # never flash a window
    try:
        out = subprocess.run([exe] + QUERY, capture_output=True, text=True,
                             timeout=5, creationflags=flags)
    except (OSError, subprocess.SubprocessError):
        return None
    gpus = parse(out.stdout) if out.returncode == 0 else []
    return gpus or None


def _gb(mb):
    return mb / 1024.0


class GpuWatch:
    """Turns periodic readings into alarms.

    Alarm when a GPU goes above `threshold` %; again only if it climbs a
    further `step` points; re-armed once it falls `margin` points below the
    threshold (66 -> 60). The margin must follow the threshold: a fixed
    re-arm level above a low threshold re-alarmed on every poll.
    """

    def __init__(self, threshold=66, step=10, margin=6, reader=read):
        self.threshold = threshold
        self.step = step
        self.rearm = threshold - margin
        self.reader = reader
        self.last_alarm = {}        # gpu index -> % at the last alarm
        self.peak = {}              # gpu index -> (pct, used, total, name)
        self.available = None
        # The most recent reading, so the per-frame gauge can draw from it
        # instead of spawning an nvidia-smi of its own on the output path.
        self.last = None

    def describe(self):
        """One header line: what is watched."""
        gpus = self.reader()
        self.available = bool(gpus)
        if not self.threshold:
            return None
        if not gpus:
            return "GPU memory: not watched (nvidia-smi not found - NVIDIA GPUs only)"
        names = ", ".join("%s (%.0f GB)" % (n, _gb(t)) for _i, n, _u, t in gpus)
        return "GPU memory watched: %s - alarm above %d%%" % (names, self.threshold)

    def sample(self):
        """Read once and remember it. Separate from check() because the gauge
        wants a reading even when the alarm is switched off (threshold 0) --
        but `available is False` still short-circuits, so a machine without
        nvidia-smi is not probed over and over."""
        if self.available is False:
            return None
        self.last = self.reader() or None
        return self.last

    def latest(self):
        """The last reading, without touching nvidia-smi. None until the
        first sample lands."""
        return self.last

    def check(self):
        """Poll once; returns alarm / all-clear texts (usually none)."""
        gpus = self.sample()
        if not self.threshold or self.available is False:
            return []
        if not gpus:
            return []
        out = []
        for idx, name, used, total in gpus:
            if total <= 0:
                continue
            pct = 100.0 * used / total
            if pct > self.peak.get(idx, (-1,))[0]:
                self.peak[idx] = (pct, used, total, name)
            last = self.last_alarm.get(idx)
            if last is None and pct >= self.threshold or \
                    last is not None and pct >= last + self.step:
                self.last_alarm[idx] = pct
                out.append("!! GPU MEMORY %d%% FULL (%.1f/%.1f GB, %s) - renders may "
                           "fail or slow down; fewer SubScenes at once would help"
                           % (pct, _gb(used), _gb(total), name))
            elif last is not None and pct < self.rearm:
                del self.last_alarm[idx]
                out.append("   GPU memory back to %d%% (%s)" % (pct, name))
        return out

    def summary(self):
        if not self.threshold or not self.peak:
            return None
        parts = []
        for idx in sorted(self.peak):
            pct, used, total, name = self.peak[idx]
            flag = "!! " if pct >= self.threshold else ""
            parts.append("%sGPU memory peak: %d%% (%.1f/%.1f GB, %s)"
                         % (flag, pct, _gb(used), _gb(total), name))
        return parts
