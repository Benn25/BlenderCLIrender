"""Pure job-building logic: output names, command lines, validation.

No bpy imports, so it is tested with plain Python (tests/test_jobs.py).
Every rule here was checked against real command-line renders in Blender
5.2.2 - see the notes on each function.
"""
import os

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


def render_command(blender, blend, start, end, output_path=None):
    """Command line for one render job.

    `output_path` is None when the scene's Save Output is off: -o is then
    left out entirely (the main render writes nothing either way - tested -
    and only the compositor's File Output nodes write files).
    """
    cmd = [blender, "-b", blend, "-s", str(start), "-e", str(end)]
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
