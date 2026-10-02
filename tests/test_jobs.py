"""Checks for jobs.py - plain Python, no Blender needed.

    python tests/test_jobs.py

The expected names/commands were confirmed against real command-line
renders in Blender 5.2.2 (see the V5.3 changelog in the conversation).
"""
import glob
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, (sorted(glob.glob(os.path.join(HERE, "..", "V*")), key=lambda p: [int(x) for x in os.path.basename(p)[1:].split(".") if x.isdigit()]) or [os.path.join(HERE, "..", "cli_render_launcher")])[-1])

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

print("\nSubScene overrides: command line")
check("no overrides: command unchanged",
      jobs.render_command("B", "f.blend", 1, 9, "o/x_", "Sc"),
      ["B", "-b", "f.blend", "-S", "Sc", "-s", "1", "-e", "9", "-o", "o/x_", "-a"])
check("every 4th frame: -j before -a, step 1 adds nothing",
      (jobs.render_command("B", "f.blend", 1, 9, None, "Sc", frame_step=4)[-3:],
       "-j" in jobs.render_command("B", "f.blend", 1, 9, None, "Sc", frame_step=1)),
      (["-j", "4", "-a"], False))
cmd = jobs.render_command("B", "f.blend", 1, 9, None, "Sc", python_expr="X")
check("python after -S, before the range, failing = exit 77",
      cmd[3:9], ["-S", "Sc", "--python-exit-code", "77", "--python-expr", "X"])
check("no override: no python at all", jobs.override_expr("Sc", "cycles.samples", 0, None), None)
check("samples without a samples setting (Workbench): nothing",
      jobs.override_expr("Sc", None, 16, None), None)
for value in (jobs.override_expr("Scène", "cycles.samples", 16, "Caméra"),):
    check("one line, plain ASCII", ("\n" in value, value.isascii()), (False, True))

print("\nSubScene overrides: the Python actually run (fake bpy)")


class Obj:
    def __init__(self, name):
        self.name = name


class Marker:
    def __init__(self, cam):
        self.camera = cam


class NS:
    pass


def fake_scene(name):
    sc = NS()
    sc.name = name
    sc.cycles = NS()
    sc.cycles.samples = 512
    sc.eevee = NS()
    sc.eevee.taa_render_samples = 64
    sc.objects = {"Main": Obj("Main"), "Caméra": Obj("Caméra")}
    sc.camera = sc.objects["Main"]
    sc.timeline_markers = [Marker(sc.objects["Main"]), Marker(None), Marker(sc.objects["Main"])]
    return sc


def run_expr(expr, scene):
    bpy = NS()
    bpy.data = NS()
    bpy.data.scenes = {scene.name: scene}
    sys.modules["bpy"] = bpy
    try:
        exec(expr, {})
    finally:
        del sys.modules["bpy"]


sc = fake_scene("Scène")
run_expr(jobs.override_expr("Scène", "cycles.samples", 16, "Caméra"), sc)
check("cycles samples set", sc.cycles.samples, 16)
check("forced camera set", sc.camera.name, "Caméra")
check("camera markers can no longer switch it",
      [m.camera for m in sc.timeline_markers], [None, None, None])
sc = fake_scene("S")
run_expr(jobs.override_expr("S", "eevee.taa_render_samples", 8, None), sc)
check("EEVEE samples set, camera and markers left alone",
      (sc.eevee.taa_render_samples, sc.camera.name, sc.timeline_markers[0].camera.name),
      (8, "Main", "Main"))

print("\nSubScene overrides: resuming after a GPU stop")
import scheduler  # noqa: E402
cmd = jobs.render_command("B", "f.blend", 1, 9, None, "-s", frame_step=4,
                          python_expr=jobs.override_expr("-s", "cycles.samples", 16, None))
resumed = scheduler.with_start(cmd, 5)
check("resume moves only the start (even in a scene called '-s')",
      [(a, b) for a, b in zip(cmd, resumed) if a != b], [("1", "5")])
check("resume keeps -j and the overrides", resumed[-7:], ["-s", "5", "-e", "9", "-j", "4", "-a"])
check("same exit code on both sides", scheduler.OVERRIDE_CODE, jobs.OVERRIDE_FAILED_CODE)
job = scheduler.Job({"name": "a", "cmd": cmd, "frames": [1, 5, 9], "overrides": "every 4th frame"})
check("the console gets the overrides in words", job.as_launch()["overrides"], "every 4th frame")

print("\nSubScene overrides: in words")
check("all three", jobs.override_summary(16, 4, "CloseUp"),
      "16 samples · every 4th frame · camera CloseUp")
check("none", jobs.override_summary(0, 1, None), "")
check("ordinals", [jobs.ordinal(n) for n in (2, 3, 11, 12, 21, 22)],
      ["2nd", "3rd", "11th", "12th", "21st", "22nd"])

print("\nSnapshot")
snap = jobs.snapshot_path(os.path.join("D:", "proj", "shot.blend"), "20260928-1405")
check("snapshot sits next to the .blend", os.path.dirname(snap), os.path.join("D:", "proj"))
check("snapshot name", os.path.basename(snap), "shot.cli_snapshot_20260928-1405.blend")

print()
if FAILS:
    print("%d FAILED: %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("All checks passed.")
