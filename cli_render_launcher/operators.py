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
    return bool(_compositor_tree(scene)) and _tree_has_file_output(
        _compositor_tree(scene), set())


def _compositor_tree(scene):
    if not scene.render.use_compositing:
        return None
    tree = getattr(scene, "compositing_node_group", None)        # 5.0+
    if tree is None and getattr(scene, "use_nodes", False):
        tree = getattr(scene, "node_tree", None)                 # 4.x
    return tree


def scene_info(scene):
    """What is being rendered, for the queue window's header.

    Captured in Blender because the queue process never opens the .blend. The
    view layers are the point: "which layers am I actually rendering" is not
    answerable from the command line, and a layer accidentally left disabled
    is the kind of thing worth seeing before a 2700-frame job finishes.
    """
    img = scene.render.image_settings
    engine = scene.render.engine
    samples = None
    if engine == 'CYCLES' and hasattr(scene, "cycles"):
        samples = getattr(scene.cycles, "samples", None)
    else:
        samples = getattr(scene.eevee, "taa_render_samples", None) if hasattr(scene, "eevee") else None
    pct = scene.render.resolution_percentage
    return {
        # the window tells launches apart by it once a second scene joins
        "name": scene.name,
        "engine": engine,
        "res": "%d x %d" % (scene.render.resolution_x * pct // 100,
                            scene.render.resolution_y * pct // 100),
        "res_pct": pct,
        "format": getattr(img, "file_format", ""),
        "media": getattr(img, "media_type", ""),
        "depth": getattr(img, "color_depth", ""),
        "color": getattr(img, "color_mode", ""),
        "samples": samples,
        "fps": scene.render.fps,
        "step": max(1, scene.frame_step),
        "view_layers": [{"name": vl.name, "on": bool(vl.use)}
                        for vl in scene.view_layers],
        "save_output": scene_saves_output(scene),
    }


def _slot_prefixes(node):
    """Path prefixes one File Output node writes, as `folder/start-of-name`.

    Blender appends the frame number to whatever the slot is called, so a slot
    named `Crusher_####` writes `Crusher_0481.jpg`: cutting at the first '#'
    leaves exactly the prefix `output_files()` already globs for, the same
    shape as the main render's -o value.

    `directory` is resolved through bpy.path.abspath because '//relative'
    paths are the norm here, and the viewer needs a real path to look in.
    """
    directory = bpy.path.abspath(getattr(node, "directory", "") or "")
    if not directory:
        return []
    items = list(getattr(node, "file_output_items", None) or [])
    names = [getattr(it, "name", "") for it in items] if items else []
    if not names:
        names = [getattr(node, "file_name", "") or ""]
    out = []
    node_label = (getattr(node, "label", "") or getattr(node, "name", "")
                  or "File Output")
    for name in names:
        # Cut at the first '#' OR '{': '####' is the frame number, and names
        # like '{blend_name}' are template tokens Blender expands at write
        # time. Measured on 5.2.2: a freshly added File Output node has NO
        # file_output_items and its file_name is literally '{blend_name}', so
        # a prefix built without this would never match a single file.
        stem = (name or "")
        for cut in ("#", "{"):
            stem = stem.split(cut)[0]
        # An empty stem is fine: it globs the node's whole folder, which is
        # where those files go. Over-inclusive beats listing nothing.
        # Labelled with the node (and slot, when it has one) so the window can
        # say WHICH output a folder belongs to instead of listing bare paths.
        label = node_label
        if name and len(names) > 1:
            label = "%s  ·  %s" % (node_label, name)
        out.append({"prefix": os.path.join(directory, stem), "label": label})
    return out


def compositor_output_prefixes(scene):
    """Every path prefix the active File Output nodes write to.

    The main render's -o is not the whole story: with Save Output off these
    nodes are the ONLY thing that writes, and the viewer listed nothing at all
    for such a render before 5.9.0.
    """
    tree = _compositor_tree(scene)
    found, seen = [], set()

    def walk(t):
        if t is None or t.name_full in seen:
            return
        seen.add(t.name_full)
        for node in t.nodes:
            if node.mute:
                continue
            if node.bl_idname == 'CompositorNodeOutputFile':
                found.extend(_slot_prefixes(node))
            walk(getattr(node, "node_tree", None))

    walk(tree)
    # Stable order, no duplicates: two slots can share a folder.
    seen_prefix, result = set(), []
    for entry in sorted(found, key=lambda e: (e["prefix"], e["label"])):
        if entry["prefix"] and entry["prefix"] not in seen_prefix:
            seen_prefix.add(entry["prefix"])
            result.append(entry)
    return result


def scene_is_video(scene):
    img = scene.render.image_settings
    return jobs.is_video_output(getattr(img, "media_type", None), img.file_format)


# --------------------------------------------------------------------------
# Launching
# --------------------------------------------------------------------------

def _free_port():
    """Ask the OS for a free local port and hand it to the queue.

    Blender picks it rather than the queue so the URL is known HERE, and can be
    reported in the status bar: with the queue console hidden there is nowhere
    else the address could appear. The queue falls back to any free port if
    this one is taken in the moment between closing and re-binding it, so a
    lost race costs the status-bar link, never the render.
    """
    import socket
    try:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]
    except OSError:
        return 0


def _open_queue_log(path):
    """A file for a hidden queue's output, or None if it cannot be made.

    Everything the queue console would have shown goes here instead - the
    window address, which job started when, GPU alarms, the final tally - so
    hiding the console loses nothing, it just stops it interrupting.
    """
    if not path:
        return None
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return open(path, "a", encoding="utf-8", buffering=1)
    except OSError:
        return None


def open_in_terminal(cmd, minimized=False, hidden=False, log_path=None):
    """Start `cmd` in its own terminal window, detached from Blender.

    `hidden` gives it no console at all - used when the queue window is on,
    because the queue's own console then shows nothing the window does not.
    SW_SHOWMINNOACTIVE was not enough: Windows restores a minimized console
    often enough that it still interrupts.

    A hidden process has NO usable stdout, so one is always provided: without
    it the first `print()` in the queue raises and the whole queue dies with
    nowhere to report it.
    """
    if sys.platform == "win32":
        if hidden:
            out = _open_queue_log(log_path)
            return subprocess.Popen(
                cmd, creationflags=subprocess.CREATE_NO_WINDOW,
                stdout=out or subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL)
        info = None
        if minimized:
            info = subprocess.STARTUPINFO()
            info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            info.wShowWindow = 7            # SW_SHOWMINNOACTIVE
        return subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_CONSOLE,
                                startupinfo=info)
    if hidden:
        out = _open_queue_log(log_path)
        return subprocess.Popen(cmd, start_new_session=True,
                                stdout=out or subprocess.DEVNULL,
                                stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL)
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


def launch_queue(job_list, parallel, cleanup, level='CLEAN', gpu_warn=66, gpu_stop=90,
                 web_ui=True, web_ui_open=True, web_ui_port=0, queue_log=None,
                 scene_info_dict=None, blend=None):
    spec = {
        # The original .blend: later launches of the same file join this
        # queue instead of opening a second one (jobs.offer_to_queue).
        "blend": blend,
        "jobs": job_list,
        "parallel": parallel,
        "level": level,
        "gpu_warn": gpu_warn,
        "gpu_stop": gpu_stop,
        "web_ui": web_ui,
        "web_ui_open": web_ui_open,
        "web_ui_port": web_ui_port,
        "scene": scene_info_dict,
        "console_per_job": sys.platform == "win32",
        "cleanup": cleanup,
    }
    fd, jobfile = tempfile.mkstemp(prefix="cli_render_queue_", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(spec, fh, ensure_ascii=False, indent=1)
    # With the queue window on, the queue's own console is hidden entirely:
    # it would only repeat what the window already shows. Each render still
    # opens its own console (banner, progress, VRAM gauge) exactly as before.
    open_in_terminal(runner_command(jobfile), minimized=True,
                     hidden=bool(web_ui), log_path=queue_log)


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
        web_ui = prefs.queue_window if prefs else True
        web_ui_open = prefs.queue_window_open if prefs else True
        run_stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        # Same for every job in the queue: they all render the one scene, so
        # the File Output nodes are the same nodes writing to the same folders.
        comp_prefixes = compositor_output_prefixes(scene)
        info = scene_info(scene)
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
                # -S: the scene the button was pressed in. Without it Blender
                # renders the scene that was active when the file was saved.
                "cmd": jobs.render_command(bpy.app.binary_path, render_blend,
                                           start, end, output_path, scene.name),
                # the frames Blender will render (-a honours the frame step)
                "frames": list(range(start, end + 1, step)),
                "log": log,
                # Where the compositor's File Output nodes write. Captured HERE,
                # from the live scene, because the queue process never opens the
                # .blend and cannot discover them. With Save Output off these are
                # the only files a render produces.
                "extra_outputs": comp_prefixes,
                # per job: a joined launch can come from another scene
                "scene": info,
            })

        parallel = scene.cli_parallel_jobs if use_subscenes else 1

        # --- a queue already running for this .blend takes these jobs as its
        # own batch: same window, one GPU guard over every job of the file.
        # Whether they start now or wait is the queue's GPU admission rule.
        joined = jobs.offer_to_queue(blend_path, {
            "jobs": job_list, "parallel": parallel, "cleanup": cleanup})
        if joined is not None:
            where = ("  Queue window: " + joined["url"]) if joined.get("url") else ""
            self.report({'INFO'}, f"Added {len(job_list)} render job(s) to the queue "
                                  f"already running for this file - they start as soon "
                                  f"as GPU memory allows.{where}")
            return {'FINISHED'}

        port = _free_port() if web_ui else 0
        queue_log = os.path.join(log_dir, "%s_queue_%s.log" % (
            os.path.splitext(blend_file)[0], run_stamp)) if web_ui else None
        try:
            launch_queue(job_list, parallel, cleanup, level, gpu_warn, gpu_stop,
                         web_ui, web_ui_open, port, queue_log, info, blend_path)
        except OSError as exc:
            for path in cleanup:
                os.remove(path)
            self.report({'ERROR'}, f"Could not start the render queue: {exc}")
            return {'CANCELLED'}

        at_once = min(parallel, len(job_list))
        where = (f"  Queue window: http://127.0.0.1:{port}/" if (web_ui and port)
                 else "  Blender remains responsive.")
        self.report({'INFO'}, f"Launched {len(job_list)} render job(s), "
                              f"{at_once} at a time.{where}")
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
