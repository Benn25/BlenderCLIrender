"""A render-queue viewer, served to the browser from the queue process.

Blender's bundled Python has NO GUI toolkit - measured on 5.2.2 / Python
3.13.13: no tkinter, no PySide6, no PyQt5, no pystray, no PIL. `http.server`
is there, so the browser does the drawing and this stays dependency-free.

It is a VIEW ONLY. The queue runs exactly as before whether anyone opens the
page or not: the server reads the scheduler's state on a background thread and
never writes to it, and it runs no commands on anyone's behalf. A closed
browser, a refused port or a crashed request cannot disturb a render.

Served on 127.0.0.1 only. Four routes, all GET:
    /               the page
    /api/state      the whole queue, as JSON
    /api/log        the tail of one job's log
    /file           one rendered frame, for the preview and its links

`/file` is the only one that touches the disk, and it serves a file ONLY if
its real path sits inside that job's own output directory and starts with that
job's output prefix - see `_allowed`. Nothing else on the machine is reachable.

No bpy - tested with plain Python (tests/test_webui.py).
"""
import glob
import json
import mimetypes
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlencode, parse_qs, urlparse

HOST = "127.0.0.1"
POLL_MS = 1000          # how often the page asks for new state
LOG_TAIL_BYTES = 24000  # enough to read, small enough to send every poll
MAX_FILES = 4000        # the full listing one /api/files call will return
THUMB_STRIP = 48        # contact-sheet thumbnails carried in the live state
GPU_HISTORY = 180       # samples kept for the sparkline


# --------------------------------------------------------------------------
# Job output: where the frames land
# --------------------------------------------------------------------------

def output_prefix(cmd):
    """The -o value of a render command, or None.

    Blender writes `<prefix><frame>.<ext>`, so the prefix is a folder plus the
    start of a file name - not a directory. Read from the command rather than
    recomputed, so it cannot disagree with what Blender was actually told.
    """
    try:
        i = list(cmd).index("-o")
    except (ValueError, TypeError, AttributeError):
        return None
    return cmd[i + 1] if i + 1 < len(cmd) else None


def _allowed(prefix, path):
    """True if `path` is a file this job actually produced.

    Both sides are realpath'd before comparing, so neither a '..' nor a
    symlink can point the answer outside the job's own output folder.
    """
    if not prefix or not path:
        return False
    try:
        root = os.path.realpath(os.path.dirname(prefix))
        real = os.path.realpath(path)
    except OSError:
        return False
    if os.path.commonpath([root, real]) != root:
        return False
    return os.path.basename(real).startswith(os.path.basename(prefix))


# Containers written progressively: the file exists from the first frame but
# is unplayable until the muxer closes it, so linking one mid-render only ever
# produces a broken player. Listed, sized, but not offered until the job ends.
VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".mpg", ".mpeg",
             ".ogv", ".flv", ".wmv"}
# What a browser can actually draw. EXR, TIFF, DPX and the rest are listed and
# downloadable, never previewed - a thumbnail that cannot decode is worse than
# no thumbnail. Keep in step with the VIEWABLE regex in the page.
VIEWABLE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif"}


def is_video_name(name):
    return os.path.splitext(name)[1].lower() in VIDEO_EXT


def is_viewable_name(name):
    return os.path.splitext(name)[1].lower() in VIEWABLE_EXT


def _scan(prefix):
    """{name: (mtime_ns, size)} for one output location, as it is right now."""
    out = {}
    if not prefix:
        return out
    folder = os.path.dirname(prefix) or "."
    stem = os.path.basename(prefix)
    try:
        with os.scandir(folder) as it:
            for de in it:
                if not de.name.startswith(stem):
                    continue
                try:
                    if de.is_file():
                        st = de.stat()
                        out[de.name] = (st.st_mtime_ns, st.st_size)
                except OSError:
                    continue
    except OSError:
        pass
    return out


def snapshot(jobs):
    """What every output location held BEFORE the queue wrote anything.

    Taken once, at queue start. A file is later called "new" when it was not in
    this snapshot or its (mtime, size) differs - so a frame left over from an
    earlier render is told apart from one this run produced. It deliberately
    compares against a snapshot rather than against the queue start TIME: the
    output often sits on a network share, where the file server clock and this
    machine clock can disagree, and a clock comparison would then mislabel files.
    """
    snap = {}
    for job in jobs or []:
        for prefix, _src, _label in job_prefixes(job.get("cmd"),
                                                 job.get("extra_outputs")):
            if prefix not in snap:
                snap[prefix] = _scan(prefix)
    return snap


def output_files(prefix, limit=MAX_FILES, source="render", finished=True,
                 baseline=None):
    """Files on disk for one prefix, newest last.

    Returns (count, total_bytes, [entries]). An entry is marked `writing` when
    it is a video container and the job has not ended - the page then shows it
    without a link, because opening a half-muxed file is never useful. `new` is
    True/False against `baseline` (see snapshot), or None when there is none.

    os.scandir, not glob+getsize: this runs once a SECOND per output location,
    and a finished sequence is routinely thousands of frames (the crusher job
    is 2701). scandir reads the directory once and carries the size with each
    entry, instead of a separate stat syscall per file.
    """
    if not prefix:
        return 0, 0, []
    folder = os.path.dirname(prefix) or "."
    stem = os.path.basename(prefix)
    entries, total = [], 0
    try:
        with os.scandir(folder) as it:
            for de in it:
                if not de.name.startswith(stem):
                    continue
                try:
                    if not de.is_file():
                        continue
                    st = de.stat()
                except OSError:
                    continue
                total += st.st_size
                entries.append({
                    "name": de.name, "size": st.st_size, "source": source,
                    # milliseconds: also the cache-buster for thumbnails, so an
                    # overwritten frame is refetched instead of shown stale
                    "mt": st.st_mtime_ns // 1_000_000,
                    "new": (None if baseline is None
                            else baseline.get(de.name) != (st.st_mtime_ns, st.st_size)),
                    "writing": bool(is_video_name(de.name) and not finished)})
    except OSError:
        return 0, 0, []
    entries.sort(key=lambda e: e["name"])
    shown = entries if limit is None else entries[-limit:]
    return len(entries), total, shown


def job_prefixes(job_cmd, extra):
    """[(prefix, source, label)] for every place one job writes.

    `extra` entries are {"prefix", "label"} since 5.10.0; a bare string is
    still accepted so a queue file written by an older build keeps working.
    """
    out = []
    main = output_prefix(job_cmd)
    if main:
        out.append((main, "render", "Render output"))
    for entry in (extra or []):
        if isinstance(entry, dict):
            prefix, label = entry.get("prefix"), entry.get("label") or "Compositor"
        else:
            prefix, label = entry, "Compositor"
        if prefix:
            out.append((prefix, "compositor", label))
    return out


_TRAILING_DIGITS = re.compile(r"(\d+)$")


def frame_of(name, stem=""):
    """The frame number Blender wrote into a file name, or None.

    Blender appends the frame to the prefix (`Crusher_` -> `Crusher_1083.jpg`),
    so it is the run of digits at the end of the name once the extension and
    the stem are off. The stem is stripped first so digits INSIDE it (`shot2_`)
    are never mistaken for the frame.
    """
    base = os.path.splitext(name)[0]
    if stem and base.startswith(stem):
        base = base[len(stem):]
    m = _TRAILING_DIGITS.search(base)
    return int(m.group(1)) if m else None


def pick_thumbs(viewable, stem, frames, n=THUMB_STRIP):
    """Which files the contact sheet shows: (entries, (first, last) or None).

    The frames THIS job renders, not simply the last names in the folder.
    Re-rendering 1083-1689 of a 3181-frame sequence used to show 3134-3181,
    frames the job never touches. Within the job's own frames the window ends
    at the newest one written this run, so it follows the render as it goes and
    the boundary with the not-yet-redone frames stays in view; before anything
    is written it shows the first frames about to be redone.

    Falls back to the last `n` by name when the job has no frame list or none
    of its frames are on disk yet.
    """
    if not frames:
        return viewable[-n:], None
    wanted = set(frames)
    ranged = []
    for e in viewable:
        f = frame_of(e["name"], stem)
        if f is not None and f in wanted:
            ranged.append((f, e))
    if not ranged:
        return viewable[-n:], None
    ranged.sort(key=lambda fe: fe[0])
    if ranged[0][1]["new"] is None:
        # No snapshot (window opened without one): the newest file is the
        # best available guess at where the render has got to. Ties go to the
        # higher frame - a network share can stamp mtimes in whole seconds, and
        # several fast frames then share one.
        reach = max(range(len(ranged)), key=lambda i: (ranged[i][1]["mt"], i))
    else:
        reach = -1
        for i, (_f, e) in enumerate(ranged):
            if e["new"]:
                reach = i
    start = max(0, reach + 1 - n)
    window = ranged[start:start + n]
    return [e for _f, e in window], (window[0][0], window[-1][0])


def sequences_for(job_cmd, extra, finished, thumbs=THUMB_STRIP, baselines=None,
                  frames=None):
    """One record per place this job writes - a 'sequence' in the window.

    Deliberately carries NO full file list: the crusher job writes 2701 frames
    and shipping every name in a once-a-second poll is ~80 KB each time. The
    live state carries counts, sizes and the last `thumbs` previewable files
    (the contact sheet); the complete listing comes from /api/files when a card
    is actually open.

    `new`/`old` count files made by this run versus left over from before it
    (None when no snapshot was taken). Each thumbnail carries its own flag and
    its mtime, so the page can mark stale frames and refetch overwritten ones.
    """
    out = []
    for idx, (prefix, source, label) in enumerate(job_prefixes(job_cmd, extra)):
        base = (baselines or {}).get(prefix)
        count, size, entries = output_files(prefix, limit=None, source=source,
                                            finished=finished, baseline=base)
        exts = sorted({os.path.splitext(e["name"])[1].lower() for e in entries})
        viewable = [e for e in entries
                    if not e["writing"] and is_viewable_name(e["name"])]
        known = base is not None
        shown, span = pick_thumbs(viewable, os.path.basename(prefix), frames, thumbs)
        out.append({"i": idx, "label": label, "kind": source,
                    "dir": os.path.dirname(prefix),
                    "count": count, "bytes": size,
                    "exts": [e for e in exts if e],
                    "new": sum(1 for e in entries if e["new"]) if known else None,
                    "old": sum(1 for e in entries if e["new"] is False) if known else None,
                    "viewable": len(viewable),
                    "writing": sum(1 for e in entries if e["writing"]),
                    "first": entries[0]["name"] if entries else None,
                    "last": entries[-1]["name"] if entries else None,
                    "thumbs": [{"n": e["name"], "new": e["new"], "v": e["mt"]}
                               for e in shown],
                    # first/last frame on the contact sheet, so the page can say
                    # which stretch it is showing (None = the last files by name)
                    "span": list(span) if span else None,
                    # Changes whenever a file appears, disappears or is
                    # rewritten. The count alone did not: overwriting frames in
                    # place keeps it constant, so an open list never refreshed.
                    "sig": "%d:%d:%d" % (count, size,
                                         max((e["mt"] for e in entries), default=0))})
    return out


def all_outputs(job_cmd, extra_prefixes, finished, baselines=None, frames=None):
    """Totals across every sequence, for the job header and the queue stats."""
    seqs = sequences_for(job_cmd, extra_prefixes, finished, baselines=baselines,
                         frames=frames)
    return (sum(s["count"] for s in seqs),
            sum(s["bytes"] for s in seqs),
            seqs)


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def _job_entry(job, handle, started, now, baselines=None):
    st = handle.status() or {}
    done = st.get("done") or 0
    total = len(job.frames) or 0
    phase = st.get("phase") or "loading"
    elapsed = max(now - started, 0.0)
    avg = st.get("avg_s") or ((elapsed / done) if done else None)
    left = (avg * (total - done)) if (avg and total) else None
    count, size, seqs = all_outputs(job.cmd, job.extra_outputs, finished=False,
                                    baselines=baselines, frames=job.frames)
    return {"i": job.uid, "name": job.name,
            "state": "stopping" if getattr(handle, "stopping", False) else phase,
            "frame": st.get("frame"),
            "done": done, "total": total,
            "pct": (100.0 * done / total) if total else 0.0,
            "eta_s": left, "elapsed_s": elapsed,
            "avg_s": st.get("avg_s"), "fast_s": st.get("fast_s"),
            "slow_s": st.get("slow_s"), "last_s": st.get("last_s"),
            "problems": st.get("problems") or 0,
            "problem_kinds": st.get("problem_kinds") or 0,
            "noise": st.get("noise") or 0,
            "first_frame": job.frames[0] if job.frames else None,
            "last_frame": job.frames[-1] if job.frames else None,
            "sequences": seqs,
            "out_count": count, "out_bytes": size,
            "has_log": bool(job.log),
            "alone": bool(job.alone), "resumed": bool(job.resumed),
            "scene": getattr(job, "scene", None),
            "batch": getattr(job, "batch", 0),
            "note": job.note or ""}


def _idle_entry(job, state, baselines=None):
    # A finished job's videos are complete, so they become openable; a pending
    # one has written nothing yet either way.
    count, size, seqs = all_outputs(
        job.cmd, job.extra_outputs, finished=(state in ("done", "failed")),
        baselines=baselines, frames=job.frames)
    # Empty while pending; the watcher's parting numbers once it has finished.
    st = getattr(job, "final_status", None) or {}
    return {"i": job.uid, "name": job.name, "state": state,
            "frame": None, "done": st.get("done") or 0, "total": len(job.frames),
            "pct": 0.0, "eta_s": None, "elapsed_s": 0.0,
            "avg_s": st.get("avg_s"), "fast_s": st.get("fast_s"),
            "slow_s": st.get("slow_s"), "last_s": st.get("last_s"),
            "problems": st.get("problems") or 0,
            "problem_kinds": st.get("problem_kinds") or 0,
            "noise": st.get("noise") or 0,
            "first_frame": job.frames[0] if job.frames else None,
            "last_frame": job.frames[-1] if job.frames else None,
            "sequences": seqs,
            "out_count": count, "out_bytes": size,
            "has_log": bool(job.log),
            "alone": bool(job.alone), "resumed": bool(job.resumed),
            "scene": getattr(job, "scene", None),
            "batch": getattr(job, "batch", 0),
            "note": job.note or ""}


def state(sch, spec, gpu_cache=None, gpu_history=None, clock=None, started_at=None,
          baselines=None, gpu_interval=None):
    """Everything the page draws, built from the scheduler as it stands.

    Read-only, and defensive throughout: this runs on the server thread while
    the queue mutates its own lists, so anything that disappears mid-build is
    skipped rather than raised.
    """
    now = (clock or time.time)()
    jobs = []
    try:
        for job, handle, started in list(sch.running):
            try:
                jobs.append(_job_entry(job, handle, started, now, baselines))
            except Exception:
                pass
    except Exception:
        pass
    for job in list(getattr(sch, "pending", [])):
        try:
            jobs.append(_idle_entry(job, "pending", baselines))
        except Exception:
            pass
    for job in list(getattr(sch, "finished", [])):
        code, secs, note = job.result or (None, 0.0, None)
        try:
            e = _idle_entry(job, "done" if code == 0 else "failed", baselines)
        except Exception:
            continue
        e.update(code=code, elapsed_s=secs, note=note or e["note"], pct=100.0)
        # A job that exited 0 rendered every frame it was given. Without this
        # the queue total read "0 / 200 frames" with 200 files on disk, because
        # the per-frame count lives in the watcher and dies with it.
        if code == 0:
            e["done"] = e["total"]
        jobs.append(e)

    gpu = None
    reading = gpu_cache() if gpu_cache else None
    if reading:
        _i, name, used, total = max(reading, key=lambda g: (g[3] and g[2] / g[3]) or 0)
        if total:
            gpu = {"name": name, "pct": 100.0 * used / total,
                   "used_gb": used / 1024.0, "total_gb": total / 1024.0,
                   "history": list(gpu_history or []),
                   # seconds between samples, so the page can say how far back
                   # the history reaches instead of showing unlabelled bars
                   "interval_s": gpu_interval}
    running = sum(1 for j in jobs if j["state"] in ("render", "loading", "stopping"))
    pending = sum(1 for j in jobs if j["state"] == "pending")
    frames_done = sum(j["done"] for j in jobs)
    frames_total = sum(j["total"] for j in jobs)
    return {"queue": {"total": len(jobs), "running": running, "pending": pending,
                      "finished": sum(1 for j in jobs
                                      if j["state"] in ("done", "failed")),
                      "failed": sum(1 for j in jobs if j["state"] == "failed"),
                      "parallel": getattr(sch, "parallel", 1),
                      "frames_done": frames_done, "frames_total": frames_total,
                      # `is not None`, not a truth test: a started_at of 0.0 is
                      # a real time, and only a test ever passes one - which is
                      # exactly when a silently-missing field is hardest to see.
                      "elapsed_s": (now - started_at) if started_at is not None else None,
                      "out_bytes": sum(j["out_bytes"] for j in jobs),
                      "out_count": sum(j["out_count"] for j in jobs),
                      "all_done": running == 0 and pending == 0},
            "gpu": gpu,
            "levels": {"warn": spec.get("gpu_warn", 66) or 0,
                       "stop": spec.get("gpu_stop", 90) or 0},
            "scene": spec.get("scene") or None,
            "jobs": jobs}


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------

def _tail(path, limit=LOG_TAIL_BYTES):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > limit:
                fh.seek(size - limit)
            data = fh.read()
    except OSError as exc:
        return "(log not readable: %s)" % exc
    text = data.decode("utf-8", "replace")
    return text.split("\n", 1)[1] if size > limit and "\n" in text else text


class _Handler(BaseHTTPRequestHandler):
    jobs_fn = None       # -> [scheduler.Job] for log/file lookups
    baselines = None     # {prefix: {name: (mtime_ns, size)}} from snapshot()
    state_fn = None
    page = b""

    def log_message(self, *a):          # never scribble over the render console
        pass

    def _send(self, body, ctype, extra=None):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass                        # the tab was closed mid-reply

    def _job(self, query):
        """The job dict a request names, by its uid = position in the queue."""
        try:
            idx = int(query.get("job", ["-1"])[0])
        except (ValueError, TypeError):
            return None
        jobs = self.jobs_fn() if self.jobs_fn else []
        return jobs[idx] if 0 <= idx < len(jobs) else None

    def do_GET(self):
        parts = urlparse(self.path)
        path, query = parts.path, parse_qs(parts.query)
        if path in ("/", "/index.html"):
            self._send(self.page, "text/html; charset=utf-8")
            return
        if path == "/api/state":
            try:
                data = self.state_fn()
            except Exception as exc:
                data = {"error": "%s: %s" % (type(exc).__name__, exc)}
            self._send(json.dumps(data).encode("utf-8"),
                       "application/json; charset=utf-8")
            return
        if path == "/api/files":
            job = self._job(query)
            try:
                seq = int((query.get("seq") or ["-1"])[0])
            except (ValueError, TypeError):
                seq = -1
            prefixes = job_prefixes(job.get("cmd"), job.get("extra_outputs")) if job else []
            if not (0 <= seq < len(prefixes)):
                self.send_error(404)
                return
            prefix, source, label = prefixes[seq]
            fin = bool((query.get("finished") or ["0"])[0] == "1")
            count, size, entries = output_files(
                prefix, limit=MAX_FILES, source=source, finished=fin,
                baseline=(self.baselines or {}).get(prefix))
            body = {"label": label, "kind": source, "count": count, "bytes": size,
                    "shown": len(entries), "files": entries}
            self._send(json.dumps(body).encode("utf-8"),
                       "application/json; charset=utf-8")
            return
        if path == "/api/log":
            job = self._job(query)
            log = job.get("log") if job else None
            body = _tail(log) if log else "(no log for this job)"
            self._send(body.encode("utf-8", "replace"), "text/plain; charset=utf-8")
            return
        if path == "/file":
            job = self._job(query)
            name = (query.get("n") or [""])[0]
            # Every prefix this job writes to: the -o frames and each File
            # Output node. A request is rebuilt from one of THOSE plus a
            # BASENAME and re-checked against it, so a caller still cannot
            # reach anything the job did not write.
            prefixes = [p for p, _s, _l in
                        job_prefixes(job.get("cmd"), job.get("extra_outputs"))] if job else []
            target = None
            for prefix in prefixes:
                candidate = os.path.join(os.path.dirname(prefix),
                                         os.path.basename(name))
                if _allowed(prefix, candidate) and os.path.isfile(candidate):
                    target = candidate
                    break
            if target is None:
                self.send_error(404)
                return
            ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
            try:
                with open(target, "rb") as fh:
                    body = fh.read()
            except OSError:
                self.send_error(404)
                return
            self._send(body, ctype)
            return
        self.send_error(404)


def serve(state_fn, page_html, port=0, jobs_fn=None, baselines=None):
    """Start the server on a daemon thread. Returns (url, stop) or (None, None).

    Never raises: a port the OS will not give us is a reason to carry on
    without a window, not to lose a render queue.
    """
    handler = type("_BoundHandler", (_Handler,),
                   {"state_fn": staticmethod(state_fn),
                    "jobs_fn": staticmethod(jobs_fn or (lambda: [])),
                    # the SAME dict the runner extends when a launch joins:
                    # `baselines or {}` would swap an empty one for a copy
                    "baselines": baselines if baselines is not None else {},
                    "page": page_html.encode("utf-8")})
    # The caller normally names a port it already reserved, so the address can
    # be shown in Blender before the queue even starts. If something took it in
    # between, any free port still beats no window.
    srv = None
    for candidate in ([port, 0] if port else [0]):
        try:
            srv = HTTPServer((HOST, candidate), handler)
            break
        except OSError:
            continue
    if srv is None:
        return None, None
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.3},
                     daemon=True).start()

    def stop():
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:
            pass
    return "http://%s:%d/" % (HOST, srv.server_address[1]), stop


def page(title="CLI Render queue", poll_ms=POLL_MS):
    """One self-contained page: no CDN, no fonts to fetch, works offline."""
    return _PAGE.replace("__TITLE__", title).replace("__POLL__", str(int(poll_ms)))


_PAGE = r"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
 :root{
   --bg:#111113; --panel:#1b1b1f; --panel2:#17171a; --line:#2a2a30;
   --text:#d6d6da; --dim:#86868f;
   --blue:#4a8fd6; --orange:#ff8700; --white:#ffffff;
   --ok:#5bbf71; --bad:#e0564b;
 }
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--text);
   font:14px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;padding:18px}
 h1{font-size:15px;font-weight:650;margin:0}
 .sub{color:var(--dim);font-size:12px;margin-top:3px}
 .head{display:flex;justify-content:space-between;align-items:flex-start;
   gap:18px;margin-bottom:14px;flex-wrap:wrap}
 .stats{display:flex;gap:20px;flex-wrap:wrap;margin-top:7px}
 .stat b{display:block;font-size:17px;font-weight:650;font-variant-numeric:tabular-nums}
 .stat span{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.06em}
 .gpu{min-width:300px}
 .bar{display:flex;gap:2px;height:16px;margin:5px 0 3px}
 .bar i{flex:1;border-radius:1px;background:#26262c}
 .sparkwrap{position:relative;margin-top:3px}
 .spark{display:flex;align-items:flex-end;gap:1px;height:30px}
 /* faint lines at the alarm and stop levels, so a bar's height reads against them */
 .gl{position:absolute;left:0;right:0;height:0;border-top:1px dashed #3a3a42;
   pointer-events:none}
 #glw{border-color:#5a3b10} #gls{border-color:#555560}
 .sparkcap{display:flex;justify-content:space-between;color:var(--dim);
   font-size:10.5px;margin-top:5px}
 .spark i{flex:1;background:#2f6ea8;border-radius:1px;min-height:1px;opacity:.75}
 .card{background:var(--panel);border:1px solid var(--line);border-radius:7px;
   margin-bottom:8px;overflow:hidden}
 .top{padding:11px 13px;cursor:pointer}
 .row{display:flex;justify-content:space-between;align-items:baseline;gap:12px}
 .nm{font-weight:600}
 .meta{color:var(--dim);font-size:12px;
   font-variant-numeric:tabular-nums;white-space:nowrap}
 .track{height:6px;background:#26262c;border-radius:3px;margin-top:9px;overflow:hidden}
 .fill{height:100%;background:var(--blue);border-radius:3px;transition:width .4s}
 .done .fill{background:var(--ok)} .failed .fill{background:var(--bad)}
 .tag{font-size:11px;padding:1px 7px;border-radius:999px;border:1px solid var(--line);
   color:var(--dim);text-transform:uppercase;letter-spacing:.05em}
 .t-render{color:var(--blue);border-color:#2d4d6b}
 .t-loading,.t-stopping{color:var(--orange);border-color:#5a3b10}
 .t-done{color:var(--ok);border-color:#2b4a33}
 .t-failed{color:var(--bad);border-color:#5a2a26}
 .note{color:var(--orange);font-size:12px;margin-top:6px}
 .body{border-top:1px solid var(--line);background:var(--panel2);padding:12px 13px;
   display:none}
 .open .body{display:block}
 .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(118px,1fr));gap:10px 16px}
 .g b{display:block;font-variant-numeric:tabular-nums}
 .g span{color:var(--dim);font-size:11px}
 .pathrow{display:flex;gap:8px;align-items:center;margin-top:11px;flex-wrap:wrap}
 code{background:#0e0e10;border:1px solid var(--line);border-radius:4px;
   padding:3px 7px;font:12px/1.4 Consolas,monospace;color:#b6b6bd;
   overflow-wrap:anywhere}
 button{background:#26262c;border:1px solid var(--line);color:var(--text);
   border-radius:5px;padding:4px 10px;font:12px system-ui;cursor:pointer}
 button:hover{background:#30303a}
 .shots{display:flex;gap:8px;margin-top:11px;align-items:flex-start;flex-wrap:wrap}
 .shots img{width:168px;border:1px solid var(--line);border-radius:5px;background:#000}
 .files{margin-top:10px;max-height:190px;overflow:auto;border:1px solid var(--line);
   border-radius:5px}
 .files a{display:flex;justify-content:space-between;gap:12px;padding:4px 9px;
   color:#9fb6cc;text-decoration:none;font:12px/1.5 Consolas,monospace;
   border-bottom:1px solid #212127}
 .files a:hover{background:#22222a}
 .files a:last-child{border-bottom:0}
 /* one line per scene; several only once a launch from another scene joins */
 .scenerow{display:flex;gap:16px;flex-wrap:wrap;margin-top:3px}
 .scname{color:var(--text);font-weight:600}
 .sctag{color:var(--dim);font-size:11px;margin-left:8px}
 /* the row "Show every file" opens on, briefly marked so the eye finds it */
 .files .focus{background:#22324a;transition:background 1.2s}
 .files .no{display:flex;justify-content:space-between;gap:12px;padding:4px 9px;
   color:#6f6f78;font:12px/1.5 Consolas,monospace;border-bottom:1px solid #212127}
 .src{font-size:10px;color:#6f6f78;border:1px solid var(--line);border-radius:3px;
   padding:0 5px;margin-left:7px;text-transform:uppercase;letter-spacing:.04em}
 .src.compositor{color:#c89b5a;border-color:#4a3a22}
 .plabel{font-size:11px;color:var(--dim);text-transform:uppercase;
   letter-spacing:.05em;margin:11px 0 3px}
 .plabel.comp{color:#c89b5a}
 .pathrow{margin-top:0}
 .scene{margin-top:9px;
   color:var(--dim);font-size:12px}
 .scene b{color:var(--text);font-weight:600}
 .vl{display:inline-block;padding:0 7px;border-radius:999px;margin-right:5px;
   border:1px solid var(--line);font-size:11px}
 .vl.on{color:var(--blue);border-color:#2d4d6b}
 .vl.off{color:#5c5c64;text-decoration:line-through}
 /* Full-screen preview, in this tab: a second click puts it back. */
 #lb{position:fixed;inset:0;background:rgba(8,8,10,.94);display:none;
   align-items:center;justify-content:center;z-index:50;cursor:zoom-out}
 #lb.on{display:flex}
 #lbwrap{position:relative;display:inline-block;line-height:0}
 #lb img{max-width:96vw;max-height:88vh;image-rendering:auto;
   border:1px solid var(--line);border-radius:4px;background:#000}
 /* Discrete, NOT a dimming: the picture stays at full strength so an old frame
    can still be compared properly against a new one. */
 #lbold{position:absolute;top:8px;right:8px;display:none;font:600 11px system-ui;
   letter-spacing:.06em;color:#e3c48a;background:rgba(20,16,8,.82);
   border:1px solid #5a4526;border-radius:3px;padding:2px 7px;line-height:15px}
 #lbold.on{display:block}
 #lbname{position:fixed;bottom:14px;left:0;right:0;text-align:center;
   color:var(--dim);font:12px Consolas,monospace}
 /* one column per output location */
 .seqs{display:grid;grid-template-columns:repeat(auto-fit,minmax(310px,1fr));
   gap:12px;margin-top:12px}
 .seq{background:#141417;border:1px solid var(--line);border-radius:6px;padding:10px 11px;
   display:flex;flex-direction:column}
 /* Nothing to preview (EXR, TIFF, video): the list takes the room the contact
    sheet would have used, down to the bottom of the column, instead of leaving
    a hole beside a column of thumbnails. flex-basis 0 + min-height: it fills
    whatever height the row has and scrolls inside, never pushing it taller. */
 .seq.nopreview .files{flex:1 1 0;min-height:200px;max-height:none}
 /* the "no preview" note moves into the caption line; the empty sheet goes */
 .seq.nopreview .sheet{display:none}
 .seq.comp{border-color:#3b3326}
 .seqlabel{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--dim)}
 .seq.comp .seqlabel{color:#c89b5a}
 .seqsub{color:var(--dim);font-size:11.5px;margin-top:3px;
   font-variant-numeric:tabular-nums}
 .seq .pathrow{margin-top:7px}
 .seq code{font-size:11px;padding:2px 6px;flex:1;min-width:0;
   white-space:nowrap;overflow:hidden;text-overflow:ellipsis;direction:rtl;
   text-align:left}
 .sheet{display:grid;grid-template-columns:repeat(auto-fill,minmax(86px,1fr));
   gap:4px;margin-top:9px;max-height:260px;overflow:auto}
 .th{position:relative;cursor:zoom-in;border:1px solid var(--line);border-radius:4px;
   overflow:hidden;background:#000;aspect-ratio:16/9}
 .th img{width:100%;height:100%;object-fit:cover;display:block}
 .th:hover{border-color:var(--blue)}
 .thname{position:absolute;left:0;right:0;bottom:0;font:9px Consolas,monospace;
   color:#cfcfd6;background:rgba(0,0,0,.66);padding:1px 3px;
   white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 /* grid-column 1/-1 or the note lands in one 86px cell and wraps to four lines */
 /* A frame left over from an EARLIER render: dimmed, with a small chip. Only
    drawn when the folder holds a mix - a clean folder gets no marks at all. */
 .th.old img{opacity:.35;filter:grayscale(.7)}
 .chip{position:absolute;top:3px;right:3px;font:9px system-ui;letter-spacing:.04em;
   text-transform:uppercase;color:#d9b87a;background:rgba(20,16,8,.85);
   border:1px solid #4a3a22;border-radius:3px;padding:0 4px;line-height:14px}
 .files a.old,.files .no.old{color:#6c6c75}
 .files .chip{position:static;margin-left:7px}
 .nothumb{grid-column:1/-1;color:#6f6f78;font-size:11.5px;padding:12px 2px}
 .sheetcap{color:var(--dim);font-size:10.5px;margin-top:8px;
   font-variant-numeric:tabular-nums}
 .sheetcap:empty{display:none}
 .sheetcap + .sheet{margin-top:4px}
 .more{margin-top:9px;width:100%}
 /* overflow-anchor:none - the list refreshes itself while being read, and its
    scroll position is restored by loadList explicitly; the browser's own
    anchoring must not second-guess that. */
 .seq .files{margin-top:7px;max-height:220px;overflow-anchor:none}
 .logwrap{margin-top:12px}
 /* the flipbook */
 #lbbar{position:fixed;left:0;right:0;bottom:0;padding:10px 16px 14px;
   display:flex;align-items:center;gap:12px;background:linear-gradient(
     to top, rgba(8,8,10,.96), rgba(8,8,10,0));cursor:default}
 #lbbar button{min-width:38px;font-size:13px}
 #lbrange{flex:1;accent-color:var(--blue);cursor:pointer}
 #lbname{position:static;flex:0 0 auto;text-align:left;white-space:nowrap}
 #lbhint{color:#5c5c64;font:11px system-ui;white-space:nowrap}
 .shots a{cursor:zoom-in}
 pre{margin:10px 0 0;max-height:240px;overflow:auto;background:#0e0e10;
   border:1px solid var(--line);border-radius:5px;padding:9px;
   font:12px/1.45 Consolas,monospace;color:#b6b6bd;white-space:pre-wrap}
 .empty{color:var(--dim);padding:30px 0;text-align:center}
 .off{opacity:.55}
 footer{color:var(--dim);font-size:11px;margin-top:14px}
</style>
<div class="head">
  <div>
    <h1>__TITLE__</h1>
    <div class="sub" id="sum">connecting…</div>
    <div class="stats" id="stats"></div>
  </div>
  <div class="gpu" id="gpuBox" hidden>
    <div class="meta" id="gpuTop"></div>
    <div class="bar" id="gpuBar"></div>
    <div class="sparkwrap">
      <div class="spark" id="gpuSpark"></div>
      <i class="gl" id="glw"></i><i class="gl" id="gls"></i>
    </div>
    <div class="sparkcap"><span id="sparkWhat">history</span><span>now →</span></div>
    <div class="meta" id="gpuLegend"></div>
  </div>
</div>
<div class="scene" id="scene"></div>
<div id="list"></div>
<footer id="foot"></footer>
<div id="lb">
  <div id="lbwrap"><img id="lbimg" alt=""><span id="lbold">OLD</span></div>
  <div id="lbbar">
    <button id="lbprev" title="previous frame (left arrow)">◀</button>
    <button id="lbplay" title="play / pause (space)">▶</button>
    <button id="lbnext" title="next frame (right arrow)">▶|</button>
    <input id="lbrange" type="range" min="0" max="0" value="0">
    <div id="lbname"></div>
    <div id="lbhint">← → step · space play · esc close</div>
  </div>
</div>
<script>
/* The page is updated IN PLACE, never rebuilt.
   A queue runs for hours and this window stays open: replacing the DOM each
   second would refetch every preview image (the responses are no-store),
   drop any text being selected, and throw away the scroll position of a log
   someone is reading. So each job keeps its card, and a tick only writes the
   values that actually changed. */
const CELLS = 24;
// The only formats a browser reliably displays. TIFF and EXR are common File
// Output choices and neither renders, so they are links, not previews.
const VIEWABLE = /\.(png|jpe?g|webp|gif|bmp|avif)$/i;
// An open file list refreshes at most this often. A 2701-row rebuild measured
// well under this, and a frame rarely takes less, so it costs one refresh per
// rendered frame at most.
const LIST_EVERY_MS = 3000;
const cards = new Map();          // job name -> card record
const open  = new Set();          // which cards the user expanded
let levels = {warn:66, stop:90};

function cellColour(p, warn, stop){
  if (stop && p >= stop) return 'var(--white)';
  if (warn && p >= warn) return 'var(--orange)';
  return 'var(--blue)';
}
function dur(s){
  if (s == null) return '—';
  if (s < 60) return (s < 10 ? s.toFixed(1) : Math.round(s)) + 's';
  s = Math.round(s);
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60);
  return h ? h+'h'+String(m).padStart(2,'0')+'m' : m+'m'+String(s%60).padStart(2,'0')+'s';
}
function bytes(n){
  if (!n) return '—';
  const u = ['B','KB','MB','GB','TB']; let i = 0;
  while (n >= 1024 && i < u.length-1){ n /= 1024; i++; }
  return (n < 10 && i ? n.toFixed(1) : Math.round(n)) + ' ' + u[i];
}
function el(tag, cls, txt){
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (txt != null) e.textContent = txt;
  return e;
}
function setText(node, text){ if (node.textContent !== text) node.textContent = text; }

const lb = document.getElementById('lb');
const lbimg = document.getElementById('lbimg');
const lbname = document.getElementById('lbname');
const lbbar = document.getElementById('lbbar');
const lbprev = document.getElementById('lbprev');
const lbnext = document.getElementById('lbnext');
const lbplay = document.getElementById('lbplay');
const lbrange = document.getElementById('lbrange');
const lbold = document.getElementById('lbold');
/* ---------------------------------------------------------------------------
   The flipbook: step or play through consecutive frames WITHOUT the image
   moving, which is the only way to see continuity problems. Flicker, a light
   popping, denoiser boiling - none of it shows in a contact sheet, because the
   eye cannot compare two pictures that are side by side as well as it can
   compare two that occupy the same pixels a moment apart.
   --------------------------------------------------------------------------- */
const strip = {job:null, seq:null, names:[], old:new Set(), at:0, playing:false,
               timer:null, fps:8};

function stripSrc(name){
  return '/file?job=' + strip.job + '&n=' + encodeURIComponent(name);
}
function preload(i){
  [i-2, i-1, i+1, i+2].forEach(function(k){
    if (k >= 0 && k < strip.names.length){
      const im = new Image(); im.src = stripSrc(strip.names[k]);
    }
  });
}
function showAt(i){
  if (!strip.names.length) return;
  strip.at = Math.max(0, Math.min(strip.names.length - 1, i));
  const name = strip.names[strip.at];
  lbimg.src = stripSrc(name);
  // The corner mark follows the frame on screen, so stepping or playing across
  // the boundary shows exactly where the new frames start.
  lbold.classList.toggle('on', strip.old.has(name));
  setText(lbname, name + '      ' + (strip.at + 1) + ' / ' + strip.names.length
          + (strip.old.has(name) ? '      \u00b7 from an earlier render' : ''));
  lbrange.max = String(strip.names.length - 1);
  lbrange.value = String(strip.at);
  preload(strip.at);
}
function stripStep(d){ stopPlay(); showAt(strip.at + d); }
function startPlay(){
  if (strip.names.length < 2) return;
  strip.playing = true;
  lbplay.textContent = '❚❚';
  strip.timer = setInterval(function(){
    showAt(strip.at + 1 >= strip.names.length ? 0 : strip.at + 1);
  }, Math.round(1000 / strip.fps));
}
function stopPlay(){
  strip.playing = false;
  lbplay.textContent = '▶';
  if (strip.timer){ clearInterval(strip.timer); strip.timer = null; }
}
function togglePlay(){ strip.playing ? stopPlay() : startPlay(); }

function openStrip(j, sq, name, known){
  strip.job = j.i; strip.seq = sq.i;
  const start = function(list){
    const usable = list.filter(function(f){
      return !f.writing && VIEWABLE.test(f.name);
    });
    strip.names = usable.map(function(f){ return f.name; });
    // Only worth flagging when the sequence is a MIX; if every frame is old (the
    // job has not written yet) or none is, the label would say nothing useful.
    const stale = usable.filter(function(f){ return f.new === false; });
    strip.old = (stale.length && stale.length < usable.length)
      ? new Set(stale.map(function(f){ return f.name; })) : new Set();
    if (!strip.names.length) strip.names = [name];
    const idx = strip.names.indexOf(name);
    lb.classList.add('on');
    showAt(idx < 0 ? strip.names.length - 1 : idx);
  };
  if (known && known.length) { start(known); return; }
  // Nothing fetched yet: pull the sequence so stepping covers every frame on
  // disk, not just the handful in the contact sheet.
  const fin = (j.state === 'done' || j.state === 'failed') ? '1' : '0';
  lb.classList.add('on');
  setText(lbname, 'loading the sequence…');
  fetch('/api/files?job=' + j.i + '&seq=' + sq.i + '&finished=' + fin,
        {cache:'no-store'})
    .then(function(r){ return r.json(); })
    .then(function(d){ start(d.files || []); })
    .catch(function(){ start([{name:name, writing:false}]); });
}

function hideBig(){
  stopPlay();
  lb.classList.remove('on');
  lbimg.src = '';
  lbold.classList.remove('on');
  strip.names = [];
}

lbprev.onclick = function(e){ e.stopPropagation(); stripStep(-1); };
lbnext.onclick = function(e){ e.stopPropagation(); stripStep(1); };
lbplay.onclick = function(e){ e.stopPropagation(); togglePlay(); };
lbrange.oninput = function(e){ e.stopPropagation(); stopPlay(); showAt(+lbrange.value); };
lbrange.onclick = function(e){ e.stopPropagation(); };
lbbar.onclick = function(e){ e.stopPropagation(); };
lbimg.onclick = function(e){ e.stopPropagation(); hideBig(); };
lb.onclick = hideBig;
document.addEventListener('keydown', function(e){
  if (!lb.classList.contains('on')) return;
  if (e.key === 'Escape'){ hideBig(); }
  else if (e.key === 'ArrowLeft'){ e.preventDefault(); stripStep(-1); }
  else if (e.key === 'ArrowRight'){ e.preventDefault(); stripStep(1); }
  else if (e.key === ' '){ e.preventDefault(); togglePlay(); }
  else if (e.key === 'Home'){ stopPlay(); showAt(0); }
  else if (e.key === 'End'){ stopPlay(); showAt(strip.names.length - 1); }
});


function paintHead(s){
  const q = s.queue;
  setText(document.getElementById('sum'),
    q.running + ' rendering · ' + q.pending + ' waiting · ' + q.finished + ' finished'
    + (q.failed ? ' (' + q.failed + ' failed)' : '')
    + '   (up to ' + q.parallel + ' at a time)');
  const want = [];
  if (q.frames_total) want.push(['frames', q.frames_done + ' / ' + q.frames_total]);
  if (q.elapsed_s != null) want.push(['elapsed', dur(q.elapsed_s)]);
  if (q.out_count) want.push(['written', q.out_count + ' files']);
  if (q.out_bytes) want.push(['on disk', bytes(q.out_bytes)]);
  const box = document.getElementById('stats');
  if (box.children.length !== want.length){
    box.innerHTML = '';
    want.forEach(function(){
      const d = el('div','stat'); d.appendChild(el('b')); d.appendChild(el('span'));
      box.appendChild(d);
    });
  }
  want.forEach(function(pair, i){
    setText(box.children[i].querySelector('b'), pair[1]);
    setText(box.children[i].querySelector('span'), pair[0]);
  });
}

function paintGpu(g){
  const box = document.getElementById('gpuBox');
  if (!g){ box.hidden = true; return; }
  box.hidden = false;
  setText(document.getElementById('gpuTop'),
    'VRAM  ' + Math.round(g.pct) + '%   ' + g.used_gb.toFixed(1) + ' / ' +
    g.total_gb.toFixed(1) + ' GB   ' + g.name);
  const bar = document.getElementById('gpuBar');
  if (bar.children.length !== CELLS){
    bar.innerHTML = '';
    for (let i=0;i<CELLS;i++) bar.appendChild(document.createElement('i'));
  }
  const filled = Math.round(CELLS * Math.min(Math.max(g.pct,0),100) / 100);
  for (let i=0;i<CELLS;i++){
    const mid = (i+0.5)*100/CELLS;
    const col = i < filled ? cellColour(mid, levels.warn, levels.stop) : '#26262c';
    if (bar.children[i].style.background !== col) bar.children[i].style.background = col;
  }
  const hist = (g.history||[]).slice(-90);
  const sp = document.getElementById('gpuSpark');
  while (sp.children.length < hist.length) sp.appendChild(document.createElement('i'));
  while (sp.children.length > hist.length) sp.removeChild(sp.lastChild);
  hist.forEach(function(p, i){
    sp.children[i].style.height = Math.max(1, Math.round(p)) + '%';
    sp.children[i].style.background = cellColour(p, levels.warn, levels.stop);
  });
  // Position the dashed guide lines; bar heights are % of the same box, so a
  // bar crossing a line really is crossing that threshold.
  [['glw', levels.warn], ['gls', levels.stop]].forEach(function(pair){
    const gl = document.getElementById(pair[0]);
    gl.style.display = pair[1] ? '' : 'none';
    if (pair[1]) gl.style.bottom = pair[1] + '%';
  });
  const secs = hist.length * (g.interval_s || 0);
  const span = !g.interval_s ? 'history'
    : secs < 90 ? 'last ' + Math.round(secs) + ' s'
    : 'last ' + (secs / 60).toFixed(secs < 600 ? 1 : 0) + ' min';
  setText(document.getElementById('sparkWhat'),
          'VRAM over time · ' + span + (g.interval_s ? ' · a bar every '
          + Math.round(g.interval_s) + ' s' : ''));
  const parts = [];
  if (levels.warn) parts.push('alarm ' + levels.warn + '%');
  if (levels.stop) parts.push('stops a render above ' + levels.stop + '%');
  setText(document.getElementById('gpuLegend'), parts.join('  ·  '));
}

function buildCard(j){
  const e = el('div','card');
  const top = el('div','top');
  const row = el('div','row');
  const left = el('span');
  const nm = el('span','nm', j.name);
  const tag = el('span','tag');
  const sctag = el('span','sctag');
  left.appendChild(nm); left.append(' '); left.appendChild(tag); left.appendChild(sctag);
  const meta = el('span','meta');
  row.appendChild(left); row.appendChild(meta);
  const track = el('div','track');
  const fill = el('div','fill');
  track.appendChild(fill);
  const note = el('div','note'); note.hidden = true;
  top.appendChild(row); top.appendChild(track); top.appendChild(note);
  e.appendChild(top);
  const body = el('div','body');
  const grid = el('div','grid');
  body.appendChild(grid);
  e.appendChild(body);
  top.onclick = function(ev){
    if (ev.target.tagName === 'BUTTON' || ev.target.tagName === 'A') return;
    if (open.has(j.i)) open.delete(j.i); else open.add(j.i);
    e.classList.toggle('open');
  };
  const rec = {el:e, tag:tag, sctag:sctag, meta:meta, fill:fill, note:note, grid:grid, body:body,
               lastFiles:-1, logAt:0, files:null, shots:null, pre:null, pathrow:null};
  cards.set(j.i, rec);
  return rec;
}

function paintGrid(rec, j){
  const want = [
    ['frames', j.first_frame != null ? j.first_frame + ' – ' + j.last_frame : '—'],
    ['per frame', dur(j.avg_s)],
    ['fastest', dur(j.fast_s)],
    ['slowest', dur(j.slow_s)],
    ['elapsed', dur(j.elapsed_s)],
    ['files written', j.out_count ? j.out_count + '  (' + bytes(j.out_bytes) + ')' : '—']
  ];
  if (j.problems) want.push(['render problems', j.problems + ' (' + j.problem_kinds + ' kinds)']);
  if (j.noise) want.push(['add-on noise', String(j.noise)]);
  if (rec.grid.children.length !== want.length){
    rec.grid.innerHTML = '';
    want.forEach(function(){
      const d = el('div','g'); d.appendChild(el('b')); d.appendChild(el('span'));
      rec.grid.appendChild(d);
    });
  }
  want.forEach(function(pair, i){
    setText(rec.grid.children[i].querySelector('b'), pair[1]);
    setText(rec.grid.children[i].querySelector('span'), pair[0]);
  });
}

function paintBody(rec, j){
  paintGrid(rec, j);
  const seqs = j.sequences || [];
  if (!rec.seqWrap){
    rec.seqWrap = el('div','seqs');
    rec.body.appendChild(rec.seqWrap);
    rec.seqCards = new Map();
  }
  // One column per output location. Columns are created once and thenupdated,
  // so a contact sheet does not reload while you are looking at it.
  seqs.forEach(function(sq){
    let c = rec.seqCards.get(sq.i);
    if (!c){
      c = buildSeq(j, sq);
      rec.seqCards.set(sq.i, c);
      rec.seqWrap.appendChild(c.el);
    }
    paintSeq(c, j, sq);
  });
  if (j.has_log){
    if (!rec.pre){
      rec.logWrap = el('div','logwrap');
      rec.logWrap.appendChild(el('div','plabel','Render log'));
      rec.pre = el('pre', null, 'loading log…');
      rec.logWrap.appendChild(rec.pre);
      rec.body.appendChild(rec.logWrap);
    }
    const now = Date.now();
    if (now - rec.logAt > 3000){
      rec.logAt = now;
      // Follow the tail only if the reader is already at the bottom, so
      // scrolling up to read something is not undone a second later.
      const stick = rec.pre.scrollTop + rec.pre.clientHeight >= rec.pre.scrollHeight - 24;
      fetch('/api/log?job=' + j.i, {cache:'no-store'}).then(function(r){ return r.text(); })
        .then(function(t){
          setText(rec.pre, t || '(log is empty)');
          if (stick) rec.pre.scrollTop = rec.pre.scrollHeight;
        }).catch(function(){});
    }
  }
}

function buildSeq(j, sq){
  const e = el('div','seq' + (sq.kind === 'compositor' ? ' comp' : ''));
  const head = el('div','seqhead');
  const title = el('div','seqtitle');
  title.appendChild(el('span','seqlabel', sq.kind === 'compositor'
    ? 'Compositor · ' + sq.label : sq.label));
  head.appendChild(title);
  const sub = el('div','seqsub');
  head.appendChild(sub);
  const pathrow = el('div','pathrow');
  const code = el('code');
  const cp = el('button', null, 'Copy');
  cp.onclick = function(ev){
    ev.stopPropagation();
    navigator.clipboard.writeText(sq.dir).then(function(){
      cp.textContent = 'Copied';
      setTimeout(function(){ cp.textContent = 'Copy'; }, 1200);
    });
  };
  pathrow.appendChild(code); pathrow.appendChild(cp);
  head.appendChild(pathrow);
  e.appendChild(head);
  const sheetcap = el('div','sheetcap');
  e.appendChild(sheetcap);
  const sheet = el('div','sheet');
  e.appendChild(sheet);
  const more = el('button','more','Show every file');
  e.appendChild(more);
  const list = el('div','files');
  list.hidden = true;
  e.appendChild(list);
  more.onclick = function(ev){
    ev.stopPropagation();
    showList(c, list.hidden);
  };
  // lastThumbs starts as null, NOT '': a sequence with no previewable files
  // joins to '' too, and an initial '' would compare equal - so the "no
  // preview for .exr" note would never be drawn at all.
  // c.j / c.sq are the LATEST job and sequence (paintSeq refreshes them). The
  // button used to keep the ones from when the column was built, so a list
  // opened after the job finished still asked for it as running, and its
  // finished video stayed unopenable.
  const c = {el:e, sub:sub, code:code, sheet:sheet, sheetcap:sheetcap, list:list,
             more:more, lastSig:null, lastThumbs:null, wantList:false, files:[],
             loading:false, loadedAt:0, j:j, sq:sq, focusNext:false,
             autoOpened:false};
  return c;
}

function showList(c, on){
  c.list.hidden = !on;
  c.more.textContent = on ? 'Hide the list' : 'Show every file';
  // A hidden list stops refreshing; it reloads fresh when shown again.
  c.wantList = on;
  if (on){
    c.rows = null;
    c.focusNext = true;                 // open where the render is
    c.lastSig = c.sq.sig;
    loadList(c, c.j, c.sq);
  }
}

// Frame number at the end of a file name, as Blender writes it.
function frameOf(name){
  const m = /(\d+)(\.[^.]*)?$/.exec(name);
  return m ? parseInt(m[1], 10) : null;
}

// Where "Show every file" opens: the last frame this run has written (the one
// being rendered is right above it - the list is newest first); before the
// job has written anything, its first frame; otherwise the top. On a list of
// thousands of frames, scrolling there by hand was the tedious part.
function focusList(c, j, files){
  let at = -1;
  for (let i = files.length - 1; i >= 0; i--){
    if (files[i].new === true){ at = i; break; }
  }
  if (at < 0 && j.first_frame != null){
    at = files.findIndex(function(f){ return frameOf(f.name) === j.first_frame; });
  }
  const row = at >= 0 ? c.rows[at] : null;
  if (!row) return;
  const list = c.list;
  list.scrollTop += row.getBoundingClientRect().top - list.getBoundingClientRect().top
                  - (list.clientHeight - row.offsetHeight) / 2;
  row.classList.add('focus');
  setTimeout(function(){ row.classList.remove('focus'); }, 1800);
}

function paintSeq(c, j, sq){
  c.j = j; c.sq = sq;
  const nopreview = !!sq.count && !sq.viewable;
  setText(c.code, sq.dir);
  const bits = [];
  bits.push(sq.count + (sq.count === 1 ? ' file' : ' files'));
  if (sq.bytes) bits.push(bytes(sq.bytes));
  if (sq.exts && sq.exts.length) bits.push(sq.exts.join(' '));
  // Only when the folder held files from before this run: otherwise every file
  // is new and saying so on every column would just be noise.
  if (sq.old) bits.push(sq.new + ' new \u00b7 ' + sq.old + ' from an earlier render');
  if (sq.first && sq.last && sq.count > 1) bits.push(sq.first + ' → ' + sq.last);
  else if (sq.first && sq.count === 1) bits.push(sq.first);
  setText(c.sub, bits.join('   ·   '));

  // The contact sheet: rebuilt only when the thumbnails actually change, so an
  // open card does not refetch its images once a second.
  // The key includes each thumbnail flag and mtime: a frame that gets
  // overwritten flips from old to new, and must be refetched - the same name
  // alone would leave the stale picture on screen.
  // Say which stretch the contact sheet shows, so it is never mistaken for
  // the end of the folder when it is really the job's own frames.
  setText(c.sheetcap, nopreview
    ? 'no preview for ' + (sq.exts || []).join(' ') + ' — every file is listed below'
    : sq.span
    ? 'frames ' + sq.span[0] + ' – ' + sq.span[1] + '  ·  of this job\u2019s '
      + j.first_frame + ' – ' + j.last_frame
    : (sq.thumbs && sq.thumbs.length ? 'the last ' + sq.thumbs.length + ' files' : ''));
  const key = (sq.thumbs || []).map(function(t){
    return t.n + ':' + t.v + ':' + t.new;
  }).join('|');
  if (key !== c.lastThumbs){
    c.lastThumbs = key;
    // Tiles are REUSED when their file has not changed. Rebuilding all 48
    // refetched every picture each time one frame landed - with the sheet now
    // following the render, that blanked the whole sheet once per frame.
    const prev = c.tiles || new Map();
    const next = new Map();
    const frag = document.createDocumentFragment();
    if (!sq.thumbs || !sq.thumbs.length){
      const why = sq.count
        ? (sq.writing ? 'still being written — no preview until it finishes'
                      : 'no preview for ' + (sq.exts || []).join(' ') +
                        ' — every file is listed below')
        : 'nothing written yet';
      frag.appendChild(el('div','nothumb', why));
    } else {
      sq.thumbs.forEach(function(th){
        const name = th.n;
        const stale = !!sq.old && th.new === false;
        const k = name + ':' + th.v + ':' + stale;
        let t = prev.get(k);
        if (!t || t.dead){
          // &v= is the file mtime: a new value is a new URL, so an overwritten
          // frame is never served from the browser cache.
          const src = '/file?job=' + j.i + '&n=' + encodeURIComponent(name) + '&v=' + th.v;
          t = el('div', 'th' + (stale ? ' old' : ''));
          const im = document.createElement('img');
          const tile = t;
          // dead: never reuse a tile whose picture failed to load
          im.onerror = function(){ tile.dead = true; tile.remove(); };
          im.src = src; im.loading = 'lazy'; im.alt = name;
          t.appendChild(im);
          if (stale) t.appendChild(el('span','chip','old'));
          t.appendChild(el('span','thname', name));
          t.onclick = function(ev){
            ev.stopPropagation();
            // Open the flipbook on the WHOLE sequence, not just the strip, so
            // stepping runs across every frame on disk.
            openStrip(j, sq, name);
          };
        }
        if (!t.dead){ next.set(k, t); frag.appendChild(t); }
      });
    }
    c.sheet.innerHTML = '';
    c.sheet.appendChild(frag);
    c.tiles = next;
  }
  c.el.classList.toggle('nopreview', nopreview);
  // Opened ONCE for the reader; hiding it afterwards is respected.
  if (nopreview && !c.autoOpened && c.list.hidden){
    c.autoOpened = true;
    showList(c, true);
  }
  // Refresh an open list whenever its files change - including frames being
  // overwritten in place, which leaves the count alone. At most one request in
  // flight and one every LIST_EVERY_MS: a deferred refresh is not lost, the
  // signature still differs on the next tick and it runs then.
  if (c.wantList && sq.sig !== c.lastSig && !c.loading
      && Date.now() - c.loadedAt >= LIST_EVERY_MS){
    // Never rebuild under a selection the reader is making in the list.
    const sel = window.getSelection && window.getSelection();
    if (!(sel && !sel.isCollapsed && c.list.contains(sel.anchorNode))){
      c.lastSig = sq.sig;
      loadList(c, j, sq);
    }
  }
}

// The row at the top of the list's view, and how far it is scrolled past, so
// a rebuild can put the same file back in the same place.
function listAnchor(list){
  if (!list.scrollTop) return null;          // at the top: stay at the top
  const top = list.getBoundingClientRect().top;
  const rows = list.children;
  for (let i = 0; i < rows.length; i++){
    const r = rows[i].getBoundingClientRect();
    if (r.bottom > top) return {name: rows[i].dataset.n, off: r.top - top};
  }
  return null;
}

// One row of the file list. Its key changes exactly when the row would look
// different, so a refresh can tell which rows need replacing.
function rowKey(f, anyOld){
  return f.mt + ':' + f.size + ':' + (anyOld && f.new === false) + ':' + f.writing;
}

function fileRow(c, j, sq, f, anyOld){
  const label = el('span', null, f.name);
  const stale = anyOld && f.new === false;
  if (stale) label.appendChild(el('span','chip','earlier render'));
  if (f.writing){
    const row = el('div','no');
    row.dataset.n = f.name;
    row.appendChild(label);
    row.appendChild(el('span', null, bytes(f.size) + '  ·  still writing'));
    return row;
  }
  const a = document.createElement('a');
  if (stale) a.className = 'old';
  a.href = '/file?job=' + j.i + '&n=' + encodeURIComponent(f.name) + '&v=' + f.mt;
  if (VIEWABLE.test(f.name)){
    a.onclick = function(ev){ ev.preventDefault(); ev.stopPropagation();
                              openStrip(j, sq, f.name, c.files); };
  } else {
    // EXR, DPX, mp4 ... the browser cannot draw them; let it decide
    // whether to show or download.
    a.target = '_blank';
  }
  a.dataset.n = f.name;
  a.appendChild(label);
  a.appendChild(el('span', null, bytes(f.size)));
  return a;
}

function loadList(c, j, sq){
  const fin = (j.state === 'done' || j.state === 'failed') ? '1' : '0';
  c.loading = true;
  fetch('/api/files?job=' + j.i + '&seq=' + sq.i + '&finished=' + fin,
        {cache:'no-store'})
    .then(function(r){ return r.json(); })
    .then(function(d){
      if (c.list.hidden) return;          // closed while the request was out
      const files = d.files || [];
      const anyOld = files.some(function(f){ return f.new === false; });
      const keys = files.map(function(f){ return rowKey(f, anyOld); });
      const capped = d.count > d.shown;
      // Re-rendering over existing frames keeps every name: then only the rows
      // that changed are replaced, in place. Rebuilding all 3181 rows measured
      // 70-100 ms of blocked page per refresh; a same-height row swapped in
      // place also cannot move the reader's scroll position.
      const same = c.rows && c.rows.length === files.length && c.capped === capped
        && files.every(function(f, i){ return f.name === c.rowNames[i]; });
      c.files = files;
      let patched = false;
      if (same){
        // Any surprise (a row no longer in the list) falls through to the full
        // rebuild below, never to a half-patched or stale list.
        try {
          for (let i = 0; i < files.length; i++){
            if (keys[i] === c.rowKeys[i]) continue;
            const row = fileRow(c, j, sq, files[i], anyOld);
            c.list.replaceChild(row, c.rows[i]);
            c.rows[i] = row;
          }
          patched = true;
        } catch (e) {}
      }
      if (!patched){
        const anchor = listAnchor(c.list);
        const keepTop = c.list.scrollTop;
        c.list.innerHTML = '';
        if (capped)
          c.list.appendChild(el('div','no', 'showing the newest ' + d.shown +
                                            ' of ' + d.count));
        c.rows = files.map(function(f){ return fileRow(c, j, sq, f, anyOld); });
        // newest first on screen; c.rows stays in name order, matching `files`
        for (let i = c.rows.length - 1; i >= 0; i--) c.list.appendChild(c.rows[i]);
        // Put the reader back on the file they were looking at. New files land
        // at the top (newest first), so a plain scrollTop would drift by a row
        // per frame rendered.
        let back = null;
        if (c.focusNext){
          // just opened: go to the render, not to where an old list was left
          c.focusNext = false;
          c.list.scrollTop = 0;
          focusList(c, j, files);
        } else {
          if (anchor){
            const at = files.findIndex(function(f){ return f.name === anchor.name; });
            back = at >= 0 ? c.rows[at] : null;
          }
          if (back){
            c.list.scrollTop += back.getBoundingClientRect().top
                              - c.list.getBoundingClientRect().top - anchor.off;
          } else if (anchor){
            c.list.scrollTop = keepTop;
          }
        }
      }
      c.rowNames = files.map(function(f){ return f.name; });
      c.rowKeys = keys;
      c.capped = capped;
    }).catch(function(){})
    .then(function(){ c.loading = false; c.loadedAt = Date.now(); });
}


function paintCard(j){
  const rec = cards.get(j.i) || buildCard(j);
  const cls = 'card ' + j.state + (j.state === 'pending' ? ' off' : '')
            + (open.has(j.i) ? ' open' : '');
  if (rec.el.className !== cls) rec.el.className = cls;
  setText(rec.tag, j.state + (j.alone ? ' · alone' : ''));
  rec.tag.className = 'tag t-' + j.state;
  const right = [];
  if (j.total) right.push(j.done + ' / ' + j.total + ' frames');
  if (j.frame != null) right.push('frame ' + j.frame);
  if (j.avg_s) right.push(dur(j.avg_s) + '/frame');
  if (j.eta_s != null) right.push('~' + dur(j.eta_s) + ' left');
  if (j.state === 'done') right.push('took ' + dur(j.elapsed_s));
  if (j.state === 'failed') right.push('exit ' + j.code);
  setText(rec.meta, right.join('   ·   '));
  const w = (j.state === 'done' || j.state === 'failed' ? 100 : j.pct) + '%';
  if (rec.fill.style.width !== w) rec.fill.style.width = w;
  rec.note.hidden = !j.note;
  if (j.note) setText(rec.note, j.note);
  // which scene, once the queue holds more than one
  setText(rec.sctag, multiScene && j.scene && j.scene.name ? j.scene.name : '');
  if (open.has(j.i)) paintBody(rec, j);
  return rec.el;
}

let multiScene = false;

// The header names what is being rendered. Once a launch from another scene
// joins the queue there is one line per scene, each led by its name, and every
// card says which scene it belongs to.
function paintScenes(s){
  const box = document.getElementById('scene');
  const byName = new Map();
  (s.jobs || []).forEach(function(j){
    if (j.scene) byName.set(j.scene.name || '', j.scene);
  });
  if (!byName.size && s.scene) byName.set(s.scene.name || '', s.scene);
  const scenes = Array.from(byName.values());
  multiScene = scenes.length > 1;
  const key = JSON.stringify(scenes);
  if (box.dataset.key === key) return;        // unchanged: leave it alone
  box.dataset.key = key;
  box.innerHTML = '';
  scenes.forEach(function(sc){
    const row = el('div','scenerow');
    const bit = function(label, value){
      const d = el('div');
      d.appendChild(el('b', null, value));
      d.append(' ' + label);
      row.appendChild(d);
    };
    if (multiScene && sc.name) row.appendChild(el('div','scname', sc.name));
    bit('', sc.engine);
    bit('px', sc.res + (sc.res_pct !== 100 ? '  (' + sc.res_pct + '%)' : ''));
    if (sc.samples) bit('samples', String(sc.samples));
    bit('', sc.format + (sc.depth ? '  ' + sc.depth + '-bit' : ''));
    if (sc.step > 1) bit('frame step', String(sc.step));
    if (sc.save_output === false) bit('', 'Save Output OFF');
    const vls = sc.view_layers || [];
    if (vls.length){
      const d = el('div');
      d.append((vls.length > 1 ? 'view layers ' : 'view layer '));
      vls.forEach(function(v){
        d.appendChild(el('span', 'vl ' + (v.on ? 'on' : 'off'), v.name));
      });
      row.appendChild(d);
    }
    box.appendChild(row);
  });
}

async function tick(){
  let s;
  try { s = await (await fetch('/api/state', {cache:'no-store'})).json(); }
  catch (e){
    setText(document.getElementById('sum'),
      'the queue has finished — this window is no longer updating');
    return;
  }
  if (s.error){
    setText(document.getElementById('sum'), s.error);
  } else {
    levels = s.levels || levels;
    paintHead(s);
    paintScenes(s);
    paintGpu(s.gpu);
    const list = document.getElementById('list');
    const seen = new Set();
    s.jobs.forEach(function(j, pos){
      seen.add(j.i);
      const node = paintCard(j);
      if (list.children[pos] !== node) list.insertBefore(node, list.children[pos] || null);
    });
    Array.from(cards.keys()).forEach(function(uid){
      if (!seen.has(uid)){ cards.get(uid).el.remove(); cards.delete(uid); }
    });
    setText(document.getElementById('foot'), s.queue.all_done
      ? 'all jobs finished — you can close this window'
      : 'updating every second · click a job for details');
  }
  setTimeout(tick, __POLL__);
}
tick();
</script>
"""
