"""End-to-end: the real add-on operator inside a throwaway background Blender.

Run by tests/run_e2e.py, never directly:
    blender -b <scene.blend> --factory-startup --python e2e_operator.py -- <pkg_parent> <config.json>

Registers the add-on straight from its source copy (no install), applies the
scenario's settings (some of them left UNSAVED on purpose), clicks
"Render Animation in CLI" and prints the outcome. Blender then exits at
once - the render queue must keep running on its own.
"""
import json
import sys

import bpy

argv = sys.argv[sys.argv.index("--") + 1:]
sys.path.insert(0, argv[0])
cfg = json.load(open(argv[1], encoding="utf-8"))

import cli_render_launcher  # noqa: E402
cli_render_launcher.register()

sc = bpy.context.scene
for key, value in cfg.get("scene", {}).items():
    setattr(sc, key, value)
for key, value in cfg.get("render", {}).items():
    setattr(sc.render, key, value)
for name, start, end, sel in cfg.get("subscenes", []):
    e = sc.framerange_entries.add()
    e.name, e.start, e.end, e.selected = name, start, end, sel
if cfg.get("mute_file_output"):
    for node in sc.compositing_node_group.nodes:
        if node.bl_idname == 'CompositorNodeOutputFile':
            node.mute = True
# Python property sets do not mark a file dirty in background mode; an undo
# push does - so the edits above really count as "unsaved changes".
bpy.ops.ed.undo_push(message="e2e edits")

try:
    result = bpy.ops.render.cli_launcher()
    outcome = {"result": sorted(result), "error": None}
except RuntimeError as exc:
    outcome = {"result": ["CANCELLED"], "error": str(exc).strip()}
outcome["dirty_after"] = bpy.data.is_dirty
outcome["filepath_after"] = bpy.data.filepath
print("E2E_OUTCOME " + json.dumps(outcome))
