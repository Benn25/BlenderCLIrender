"""Checks for jobs.py - plain Python, no Blender needed.

    python tests/test_jobs.py

The expected names/commands were confirmed against real command-line
renders in Blender 5.2.2 (see the V5.3 changelog in the conversation).
"""
import glob
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, (sorted(glob.glob(os.path.join(HERE, "..", "V*"))) or [os.path.join(HERE, "..", "cli_render_launcher")])[-1])

import jobs  # noqa: E402

FAILS = []


def check(label, got, want):
    ok = got == want
    print("  %s  %-52s %r" % ("PASS" if ok else "FAIL", label, got))
    if not ok:
        FAILS.append(label)
        print("        want %r" % (want,))


print("Frame ranges")
check("single frame is valid (start == end)", jobs.range_error(5, 5), None)
check("normal range valid", jobs.range_error(1, 100), None)
check("backwards range rejected", jobs.range_error(10, 5) is not None, True)
check("error names the SubScene", "closeup" in jobs.range_error(10, 5, "closeup"), True)

print("\nImage names (Blender appends the frame number)")
check("range no longer glued to frame number",
      jobs.output_name("closeup", "_09-28_14-05__", True, 121, 240, False),
      "closeup_09-28_14-05__0121-0240_")
check("range separated from a bare base name too",
      jobs.output_name("a", "", True, 1, 2, False), "a_0001-0002_")
check("timestamp already ends with a separator",
      jobs.output_name("closeup", "_14-05__", False, 1, 2, False), "closeup_14-05__")
check("bare base name gets a separator",
      jobs.output_name("closeup", "", False, 1, 2, False), "closeup_")
check("user's own separator kept", jobs.output_name("shot-", "", False, 1, 2, False), "shot-")
check("user-placed # left alone", jobs.output_name("shot_####_x", "", False, 1, 2, False),
      "shot_####_x")

print("\nVideo names (Blender appends the range itself)")
check("video: no range added", jobs.output_name("shot_", "", True, 1, 3, True), "shot_")
check("video detected from media_type", jobs.is_video_output('VIDEO', 'FFMPEG'), True)
check("images are not video", jobs.is_video_output('IMAGE', 'PNG'), False)
check("pre-5.0 FFMPEG format is video", jobs.is_video_output(None, 'FFMPEG'), True)

print("\nCommand lines")
check("with output", jobs.render_command("B", "f.blend", 1, 3, "o/x_"),
      ["B", "-b", "f.blend", "-s", "1", "-e", "3", "-o", "o/x_", "-a"])
check("Save Output off: no -o at all", jobs.render_command("B", "f.blend", 1, 3, None),
      ["B", "-b", "f.blend", "-s", "1", "-e", "3", "-a"])

print("\nSnapshot")
snap = jobs.snapshot_path(os.path.join("D:", "proj", "shot.blend"), "20260928-1405")
check("snapshot sits next to the .blend", os.path.dirname(snap), os.path.join("D:", "proj"))
check("snapshot name", os.path.basename(snap), "shot.cli_snapshot_20260928-1405.blend")

print()
if FAILS:
    print("%d FAILED: %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("All checks passed.")
