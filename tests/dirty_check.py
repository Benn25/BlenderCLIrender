"""Does "save a copy" clear the unsaved-changes flag?

    blender -b <scene.blend> --factory-startup --python dirty_check.py -- <snapshot_path>

If it did, Blender would stop warning about unsaved work on quit after every
snapshot - so this must be known for certain, not assumed.
"""
import sys

import bpy

snap = sys.argv[sys.argv.index("--") + 1]
print("DIRTY after load:", bpy.data.is_dirty)
bpy.context.scene.render.resolution_x = 40
print("DIRTY after python property set:", bpy.data.is_dirty)
bpy.ops.ed.undo_push(message="test edit")
print("DIRTY after undo push:", bpy.data.is_dirty)
before = bpy.data.is_dirty
bpy.ops.wm.save_as_mainfile(filepath=snap, copy=True, check_existing=False)
print("DIRTY after save copy:", bpy.data.is_dirty, "(was %s)" % before)
print("FILEPATH after save copy:", bpy.data.filepath)
