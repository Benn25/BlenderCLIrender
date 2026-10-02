# Run with: blender -b --factory-startup --python make_problem_blend.py -- <out.blend>
# A tiny Cycles scene with two planted problems, to record what Blender prints
# for them: a missing image texture and a driver whose expression fails.
import sys

import bpy

out_blend = sys.argv[sys.argv.index("--") + 1]
sc = bpy.context.scene
sc.render.engine = 'CYCLES'
sc.cycles.device = 'CPU'
sc.cycles.samples = 16
sc.cycles.use_denoising = False
sc.render.resolution_x, sc.render.resolution_y = 48, 27
sc.frame_start, sc.frame_end = 1, 3
sc.render.image_settings.file_format = 'PNG'

cube = bpy.data.objects.get("Cube")
mat = bpy.data.materials.new("Missing Texture Mat")
cube.data.materials.clear()
cube.data.materials.append(mat)
tree = mat.node_tree
tex = tree.nodes.new("ShaderNodeTexImage")
img = bpy.data.images.new("wood_diffuse", 8, 8)
img.source = 'FILE'
img.filepath = "//textures/wood_diffuse.png"      # does not exist
tex.image = img
bsdf = next(n for n in tree.nodes if n.type == 'BSDF_PRINCIPLED')
tree.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])

drv = cube.driver_add("location", 2).driver
drv.type = 'SCRIPTED'
drv.expression = "undefined_function(frame)"      # fails at evaluation

bpy.ops.wm.save_as_mainfile(filepath=out_blend)
print("MADE", out_blend)
