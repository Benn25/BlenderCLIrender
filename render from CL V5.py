bl_info = {
    "name": "Command line Render Launcher (with Custom Output & Frame Range SubScenes)",
    "author": "Benn",
    "version": (5, 0, 0),
    "blender": (2, 80, 0),
    "location": "Render Properties > CLI Render",
    "description": "Batch render with custom output, SubScene presets, subfolder support, and sequential/parallel option. No camera override, no temp file logic, always fully responsive.",
    "category": "Render",
}

import bpy
import os
import subprocess
import sys
import threading
from datetime import datetime

def get_timestamp_string(mode):
    now = datetime.now()
    if mode == 'TIME':
        return now.strftime("_%H-%M__")
    elif mode == 'DATE':
        return now.strftime("_%m-%d__")
    elif mode == 'DATETIME':
        return now.strftime("_%m-%d_%H-%M__")
    return ""

class FrameRangeEntry(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty()
    start: bpy.props.IntProperty()
    end: bpy.props.IntProperty()
    selected: bpy.props.BoolProperty(name="Render", default=False)
    order: bpy.props.IntProperty(name="Order", default=0)

class FRAMERANGE_UL_List(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        use_subscenes = getattr(context.scene, "use_presets", True)
        row = layout.row(align=True)
        row.enabled = use_subscenes
        row.label(text="", icon='BLANK1')
        row.prop(item, "selected", text="")
        start = row.row(align=True)
        start.scale_x = 0.8
        start.prop(item, "start", text="")
        end = row.row(align=True)
        end.scale_x = 0.8
        end.prop(item, "end", text="")
        row.prop(item, "name", text="")
        arrows = row.row(align=True)
        up = arrows.operator("framerange.move_entry", text="", icon='TRIA_UP', emboss=False)
        up.direction = 'UP'
        up.index = index
        down = arrows.operator("framerange.move_entry", text="", icon='TRIA_DOWN', emboss=False)
        down.direction = 'DOWN'
        down.index = index

class FRAMERANGE_OT_move_entry(bpy.types.Operator):
    bl_idname = "framerange.move_entry"
    bl_label = "Move SubScene Entry"
    direction: bpy.props.EnumProperty(items=[('UP', "Up", ""), ('DOWN', "Down", "")])
    index: bpy.props.IntProperty()
    def execute(self, context):
        scene = context.scene
        entries = scene.framerange_entries
        idx = self.index
        if self.direction == 'UP' and idx > 0:
            entries.move(idx, idx - 1)
            entries[idx].order, entries[idx - 1].order = entries[idx - 1].order, entries[idx].order
            scene.framerange_index = idx - 1
        elif self.direction == 'DOWN' and idx < len(entries) - 1:
            entries.move(idx, idx + 1)
            entries[idx].order, entries[idx + 1].order = entries[idx + 1].order, entries[idx].order
            scene.framerange_index = idx + 1
        return {'FINISHED'}

class FRAMERANGE_OT_add(bpy.types.Operator):
    bl_idname = "framerange.add_entry"
    bl_label = "Add SubScene"
    bl_description = "Add a new SubScene preset with the current Base Name and frame range"
    def execute(self, context):
        scene = context.scene
        base = scene.cli_base_name.strip() or "SubScene"
        names = {item.name for item in scene.framerange_entries}
        name = base
        idx = 1
        while name in names:
            name = f"{base}_{idx}"
            idx += 1
        entry = scene.framerange_entries.add()
        entry.name = name
        entry.start = scene.cli_start_frame
        entry.end = scene.cli_end_frame
        entry.selected = False
        entry.order = len(scene.framerange_entries) - 1
        scene.framerange_index = len(scene.framerange_entries) - 1
        return {'FINISHED'}

class FRAMERANGE_OT_delete(bpy.types.Operator):
    bl_idname = "framerange.delete_entry"
    bl_label = "Delete SubScene"
    bl_description = "Delete the selected SubScene preset"
    def execute(self, context):
        scene = context.scene
        idx = scene.framerange_index
        if 0 <= idx < len(scene.framerange_entries):
            scene.framerange_entries.remove(idx)
            for i, entry in enumerate(scene.framerange_entries):
                entry.order = i
            scene.framerange_index = min(idx, len(scene.framerange_entries) - 1)
        return {'FINISHED'}

class FRAMERANGE_OT_apply(bpy.types.Operator):
    bl_idname = "framerange.apply_entry"
    bl_label = "Apply SubScene"
    bl_description = "Apply the selected SubScene preset to the current fields"
    def execute(self, context):
        scene = context.scene
        idx = scene.framerange_index
        if 0 <= idx < len(scene.framerange_entries):
            entry = scene.framerange_entries[idx]
            scene.cli_start_frame = entry.start
            scene.cli_end_frame = entry.end
            scene.cli_base_name = entry.name
        return {'FINISHED'}

class FRAMERANGE_OT_update(bpy.types.Operator):
    bl_idname = "framerange.update_entry"
    bl_label = "Update SubScene"
    bl_description = "Update the selected preset with the current frame range"
    def execute(self, context):
        scene = context.scene
        idx = scene.framerange_index
        if 0 <= idx < len(scene.framerange_entries):
            entry = scene.framerange_entries[idx]
            entry.start = scene.cli_start_frame
            entry.end = scene.cli_end_frame
        return {'FINISHED'}

class RENDER_PT_cli_launcher(bpy.types.Panel):
    bl_label = "CLI Render"
    bl_idname = "RENDER_PT_cli_launcher"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "render"
    def draw(self, context):
        layout = self.layout
        scene = context.scene
        layout.prop(scene, "cli_base_name")
        box = layout.box()
        row = box.row()
        row.label(text="Frame Range:")
        row.operator("render.sync_frame_range", icon='FILE_REFRESH', text="Sync Full Range")
        row = box.row(align=True)
        split = row.split(factor=0.5, align=True)
        split.prop(scene, "cli_start_frame", text="")
        split.operator("render.set_start_current", text="Set to Playhead", icon='PREVIEW_RANGE')
        row = box.row(align=True)
        split = row.split(factor=0.5, align=True)
        split.prop(scene, "cli_end_frame", text="")
        split.operator("render.set_end_current", text="Set to Playhead", icon='PREVIEW_RANGE')

        layout.prop(scene, "use_presets", text="Use SubScenes")
        layout.prop(scene, "preset_use_subfolder", text="Put SubScenes in subfolders")
        layout.prop(scene, "sequential_render", text="Render SubScenes one by one")

        layout.label(text="SubScenes Presets:")
        row = layout.row()
        row.enabled = scene.use_presets
        row.template_list("FRAMERANGE_UL_List", "", scene, "framerange_entries", scene, "framerange_index", rows=6)
        col = row.column(align=True)
        col.operator("framerange.add_entry", icon='ADD', text="")
        col.operator("framerange.delete_entry", icon='REMOVE', text="")
        col.operator("framerange.apply_entry", icon='EXPORT', text="")
        col.operator("framerange.update_entry", icon='IMPORT', text="")
        layout.prop(scene, "cli_output_directory")
        layout.prop(scene, "cli_timestamp_mode")
        layout.prop(scene, "cli_include_framerange")

        layout.operator("render.cli_launcher", icon='CONSOLE')

class RENDER_OT_sync_frame_range(bpy.types.Operator):
    bl_idname = "render.sync_frame_range"
    bl_label = "Sync Frame Range"
    bl_options = {'INTERNAL'}
    def execute(self, context):
        scene = context.scene
        scene.cli_start_frame = scene.frame_start
        scene.cli_end_frame = scene.frame_end
        self.report({'INFO'}, f"Frame range synced: {scene.frame_start}-{scene.frame_end}")
        return {'FINISHED'}

class RENDER_OT_set_start_current(bpy.types.Operator):
    bl_idname = "render.set_start_current"
    bl_label = "Set Start to Current Frame"
    bl_options = {'INTERNAL'}
    def execute(self, context):
        context.scene.cli_start_frame = context.scene.frame_current
        self.report({'INFO'}, f"Start frame set to {context.scene.frame_current}")
        return {'FINISHED'}

class RENDER_OT_set_end_current(bpy.types.Operator):
    bl_idname = "render.set_end_current"
    bl_label = "Set End to Current Frame"
    bl_options = {'INTERNAL'}
    def execute(self, context):
        context.scene.cli_end_frame = context.scene.frame_current
        self.report({'INFO'}, f"End frame set to {context.scene.frame_current}")
        return {'FINISHED'}

class RENDER_OT_cli_launcher(bpy.types.Operator):
    bl_idname = "render.cli_launcher"
    bl_label = "Render Animation in CLI"

    def run_sequential(self, cmds):
        for render_cmd in cmds:
            if sys.platform == "win32":
                p = subprocess.Popen(render_cmd, creationflags=subprocess.CREATE_NEW_CONSOLE)
            elif sys.platform == "darwin":
                osa_cmd = [
                    "osascript", "-e",
                    f'tell app "Terminal" to do script "{" ".join(render_cmd)}"'
                ]
                p = subprocess.Popen(osa_cmd)
            else:
                try:
                    p = subprocess.Popen(['x-terminal-emulator', '-e'] + render_cmd)
                except FileNotFoundError:
                    p = subprocess.Popen(render_cmd)
            p.wait()

    def execute(self, context):
        scene = context.scene
        blend_path = bpy.data.filepath
        if not blend_path:
            self.report({'ERROR'}, "Please save your .blend file first!")
            return {'CANCELLED'}

        use_subscenes = getattr(scene, "use_presets", True)
        preset_use_subfolder = getattr(scene, "preset_use_subfolder", False)
        sequential = getattr(scene, "sequential_render", True)
        checked = [entry for entry in scene.framerange_entries if entry.selected]
        cmds = []

        video_extensions = {'.mp4', '.avi', '.mov', '.mkv', '.flv', '.wmv', '.webm',
                           '.ogg', '.ogv', '.mpeg', '.mpg', '.m4v', '.3gp'}

        def make_filename(base, timestamp, framerange, is_video):
            return f"{base}{timestamp}" if is_video else f"{base}{timestamp}{framerange}"

        if use_subscenes and checked:
            checked = sorted(checked, key=lambda e: e.order)
            for entry in checked:
                base = entry.name.strip() or "render"
                start = entry.start
                end = entry.end
                timestamp = get_timestamp_string(scene.cli_timestamp_mode) if scene.cli_timestamp_mode != 'NONE' else ""
                framerange = f"{start:04}-{end:04}" if scene.cli_include_framerange else ""
                output_dir = bpy.path.abspath(scene.cli_output_directory)
                if preset_use_subfolder:
                    output_dir = os.path.join(output_dir, base)
                    os.makedirs(output_dir, exist_ok=True)
                ext = os.path.splitext(base)[1].lower()
                is_video = ext in video_extensions
                filename = make_filename(base, timestamp, framerange, is_video)
                output_pattern = os.path.join(output_dir, filename)
                render_cmd = [
                    bpy.app.binary_path,
                    "-b", blend_path,
                    "-s", str(start),
                    "-e", str(end),
                    "-o", output_pattern,
                    "-a"
                ]
                cmds.append(render_cmd)
        else:
            base = scene.cli_base_name.strip() or "render"
            start = scene.cli_start_frame
            end = scene.cli_end_frame
            timestamp = get_timestamp_string(scene.cli_timestamp_mode) if scene.cli_timestamp_mode != 'NONE' else ""
            framerange = f"{start:04}-{end:04}" if scene.cli_include_framerange else ""
            output_dir = bpy.path.abspath(scene.cli_output_directory)
            ext = os.path.splitext(base)[1].lower()
            is_video = ext in video_extensions
            filename = make_filename(base, timestamp, framerange, is_video)
            output_pattern = os.path.join(output_dir, filename)
            render_cmd = [
                bpy.app.binary_path,
                "-b", blend_path,
                "-s", str(start),
                "-e", str(end),
                "-o", output_pattern,
                "-a"
            ]
            cmds.append(render_cmd)

        if sequential and len(cmds) > 1:
            import threading
            thread = threading.Thread(target=self.run_sequential, args=(cmds,))
            thread.start()
        else:
            for render_cmd in cmds:
                if sys.platform == "win32":
                    subprocess.Popen(render_cmd, creationflags=subprocess.CREATE_NEW_CONSOLE)
                elif sys.platform == "darwin":
                    osa_cmd = [
                        "osascript", "-e",
                        f'tell app "Terminal" to do script "{" ".join(render_cmd)}"'
                    ]
                    subprocess.Popen(osa_cmd)
                else:
                    try:
                        subprocess.Popen(['x-terminal-emulator', '-e'] + render_cmd)
                    except FileNotFoundError:
                        subprocess.Popen(render_cmd)

        self.report({'INFO'}, f"Launched {len(cmds)} render job(s). Blender UI remains responsive.")
        return {'FINISHED'}

classes = (
    FrameRangeEntry,
    FRAMERANGE_UL_List,
    FRAMERANGE_OT_move_entry,
    FRAMERANGE_OT_add,
    FRAMERANGE_OT_delete,
    FRAMERANGE_OT_apply,
    FRAMERANGE_OT_update,
    RENDER_PT_cli_launcher,
    RENDER_OT_cli_launcher,
    RENDER_OT_sync_frame_range,
    RENDER_OT_set_start_current,
    RENDER_OT_set_end_current,
)

def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.cli_base_name = bpy.props.StringProperty(
        name="SubScene base Name",
        description="Base name for rendered files",
        default="render"
    )
    bpy.types.Scene.cli_start_frame = bpy.props.IntProperty(
        name="Start Frame",
        default=1200,
        min=-1000
    )
    bpy.types.Scene.cli_end_frame = bpy.props.IntProperty(
        name="End Frame",
        default=1250,
        min=-1000
    )
    bpy.types.Scene.cli_output_directory = bpy.props.StringProperty(
        name="Output Directory",
        description="Absolute directory for rendered frames",
        default="//",
        subtype='DIR_PATH'
    )
    bpy.types.Scene.cli_timestamp_mode = bpy.props.EnumProperty(
        name="Timestamp Mode",
        description="How to add timestamp to filename",
        items=[
            ('NONE', "None", "No timestamp"),
            ('TIME', "Time Only", "Add only time (HH-MM)"),
            ('DATE', "Date Only", "Add only date (MM-DD)"),
            ('DATETIME', "Date and Time", "Add date (MM-DD) and time (HH-MM)"),
        ],
        default='NONE'
    )
    bpy.types.Scene.cli_include_framerange = bpy.props.BoolProperty(
        name="Include Frame Range",
        default=False
    )
    bpy.types.Scene.use_presets = bpy.props.BoolProperty(
        name="Use SubScenes",
        description="Enable to use the batch SubScene list",
        default=True
    )
    bpy.types.Scene.preset_use_subfolder = bpy.props.BoolProperty(
        name="Put SubScenes in subfolders",
        description="Place each SubScene render output in its subfolder",
        default=False
    )
    bpy.types.Scene.sequential_render = bpy.props.BoolProperty(
        name="Render SubScenes one by one",
        description="If enabled, checked SubScenes will render sequentially, else in parallel",
        default=True
    )
    bpy.types.Scene.framerange_entries = bpy.props.CollectionProperty(type=FrameRangeEntry)
    bpy.types.Scene.framerange_index = bpy.props.IntProperty(default=0)

def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.cli_base_name
    del bpy.types.Scene.cli_start_frame
    del bpy.types.Scene.cli_end_frame
    del bpy.types.Scene.cli_output_directory
    del bpy.types.Scene.cli_timestamp_mode
    del bpy.types.Scene.cli_include_framerange
    del bpy.types.Scene.use_presets
    del bpy.types.Scene.preset_use_subfolder
    del bpy.types.Scene.sequential_render
    del bpy.types.Scene.framerange_entries
    del bpy.types.Scene.framerange_index

if __name__ == "__main__":
    register()
