# Run with:  blender -b --factory-startup --python make_test_blend.py -- <out.blend> <save_output 0|1> <media IMAGE|VIDEO>
# Builds a tiny, fast throwaway scene for testing command-line render behaviour.
import sys

import bpy

argv = sys.argv[sys.argv.index("--") + 1:]
out_blend, save_output, media = argv[0], argv[1] == "1", argv[2]

sc = bpy.context.scene
sc.render.engine = 'BLENDER_WORKBENCH'
sc.render.resolution_x, sc.render.resolution_y = 32, 18
sc.render.resolution_percentage = 100
sc.frame_start, sc.frame_end = 1, 3
sc.render.save_output = save_output
if media == "VIDEO":
    sc.render.image_settings.media_type = 'VIDEO'
    sc.render.ffmpeg.format = 'MPEG4'
    sc.render.ffmpeg.codec = 'H264'
else:
    sc.render.image_settings.file_format = 'PNG'

# Compositor: Render Layers -> Group Output, plus a File Output node whose
# directory does NOT exist yet (test: does Blender create it?).
tree = bpy.data.node_groups.new("Test Compositing", "CompositorNodeTree")
sc.compositing_node_group = tree
rl = tree.nodes.new("CompositorNodeRLayers")
go = tree.nodes.new("NodeGroupOutput")
tree.interface.new_socket("Image", in_out='OUTPUT', socket_type='NodeSocketColor')
tree.links.new(rl.outputs["Image"], go.inputs[0])
fo = tree.nodes.new("CompositorNodeOutputFile")
fo.directory = "//comp_out/missing_sub/"
fo.file_name = "comp_"
fo.format.media_type = 'IMAGE'
fo.format.file_format = 'PNG'
item = fo.file_output_items.new('RGBA', "Image")
tree.links.new(rl.outputs["Image"], fo.inputs[item.name])

bpy.ops.wm.save_as_mainfile(filepath=out_blend)
print("MADE", out_blend, "save_output", sc.render.save_output, "media", media)
