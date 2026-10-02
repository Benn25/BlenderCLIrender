"""Pure job-building logic: output names, command lines, validation.

No bpy imports, so it is tested with plain Python (tests/test_jobs.py).
Every rule here was checked against real command-line renders in Blender
5.2.2 - see the notes on each function.
"""
import hashlib
import json
import os
import tempfile
import time

# Blender 5.x image_settings.media_type value for video output.
VIDEO_MEDIA = 'VIDEO'
# Pre-5.0 file_format values that mean "video".
LEGACY_VIDEO_FORMATS = {'FFMPEG', 'AVI_JPEG', 'AVI_RAW'}

# A name ending with one of these already separates the frame number.
SEPARATORS = ("_", "-", ".", " ")


def is_video_output(media_type, file_format):
    """True when the scene renders to a video file rather than images.

    Decided from the output format, never from the base name: nobody types
    "shot.mp4" as a base name, so the old name-based check never fired.
    """
    return media_type == VIDEO_MEDIA or file_format in LEGACY_VIDEO_FORMATS


def range_error(start, end, label=""):
    """Error text for an invalid range, or None.

    Only a backwards range is invalid: start == end is a single frame, and
    `-s 5 -e 5 -a` renders exactly that frame.
    """
    if start > end:
        where = " in SubScene '%s'" % label if label else ""
        return "Start frame (%d) is after end frame (%d)%s" % (start, end, where)
    return None


def output_name(base, timestamp, include_range, start, end, is_video):
    """File name pattern given to -o (no folder).

    Images: Blender appends the frame number right after the name, so a
    separator is added when the name does not end with one - otherwise
    "shot_0121-0240" + frame 121 came out as "shot_0121-02400121.png".

    Video: Blender appends the frame range itself ("shot_0001-0003.mp4"),
    so the add-on never adds it - it used to come out doubled.

    A '#' typed in the base name means the user placed the frame number
    themselves; nothing is appended after it then.
    """
    name = base + timestamp
    if is_video:
        return name
    if include_range:
        # separate the range from the name too: "a" + "0001-0002" read as
        # "a0001-0002", i.e. the same glueing problem on the other side
        if name and not name.endswith(SEPARATORS):
            name += "_"
        name += "%04d-%04d" % (start, end)
    if "#" not in name and not name.endswith(SEPARATORS):
        name += "_"
    return name


def render_command(blender, blend, start, end, output_path=None, scene=None):
    """Command line for one render job.

    `output_path` is None when the scene's Save Output is off: -o is then
    left out entirely (the main render writes nothing either way - tested -
    and only the compositor's File Output nodes write files).

    `scene` is passed as -S. Without it Blender renders whichever scene was
    active when the file was SAVED, not the one the button was pressed in - so
    launching from a second scene rendered the first one. -S must come after
    the .blend and before -s/-e/-a.
    """
    cmd = [blender, "-b", blend]
    if scene:
        cmd += ["-S", scene]
    cmd += ["-s", str(start), "-e", str(end)]
    if output_path is not None:
        cmd += ["-o", output_path]
    cmd.append("-a")
    return cmd


def snapshot_path(blend_path, stamp):
    """Where the render snapshot is saved: next to the .blend, so every
    relative path (//textures, //renders) resolves exactly as in the
    original file."""
    folder, fname = os.path.split(blend_path)
    stem = os.path.splitext(fname)[0]
    return os.path.join(folder, "%s.cli_snapshot_%s.blend" % (stem, stamp))


# --------------------------------------------------------------------------
# One queue per .blend: a later launch joins the running one
# --------------------------------------------------------------------------
#
# The running queue advertises itself in a small registry file named after the
# .blend, and takes hand-overs from an inbox FOLDER. Never over HTTP: the
# queue window must stay read-only, or any web page open in the browser could
# post a render command to it.
#
# Hand-over is race-free by renaming. Blender drops `<x>.json` in the inbox;
# the queue claims it by renaming it to `.taken` and answers with `<x>.ok`. If nothing claims it within
# the timeout, Blender withdraws it by renaming it to `.withdrawn` and starts a
# queue of its own. A rename succeeds for exactly one side, so a set of jobs
# can be neither lost nor rendered twice - even when the queue is finishing at
# that very moment (it stops advertising first, then claims what is left).

REGISTRY_DIR = os.path.join(tempfile.gettempdir(), "cli_render_queues")
# The queue touches its registry file every HEARTBEAT_S; an older file is a
# queue that died without cleaning up, and is ignored.
HEARTBEAT_S = 2.0
STALE_S = 10.0
OFFER_TIMEOUT_S = 4.0


def registry_path(blend_path):
    """Where the queue for this .blend advertises itself.

    Keyed on the ORIGINAL .blend, never the render snapshot (a new file every
    launch). normcase: on Windows "D:/A.blend" and "d:/a.blend" are one file.
    """
    key = os.path.normcase(os.path.abspath(blend_path))
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return os.path.join(REGISTRY_DIR, digest + ".json")


def read_registry(blend_path, now=None):
    """The live queue's registry entry for this .blend, or None."""
    path = registry_path(blend_path)
    try:
        age = (now or time.time()) - os.path.getmtime(path)
        if age > STALE_S:
            return None
        with open(path, encoding="utf-8-sig") as fh:
            entry = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(entry, dict) or not os.path.isdir(entry.get("inbox") or ""):
        return None
    return entry


def offer_to_queue(blend_path, batch, timeout=OFFER_TIMEOUT_S, sleep=time.sleep):
    """Hand `batch` to the queue already running for this .blend.

    Returns that queue's registry entry when it took the jobs, None when there
    is no live queue or it did not claim them in time (the caller then starts
    a queue of its own, exactly as before).
    """
    entry = read_registry(blend_path)
    if entry is None:
        return None
    inbox = entry["inbox"]
    name = "%d_%d" % (time.time() * 1000, os.getpid())
    part = os.path.join(inbox, name + ".part")
    offer = os.path.join(inbox, name + ".json")
    try:
        with open(part, "w", encoding="utf-8") as fh:
            json.dump(batch, fh, ensure_ascii=False)
        os.replace(part, offer)             # appears complete, never half-written
    except OSError:
        try:
            os.remove(part)
        except OSError:
            pass
        return None
    ack = os.path.join(inbox, name + ".ok")

    def acked(seconds):
        waited = 0.0
        while waited < seconds:
            if os.path.exists(ack):
                try:
                    os.remove(ack)
                except OSError:
                    pass
                return True
            sleep(0.1)
            waited += 0.1
        return False

    # Only an ACK counts as taken. The offer merely vanishing proves nothing:
    # a queue that died has its temp folder deleted, inbox and all, and
    # reading that as "taken" would silently drop the renders.
    if acked(timeout):
        return entry
    try:
        os.replace(offer, offer[:-5] + ".withdrawn")
    except FileNotFoundError:
        # Claimed in the last instant, or the inbox is gone: the ack decides.
        return entry if acked(2.0) else None
    except OSError:
        return None
    try:
        os.remove(offer[:-5] + ".withdrawn")
    except OSError:
        pass
    return None
