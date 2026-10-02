"""Exercise the panel's draw code in a background Blender.

    blender -b <scene.blend> --factory-startup --python draw_check.py -- <pkg_parent>

Panels are not drawn in background mode, so draw() is called by hand with a
stand-in layout that records every call. Any typo in a property name (the
kind of bug that only shows up as a broken panel) fails layout.prop() here,
because the recorder checks each property really exists on its data.
"""
import sys

import bpy

sys.path.insert(0, sys.argv[sys.argv.index("--") + 1])
import cli_render_launcher  # noqa: E402
from cli_render_launcher import panels  # noqa: E402

cli_render_launcher.register()


class Recorder:
    def __init__(self, log):
        self.log = log
        self.enabled = True
        self.alert = False
        self.scale_x = self.scale_y = 1.0

    def _child(self, *a, **k):
        return Recorder(self.log)

    row = column = box = _child

    def split(self, *a, **k):
        return Recorder(self.log)

    def prop(self, data, name, **k):
        if name not in data.bl_rna.properties:
            raise AttributeError("no property %r on %s" % (name, data))
        self.log.append(("prop", name, k.get("text"), self.enabled))

    def label(self, **k):
        self.log.append(("label", k.get("text")))

    def operator(self, idname, **k):
        mod, fn = idname.split(".")
        getattr(getattr(bpy.ops, mod), fn)   # must exist
        self.log.append(("op", idname))

        class Props:
            pass
        return Props()

    def separator(self, *a, **k):
        pass

    def template_list(self, *a, **k):
        self.log.append(("list",))


class Ctx:
    scene = bpy.context.scene


def draw(save_output):
    bpy.context.scene.render.save_output = save_output
    log = []
    rec = Recorder(log)
    panels.RENDER_PT_cli_launcher.draw(type("P", (), {"layout": rec})(), Ctx)
    panels.RENDER_PT_cli_launcher.draw_header(type("P", (), {"layout": rec})(), Ctx)
    return log


try:
    from cli_render_launcher import preferences as P      # 5.4+
except ImportError:
    P = None
if P is not None:
    rna = P.CLIRENDER_AP_preferences.bl_rna
    for level in ('CLEAN', 'DETAILED', 'FULL'):
        for logs in (True, False):
            fake = type("Prefs", (), {"bl_rna": rna, "console_level": level, "keep_logs": logs})()
            log = []
            P.CLIRENDER_AP_preferences.draw(type("S", (), {
                "layout": Recorder(log), "console_level": level, "keep_logs": logs,
                "gpu_warn_percent": 66 if logs else 0,
                "gpu_stop_percent": 90 if logs else 0, "bl_rna": rna})(), Ctx)
            props = [e[1] for e in log if e[0] == "prop"]
            labels = [e[1] for e in log if e[0] == "label"]
            print("PREFS %-8s logs=%-5s OK: props %s | %d labels | fail-note shown: %s" % (
                level, logs, props, len(labels), any("fails" in (l or "") for l in labels)))

for state in (True, False):
    log = draw(state)
    props = {entry[1]: entry[3] for entry in log if entry[0] == "prop"}
    labels = [entry[1] for entry in log if entry[0] == "label"]
    print("DRAW save_output=%s OK: %d props, %d ops" % (
        state, len(props), sum(1 for e in log if e[0] == "op")))
    print("   output dir enabled:", props.get("cli_output_directory"),
          "| subfolders enabled:", props.get("preset_use_subfolder"),
          "| save_output shown:", "save_output" in props)
    print("   header:", [l for l in labels if l and ("CLI RENDER" in l or l.startswith("v"))])
    if not state:
        print("   info shown:", any(l and "File Output" in l for l in labels))
