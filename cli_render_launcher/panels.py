import os
import textwrap

import bpy

from . import jobs
from .operators import only_frames_video, sample_setting, scene_is_video, scene_saves_output

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


def override_icon(entry):
    """The icon that says which override a SubScene has, or BLANK1 for none.

    One override shows its own icon; several at once show the monkey, and the
    Overrides box under the list says which.
    """
    frames_text = entry.frames.strip()
    if frames_text and subscene_frames(entry)[1]:
        return 'ERROR'                  # a list that cannot render: say so on the row
    active = [icon for icon, on in (('VIEW_CAMERA', entry.camera is not None),
                                    ('NODE_TEXTURE', entry.samples > 0),
                                    # frame related, one icon: every N, or a list
                                    ('RENDER_RESULT', entry.frame_step > 1 or bool(frames_text)))
              if on]
    if not active:
        return 'BLANK1'
    return active[0] if len(active) == 1 else 'MONKEY'


def subscene_frames(entry):
    """(frames, error) of the SubScene's "Only frames" list; (None, None) if empty."""
    if not entry.frames.strip():
        return None, None
    frames, err = jobs.parse_frames(entry.frames, entry.start, entry.end)
    if not err:
        video = only_frames_video(entry.id_data)    # id_data: the SubScene's scene
        if video not in (None, 'skip'):
            return None, video
    return frames, err


def wrapped_labels(layout, text, icon, width=48):
    """A long message as several labels: one label would be cut off."""
    for i, line in enumerate(textwrap.wrap(text, width)):
        layout.label(text=line, icon=icon if i == 0 else 'BLANK1')


# The syntax help under "Only Frames": (example, what it does)
FRAMES_HELP = (
    ("12, 40, 300", "single frames"),
    ("100-120", "a range, both ends included"),
    ("100..120", "the same, Blender's way"),
    ("12, 100-120, 300", "mix them freely"),
)


class FRAMERANGE_UL_List(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        use_subscenes = getattr(context.scene, "use_presets", True)
        row = layout.row(align=True)
        row.enabled = use_subscenes
        # The spare slot at the row's start shows a SubScene's overrides, so
        # the list gets no extra column (the Overrides box spells them out).
        row.label(text="", icon=override_icon(item))
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
        self.draw_overrides(layout, scene)

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

    @staticmethod
    def draw_overrides(layout, scene):
        """The selected SubScene's overrides, folded by default.

        A layout.panel (Blender 4.1+) rather than more list columns: the list
        stays as narrow as before, and the folded header still says what is
        overridden.
        """
        entries = scene.framerange_entries
        if not (0 <= scene.framerange_index < len(entries)):
            return
        entry = entries[scene.framerange_index]
        header, body = layout.panel("CLI_subscene_overrides", default_closed=True)
        header.enabled = scene.use_presets
        frames, frames_error = subscene_frames(entry)
        summary = jobs.override_summary(entry.samples, entry.frame_step,
                                        entry.camera.name if entry.camera else None,
                                        frames)
        if frames_error:
            summary = (summary + "  ·  " if summary else "") + "frame list invalid"
        icon = override_icon(entry)
        header.label(text="Overrides · %s%s" % (entry.name, ("  ·  " + summary) if summary else ""),
                     icon='NONE' if icon == 'BLANK1' else icon)
        if body is None:
            return
        body.enabled = scene.use_presets
        col = body.column()
        col.prop(entry, "samples", text="Samples (0 = scene)")
        if entry.samples and sample_setting(scene) is None:
            col.label(text="%s has no samples setting: ignored"
                      % scene.render.engine.title(), icon='INFO')
        col.separator()
        col.prop(entry, "frames", text="Only Frames")
        if frames_error:
            msg = col.column(align=True)
            msg.alert = True
            wrapped_labels(msg, frames_error[0].upper() + frames_error[1:], 'ERROR')
        elif frames:
            col.label(text="%d frame%s will render" % (len(frames), "s" if len(frames) > 1 else ""),
                      icon='CHECKMARK')
            if only_frames_video(scene) == 'skip':
                wrapped_labels(col.column(align=True), "The main video is skipped for this "
                               "SubScene: the File Output nodes write the frames.", 'INFO')
        help_header, help_body = col.panel("CLI_frames_help", default_closed=True)
        help_header.label(text="How to write frames", icon='QUESTION')
        if help_body is not None:
            box = help_body.box()
            for example, meaning in FRAMES_HELP:
                split = box.split(factor=0.42)
                split.label(text=example)
                split.label(text=meaning)
            box.label(text="Commas or spaces between items.")
            box.label(text="Frames of this SubScene only: %d-%d." % (entry.start, entry.end))
            box.label(text="Empty = the whole SubScene.")
        row = col.row()
        row.active = not frames
        row.prop(entry, "frame_step", text="Every N Frames")
        if frames and entry.frame_step > 1:
            col.label(text="Every N Frames is ignored: Only Frames is set", icon='INFO')
        col.separator()
        col.prop(entry, "camera", text="Camera")
        if entry.camera and any(m.camera for m in scene.timeline_markers):
            col.label(text="Camera markers are ignored for this SubScene", icon='INFO')


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
