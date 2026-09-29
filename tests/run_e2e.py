"""End-to-end scenarios for the CLI Render Launcher - needs a Blender binary.

    python tests/run_e2e.py <blender.exe> <workdir>

<workdir> must hold t_on/, t_off/, t_vid/ test scenes (tests/make_test_blend.py).
Each scenario copies its scene, runs the real operator in a background
Blender (tests/e2e_operator.py), then waits for the render queue - which
outlives that Blender - and checks the files it produced.
"""
import glob
import json
import os
import shutil
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = (sorted(glob.glob(os.path.join(HERE, "..", "V*"))) or [os.path.join(HERE, "..", "cli_render_launcher")])[-1]
BLENDER, WORK = sys.argv[1], os.path.abspath(sys.argv[2])
FAILS = []


def check(label, got, want):
    ok = got == want
    print("  %s  %-56s %r" % ("PASS" if ok else "FAIL", label, got))
    if not ok:
        FAILS.append(label)
        print("        want %r" % (want,))


def png_width(path):
    with open(path, "rb") as fh:
        return struct.unpack(">I", fh.read(24)[16:20])[0]


# the add-on as an importable package, straight from the source folder
PKG_PARENT = os.path.join(WORK, "pkg")
shutil.rmtree(PKG_PARENT, ignore_errors=True)
shutil.copytree(SRC, os.path.join(PKG_PARENT, "cli_render_launcher"),
                ignore=shutil.ignore_patterns("__pycache__"))


def scenario(name, scene_dir, cfg, expect_files, timeout=180):
    run_dir = os.path.join(WORK, "e2e_" + name)
    shutil.rmtree(run_dir, ignore_errors=True)
    os.makedirs(os.path.join(run_dir, "out"))
    blend = os.path.join(run_dir, "scene.blend")
    shutil.copy(os.path.join(WORK, scene_dir, "test.blend"), blend)
    cfg.setdefault("scene", {})["cli_output_directory"] = os.path.join(run_dir, "out") + os.sep
    cfg_path = os.path.join(run_dir, "cfg.json")
    json.dump(cfg, open(cfg_path, "w", encoding="utf-8"))

    t0 = time.time()
    proc = subprocess.run([BLENDER, "-b", blend, "--factory-startup", "--python",
                           os.path.join(HERE, "e2e_operator.py"), "--",
                           PKG_PARENT, cfg_path],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120)
    line = next((l for l in proc.stdout.splitlines() if l.startswith("E2E_OUTCOME ")), None)
    outcome = json.loads(line[len("E2E_OUTCOME "):]) if line else {"error": proc.stdout[-800:]}
    print("\n[%s] operator: %s %s  (launcher Blender exited after %.1fs)"
          % (name, outcome.get("result"), outcome.get("error") or "", time.time() - t0))

    if expect_files is None:           # a refusal scenario: nothing may appear
        time.sleep(3)
        return outcome, run_dir, []
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = sorted(os.path.relpath(p, run_dir).replace(os.sep, "/")
                       for p in glob.glob(os.path.join(run_dir, "**", "*.*"), recursive=True)
                       if not p.endswith((".blend", ".json", ".blend1")))
        snaps = glob.glob(os.path.join(run_dir, "*cli_snapshot*"))
        if set(expect_files) <= set(found) and not snaps:
            break
        time.sleep(1)
    print("   files after %.0fs: %s" % (time.time() - t0, found))
    return outcome, run_dir, found


# 1 - SubScenes, 2 at a time, subfolders, range in names, single-frame
#     SubScene, and an UNSAVED resolution change that only a snapshot sees.
out, d, files = scenario("subscenes", "t_on", {
    "scene": {"use_presets": True, "preset_use_subfolder": True,
              "cli_include_framerange": True, "cli_timestamp_mode": 'NONE',
              "cli_parallel_jobs": 2, "cli_use_snapshot": True},
    "render": {"resolution_x": 40},
    "subscenes": [["a", 1, 2, True], ["b", 3, 4, True], ["skip", 1, 1, False],
                  ["c", 5, 5, True]],
}, ["out/a/a_0001-0002_0001.png", "out/a/a_0001-0002_0002.png",
    "out/b/b_0003-0004_0003.png", "out/b/b_0003-0004_0004.png",
    "out/c/c_0005-0005_0005.png"])
check("operator finished", out.get("result"), ["FINISHED"])
check("frame numbers separated, single frame rendered",
      [f for f in files if f.startswith("out/")],
      ["out/a/a_0001-0002_0001.png", "out/a/a_0001-0002_0002.png",
       "out/b/b_0003-0004_0003.png", "out/b/b_0003-0004_0004.png",
       "out/c/c_0005-0005_0005.png"])
check("unticked SubScene not rendered", os.path.isdir(os.path.join(d, "out", "skip")), False)
check("snapshot rendered the UNSAVED 40 px width",
      png_width(os.path.join(d, "out", "a", "a_0001-0002_0001.png")) if files else None, 40)
check("snapshot deleted after the queue", glob.glob(os.path.join(d, "*cli_snapshot*")), [])
check("file still dirty (snapshot is a copy)", out.get("dirty_after"), True)
check("file path unchanged by the snapshot",
      os.path.basename(out.get("filepath_after", "")), "scene.blend")
logs = sorted(glob.glob(os.path.join(d, "cli_render_logs", "*.log")))
if os.path.exists(os.path.join(SRC, "console_filter.py")):          # 5.4+
    check("one full log per ticked SubScene", len(logs), 3)
    check("logs hold Blender's raw output",
          all("Rendering frame" in open(p, encoding="utf-8", errors="replace").read()
              for p in logs) if logs else False, True)
    check("log names: <blend>_<subscene>_<time>.log",
          [os.path.basename(p).split("_")[:2] for p in logs],
          [["scene", "a"], ["scene", "b"], ["scene", "c"]])

# 2 - no snapshot: renders the file as SAVED (32 px), and warns.
out, d, files = scenario("no_snapshot", "t_on", {
    "scene": {"use_presets": False, "cli_base_name": "shot", "cli_start_frame": 1,
              "cli_end_frame": 1, "cli_use_snapshot": False,
              "cli_timestamp_mode": 'NONE'},
    "render": {"resolution_x": 40},
}, ["out/shot_0001.png"])
check("renders the saved file (32 px, not 40)",
      png_width(os.path.join(d, "out", "shot_0001.png")) if files else None, 32)

# 3 - Save Output OFF with an active File Output node: launches, writes only
#     the compositor files, and needs no main output folder at all.
out, d, files = scenario("save_output_off", "t_off", {
    "scene": {"use_presets": False, "cli_start_frame": 1, "cli_end_frame": 2,
              "preset_use_subfolder": True},
}, ["comp_out/missing_sub/comp_Image0001.png", "comp_out/missing_sub/comp_Image0002.png"])
check("operator finished", out.get("result"), ["FINISHED"])
check("only compositor files written (logs aside)",
      [f for f in files if not f.startswith("cli_render_logs/")],
      ["comp_out/missing_sub/comp_Image0001.png", "comp_out/missing_sub/comp_Image0002.png"])

# 4 - Save Output OFF and the File Output node muted: nothing would be
#     written, so the render must be refused.
out, d, files = scenario("nothing_to_write", "t_off", {
    "scene": {"use_presets": False, "cli_start_frame": 1, "cli_end_frame": 2},
    "mute_file_output": True,
}, None)
check("refused", out.get("result"), ["CANCELLED"])
check("says why", "nothing would be written" in (out.get("error") or ""), True)

# 5 - backwards range refused (and names the SubScene).
out, d, files = scenario("backwards", "t_on", {
    "scene": {"use_presets": True},
    "subscenes": [["oops", 10, 5, True]],
}, None)
check("backwards range refused", out.get("result"), ["CANCELLED"])
check("error names the SubScene", "oops" in (out.get("error") or ""), True)

# 6 - video: the frame range must not be doubled.
out, d, files = scenario("video", "t_vid", {
    "scene": {"use_presets": False, "cli_base_name": "clip", "cli_start_frame": 1,
              "cli_end_frame": 3, "cli_include_framerange": True,
              "cli_timestamp_mode": 'NONE'},
}, ["out/clip0001-0003.mp4"])
check("video named once by Blender", [f for f in files if f.startswith("out/")],
      ["out/clip0001-0003.mp4"])

print()
if FAILS:
    print("%d FAILED: %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("All end-to-end checks passed.")
