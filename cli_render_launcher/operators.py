import bpy
import json
import os
import shlex
import subprocess
import sys
import tempfile
from datetime import datetime

from . import jobs
from .preferences import get_prefs
from .properties import get_timestamp_string

LOG_FOLDER = "cli_render_logs"

RUNNER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runner.py")


# --------------------------------------------------------------------------
# Scene inspection
# --------------------------------------------------------------------------

def scene_saves_output(scene):
    """The scene's own "Save Output" (Blender 5.1+). Off means the main
    render writes nothing - verified on the command line - and only the
    compositor's File Output nodes write files. Older Blender: always on."""
    return getattr(scene.render, "save_output", True)


def _tree_has_file_output(tree, seen):
    if tree is None or tree.name_full in seen:
        return False
    seen.add(tree.name_full)
    for node in tree.nodes:
        if node.mute:
            continue
        if node.bl_idname == 'CompositorNodeOutputFile':
            return True
        if _tree_has_file_output(getattr(node, "node_tree", None), seen):
            return True
    return False


def compositor_writes_files(scene):
    """True when the compositor runs and holds at least one active File
    Output node. Read-only: the nodes are never modified."""
    if not scene.render.use_compositing:
        return False
    tree = getattr(scene, "compositing_node_group", None)        # 5.0+
    if tree is None and getattr(scene, "use_nodes", False):
        tree = getattr(scene, "node_tree", None)                 # 4.x
    return _tree_has_file_output(tree, set())


def scene_is_video(scene):
    img = scene.render.image_settings
    return jobs.is_video_output(getattr(img, "media_type", None), img.file_format)


# --------------------------------------------------------------------------
# Launching
# --------------------------------------------------------------------------

def open_in_terminal(cmd, minimized=False):
    """Start `cmd` in its own terminal window, detached from Blender."""
    if sys.platform == "win32":
        info = None
        if minimized:
            info = subprocess.STARTUPINFO()
            info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            info.wShowWindow = 7            # SW_SHOWMINNOACTIVE
        return subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_CONSOLE,
                                startupinfo=info)
    if sys.platform == "darwin":
        shell_cmd = " ".join(shlex.quote(a) for a in cmd)
        escaped = shell_cmd.replace('\\', '\\\\').replace('"', '\\"')
        return subprocess.Popen(
            ["osascript", "-e", 'tell app "Terminal" to do script "%s"' % escaped])
    try:
        return subprocess.Popen(['x-terminal-emulator', '-e'] + cmd)
    except FileNotFoundError:
        return subprocess.Popen(cmd, start_new_session=True)


def runner_command(jobfile):
    """Blender's bundled Python runs the queue; if it cannot be found, a
    background Blender hosts the script instead (heavier, same result)."""
    py = sys.executable
    if py and os.path.isfile(py) and "python" in os.path.basename(py).lower():
        return [py, RUNNER, jobfile]
    return [bpy.app.binary_path, "-b", "--factory-startup",
            "--python", RUNNER, "--", jobfile]


def launch_queue(job_list, parallel, cleanup, level='CLEAN', gpu_warn=66, gpu_stop=90):
    spec = {
        "jobs": job_list,
        "parallel": parallel,
        "level": level,
        "gpu_warn": gpu_warn,
        "gpu_stop": gpu_stop,
        "console_per_job": sys.platform == "win32",
        "cleanup": cleanup,
    }
    fd, jobfile = tempfile.mkstemp(prefix="cli_render_queue_", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(spec, fh, ensure_ascii=False, indent=1)
    # On Windows the queue's own console starts minimized: each render still
    # opens its own console window, exactly as before.
    open_in_terminal(runner_command(jobfile), minimized=True)


# --------------------------------------------------------------------------
# SubScene list operators
# --------------------------------------------------------------------------

def _valid_index(context):
    scene = context.scene
    return 0 <= scene.framerange_index < len(scene.framerange_entries)


class FRAMERANGE_OT_move_entry(bpy.types.Operator):
    bl_idname = "framerange.move_entry"
    bl_label = "Move SubScene Entry"
    bl_description = "Move this SubScene up or down (the list order is the render order)"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}
    direction: bpy.props.EnumProperty(items=[('UP', "Up", ""), ('DOWN', "Down", "")])
    index: bpy.props.IntProperty()

    def execute(self, context):
        scene = context.scene
        entries = scene.framerange_entries
        idx = self.index
        target = idx - 1 if self.direction == 'UP' else idx + 1
        if 0 <= idx < len(entries) and 0 <= target < len(entries):
            entries.move(idx, target)
            scene.framerange_index = target
        return {'FINISHED'}


class FRAMERANGE_OT_add(bpy.types.Operator):
    bl_idname = "framerange.add_entry"
    bl_label = "Add SubScene"
    bl_description = "Add a new SubScene preset with the current Base Name and frame range"
    bl_options = {'REGISTER', 'UNDO'}

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
        scene.framerange_index = len(scene.framerange_entries) - 1
        return {'FINISHED'}


class FRAMERANGE_OT_delete(bpy.types.Operator):
    bl_idname = "framerange.delete_entry"
    bl_label = "Delete SubScene"
    bl_description = "Delete the selected SubScene preset"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _valid_index(context)

    def execute(self, context):
        scene = context.scene
        idx = scene.framerange_index
        scene.framerange_entries.remove(idx)
        scene.framerange_index = min(idx, len(scene.framerange_entries) - 1)
        return {'FINISHED'}


class FRAMERANGE_OT_apply(bpy.types.Operator):
    bl_idname = "framerange.apply_entry"
    bl_label = "Apply SubScene"
    bl_description = "Apply the selected SubScene preset to the current fields"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _valid_index(context)

    def execute(self, context):
        scene = context.scene
        entry = scene.framerange_entries[scene.framerange_index]
        scene.cli_start_frame = entry.start
        scene.cli_end_frame = entry.end
        scene.cli_base_name = entry.name
        return {'FINISHED'}


class FRAMERANGE_OT_update(bpy.types.Operator):
    bl_idname = "framerange.update_entry"
    bl_label = "Update SubScene"
    bl_description = "Update the selected preset with the current frame range"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _valid_index(context)

    def execute(self, context):
        scene = context.scene
        entry = scene.framerange_entries[scene.framerange_index]
        entry.start = scene.cli_start_frame
        entry.end = scene.cli_end_frame
        return {'FINISHED'}


class RENDER_OT_sync_frame_range(bpy.types.Operator):
    bl_idname = "render.sync_frame_range"
    bl_label = "Sync Frame Range"
    bl_description = "Copy the scene's frame range into the fields"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    def execute(self, context):
        scene = context.scene
        scene.cli_start_frame = scene.frame_start
        scene.cli_end_frame = scene.frame_end
        self.report({'INFO'}, f"Frame range synced: {scene.frame_start}-{scene.frame_end}")
        return {'FINISHED'}


class RENDER_OT_set_start_current(bpy.types.Operator):
    bl_idname = "render.set_start_current"
    bl_label = "Set Start to Current Frame"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    def execute(self, context):
        context.scene.cli_start_frame = context.scene.frame_current
        self.report({'INFO'}, f"Start frame set to {context.scene.frame_current}")
        return {'FINISHED'}


class RENDER_OT_set_end_current(bpy.types.Operator):
    bl_idname = "render.set_end_current"
    bl_label = "Set End to Current Frame"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    def execute(self, context):
        context.scene.cli_end_frame = context.scene.frame_current
        self.report({'INFO'}, f"End frame set to {context.scene.frame_current}")
        return {'FINISHED'}


# --------------------------------------------------------------------------
# The launcher
# --------------------------------------------------------------------------

class RENDER_OT_cli_launcher(bpy.types.Operator):
    bl_idname = "render.cli_launcher"
    bl_label = "Render Animation in CLI"
    bl_description = ("Render in separate background Blender processes. "
                      "Blender stays responsive while they run")

    # No poll(): every refusal below explains itself in the status bar,
    # which a silently greyed-out button would not.

    def execute(self, context):
        scene = context.scene
        blend_path = bpy.data.filepath
        if not blend_path:
            self.report({'ERROR'}, "Save your .blend file once first "
                                   "(relative paths need a location)")
            return {'CANCELLED'}

        # --- what to render, in list order
        use_subscenes = scene.use_presets and any(
            e.selected for e in scene.framerange_entries)
        if use_subscenes:
            ranges = [(e.name.strip() or "render", e.start, e.end, e.name)
                      for e in scene.framerange_entries if e.selected]
        else:
            ranges = [(scene.cli_base_name.strip() or "render",
                       scene.cli_start_frame, scene.cli_end_frame, "")]
        for _base, start, end, label in ranges:
            err = jobs.range_error(start, end, label)
            if err:
                self.report({'ERROR'}, err + " - render not started")
                return {'CANCELLED'}

        # --- where it goes
        save_output = scene_saves_output(scene)
        if save_output:
            out_dir = bpy.path.abspath(scene.cli_output_directory)
            if not os.path.isdir(out_dir):
                self.report({'ERROR'}, "Output Directory does not exist, "
                                       f"render not started: {out_dir}")
                return {'CANCELLED'}
        elif not compositor_writes_files(scene):
            self.report({'ERROR'}, "Save Output is off and the compositor has no "
                                   "active File Output node: nothing would be "
                                   "written. Render not started")
            return {'CANCELLED'}

        # --- which file gets rendered
        cleanup = []
        render_blend = blend_path
        if scene.cli_use_snapshot:
            snap = jobs.snapshot_path(blend_path,
                                      datetime.now().strftime("%Y%m%d-%H%M%S"))
            try:
                bpy.ops.wm.save_as_mainfile(filepath=snap, copy=True,
                                            check_existing=False)
            except RuntimeError as exc:
                self.report({'ERROR'}, f"Could not save the render snapshot: {exc}")
                return {'CANCELLED'}
            render_blend = snap
            cleanup.append(snap)
        elif bpy.data.is_dirty:
            self.report({'WARNING'}, "Unsaved changes will NOT be rendered "
                                     "(rendering the file as saved on disk)")

        # --- console level and logs (add-on preferences)
        prefs = get_prefs(context)
        level = prefs.console_level if prefs else 'CLEAN'
        keep_logs = prefs.keep_logs if prefs else True
        gpu_warn = prefs.gpu_warn_percent if prefs else 66
        gpu_stop = prefs.gpu_stop_percent if prefs else 90
        run_stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        blend_dir, blend_file = os.path.split(blend_path)
        log_dir = os.path.join(blend_dir, LOG_FOLDER)

        # --- one job per range
        timestamp = get_timestamp_string(scene.cli_timestamp_mode)
        is_video = scene_is_video(scene)
        step = max(1, scene.frame_step)
        job_list = []
        for base, start, end, _label in ranges:
            output_path = None
            if save_output:
                folder = out_dir
                if use_subscenes and scene.preset_use_subfolder:
                    folder = os.path.join(out_dir, base)
                    try:
                        os.makedirs(folder, exist_ok=True)
                    except OSError as exc:
                        for path in cleanup:
                            os.remove(path)
                        self.report({'ERROR'}, f"Could not create output folder for "
                                               f"SubScene '{base}', render not started: {exc}")
                        return {'CANCELLED'}
                name = jobs.output_name(base, timestamp, scene.cli_include_framerange,
                                        start, end, is_video)
                output_path = os.path.join(folder, name)
            log = None
            if keep_logs:
                log = os.path.join(log_dir, "%s_%s_%s.log" % (
                    os.path.splitext(blend_file)[0], base, run_stamp))
            job_list.append({
                "name": base,
                "cmd": jobs.render_command(bpy.app.binary_path, render_blend,
                                           start, end, output_path),
                # the frames Blender will render (-a honours the frame step)
                "frames": list(range(start, end + 1, step)),
                "log": log,
            })

        parallel = scene.cli_parallel_jobs if use_subscenes else 1
        try:
            launch_queue(job_list, parallel, cleanup, level, gpu_warn, gpu_stop)
        except OSError as exc:
            for path in cleanup:
                os.remove(path)
            self.report({'ERROR'}, f"Could not start the render queue: {exc}")
            return {'CANCELLED'}

        at_once = min(parallel, len(job_list))
        self.report({'INFO'}, f"Launched {len(job_list)} render job(s), "
                              f"{at_once} at a time. Blender remains responsive.")
        return {'FINISHED'}


CLASSES = (
    FRAMERANGE_OT_move_entry,
    FRAMERANGE_OT_add,
    FRAMERANGE_OT_delete,
    FRAMERANGE_OT_apply,
    FRAMERANGE_OT_update,
    RENDER_OT_sync_frame_range,
    RENDER_OT_set_start_current,
    RENDER_OT_set_end_current,
    RENDER_OT_cli_launcher,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
