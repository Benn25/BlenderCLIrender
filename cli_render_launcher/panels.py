import os

import bpy

from .operators import scene_is_video, scene_saves_output

try:
    import tomllib
except ModuleNotFoundError:     # Python < 3.11: not expected on Blender 4.2+
    tomllib = None


def _read_version():
    """Version straight from the installed manifest, so the header always
    shows the files actually on disk (no hand-copied constant to drift)."""
    if tomllib is None:
        return "?"
    try:
        path = os.path.join(os.path.dirname(__file__), "blender_manifest.toml")
        with open(path, "rb") as fh:
            return str(tomllib.load(fh).get("version", "?"))
    except Exception:
        return "?"


ADDON_VERSION = _read_version()


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


class RENDER_PT_cli_launcher(bpy.types.Panel):
    bl_label = " "
    bl_idname = "RENDER_PT_cli_launcher"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "render"

    def draw_header(self, context):
        row = self.layout.row(align=True)
        title = row.row(align=True)
        title.alert = True
        title.label(text="╠══ CLI RENDER ══╣")
        row.label(text="v" + ADDON_VERSION)

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        save_output = scene_saves_output(scene)

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

        # --- SubScenes
        layout.prop(scene, "use_presets", text="Use SubScenes")
        col = layout.column()
        col.enabled = scene.use_presets
        sub = col.row()
        sub.enabled = save_output
        sub.prop(scene, "preset_use_subfolder", text="Put SubScenes in subfolders")
        row = col.row()
        row.prop(scene, "cli_parallel_jobs")
        if scene.cli_parallel_jobs == 1:
            row.label(text="one by one")
        else:
            row.label(text=f"needs GPU memory for {scene.cli_parallel_jobs}", icon='INFO')

        layout.label(text="SubScenes Presets:")
        row = layout.row()
        row.enabled = scene.use_presets
        row.template_list("FRAMERANGE_UL_List", "", scene, "framerange_entries", scene, "framerange_index", rows=6)
        col = row.column(align=True)
        col.operator("framerange.add_entry", icon='ADD', text="")
        col.operator("framerange.delete_entry", icon='REMOVE', text="")
        col.operator("framerange.apply_entry", icon='EXPORT', text="")
        col.operator("framerange.update_entry", icon='IMPORT', text="")

        # --- Output. "Save Output" is the scene's own setting (Blender 5.1+),
        # shown here so it is visible where it matters; the add-on only reads it.
        if hasattr(scene.render, "save_output"):
            layout.prop(scene.render, "save_output", text="Save Output (main render)")
            if not save_output:
                box = layout.box()
                box.label(text="Only the compositor's File Output nodes", icon='INFO')
                box.label(text="will write files. Settings below unused.")
        col = layout.column()
        col.enabled = save_output
        col.prop(scene, "cli_output_directory")
        col.prop(scene, "cli_timestamp_mode")
        row = col.row()
        is_video = scene_is_video(scene)
        row.enabled = not is_video
        row.prop(scene, "cli_include_framerange",
                 text="Include Frame Range (Blender adds it to videos)" if is_video
                 else "Include Frame Range")

        layout.prop(scene, "cli_use_snapshot",
                    text="Render current state (temporary snapshot)")
        layout.operator("render.cli_launcher", icon='CONSOLE')


CLASSES = (
    FRAMERANGE_UL_List,
    RENDER_PT_cli_launcher,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
