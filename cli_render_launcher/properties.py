import bpy
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
    # The list order IS the render order (the arrows move entries in the
    # collection), so no separate "order" field is kept in sync any more.
    name: bpy.props.StringProperty()
    start: bpy.props.IntProperty()
    end: bpy.props.IntProperty()
    selected: bpy.props.BoolProperty(name="Render", default=False)


CLASSES = (
    FrameRangeEntry,
)


def register():
    for cls in CLASSES:
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
    bpy.types.Scene.cli_parallel_jobs = bpy.props.IntProperty(
        name="SubScenes at once",
        description=("How many ticked SubScenes render at the same time. "
                     "1 = one by one. Every running render needs its own GPU memory"),
        default=1,
        min=1,
        soft_max=8,
        max=32
    )
    bpy.types.Scene.cli_use_snapshot = bpy.props.BoolProperty(
        name="Render current state",
        description=(
            "Save a temporary copy of the current state next to the .blend and "
            "render that copy: unsaved changes are included, and edits you make "
            "while the queue runs never leak into it. The copy is deleted when "
            "the queue ends. Off: render the .blend file as saved on disk"),
        default=True
    )
    bpy.types.Scene.framerange_entries = bpy.props.CollectionProperty(type=FrameRangeEntry)
    bpy.types.Scene.framerange_index = bpy.props.IntProperty(default=0)


def unregister():
    del bpy.types.Scene.framerange_index
    del bpy.types.Scene.framerange_entries
    del bpy.types.Scene.cli_use_snapshot
    del bpy.types.Scene.cli_parallel_jobs
    del bpy.types.Scene.preset_use_subfolder
    del bpy.types.Scene.use_presets
    del bpy.types.Scene.cli_include_framerange
    del bpy.types.Scene.cli_timestamp_mode
    del bpy.types.Scene.cli_output_directory
    del bpy.types.Scene.cli_end_frame
    del bpy.types.Scene.cli_start_frame
    del bpy.types.Scene.cli_base_name

    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
