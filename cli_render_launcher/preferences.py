"""Add-on preferences: how the render consoles look.

Kept out of the render panel on purpose - it is set once, not per render.
"""
import bpy

LEVEL_ITEMS = [
    ('CLEAN', "Clean",
     "One progress line per frame (frame, frames to go, rate, time left) and the "
     "render's problems, each shown once. Other add-ons' messages are only counted"),
    ('DETAILED', "Detailed",
     "Like Clean, but every warning and error is shown, including other "
     "add-ons' messages"),
    ('FULL', "Full",
     "Everything Blender prints, untouched (the behaviour before 5.4)"),
]


class CLIRENDER_AP_preferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    console_level: bpy.props.EnumProperty(
        name="Render Console",
        description="What the render console windows show",
        items=LEVEL_ITEMS,
        default='CLEAN',
    )
    keep_logs: bpy.props.BoolProperty(
        name="Keep full logs",
        description=(
            "Write everything Blender prints to a log file per render job, in a "
            "cli_render_logs folder next to the .blend. Nothing hidden by the "
            "console is ever lost"),
        default=True,
    )

    queue_window: bpy.props.BoolProperty(
        name="Queue window in the browser",
        description=(
            "Show the render queue as a page in your browser, served from the "
            "queue itself on localhost. The queue's own console window is then "
            "hidden - it would only repeat what the window shows - and what it "
            "would have printed goes to a log beside the render logs. Each "
            "render still gets its own console. Off: no window, console as before"),
        default=True,
    )
    queue_window_open: bpy.props.BoolProperty(
        name="Open it automatically",
        description=(
            "Open the browser when a queue starts. Off: the address is printed "
            "in the queue console and you open it when you want it"),
        default=True,
    )

    gpu_warn_percent: bpy.props.IntProperty(
        name="GPU memory alarm",
        description=(
            "Warn in the render consoles when the GPU's memory goes above this "
            "(whole card, all programs). Running out of GPU memory crashes or "
            "slows renders, especially with several SubScenes at once. "
            "0 = off. NVIDIA GPUs only"),
        default=66,
        min=0,
        max=100,
        subtype='PERCENTAGE',
    )

    gpu_stop_percent: bpy.props.IntProperty(
        name="Stop a render above",
        description=(
            "When several SubScenes render at once and the GPU's memory goes above "
            "this, one of them is stopped (it resumes later at the same frame) so "
            "the others do not run out of memory. 0 = never stop. NVIDIA GPUs only"),
        default=90,
        min=0,
        max=100,
        subtype='PERCENTAGE',
    )

    def draw(self, context):
        layout = self.layout
        layout.label(text="Render console:")
        layout.row().prop(self, "console_level", expand=True)
        col = layout.column(align=True)
        col.scale_y = 0.85
        for key, _name, desc in LEVEL_ITEMS:
            if key == self.console_level:
                col.label(text=desc.split(". ")[0] + ".", icon='INFO')
        layout.prop(self, "keep_logs")
        if self.keep_logs:
            sub = layout.column(align=True)
            sub.scale_y = 0.85
            sub.label(text="Logs go to a cli_render_logs folder next to your .blend.")
        sub = layout.column(align=True)
        sub.scale_y = 0.85
        sub.label(text="A render that fails keeps its console open, so you can see why.",
                  icon='ERROR')
        layout.separator()
        layout.prop(self, "queue_window")
        if self.queue_window:
            sub = layout.column(align=True)
            sub.scale_y = 0.85
            sub.prop(self, "queue_window_open")
            sub.label(text="The queue's own console is hidden; its output goes to a log.",
                      icon='INFO')
            sub.label(text="Each render keeps its own console. 127.0.0.1 only.",
                      icon='BLANK1')

        layout.separator()
        row = layout.row()
        row.prop(self, "gpu_warn_percent")
        row.label(text="off" if self.gpu_warn_percent == 0 else "whole card, NVIDIA only")
        row = layout.row()
        row.prop(self, "gpu_stop_percent")
        row.label(text="never" if self.gpu_stop_percent == 0 else "resumes later, same frame")
        col = layout.column(align=True)
        col.scale_y = 0.85
        col.label(text="New SubScenes start only below the alarm level. A render that runs",
                  icon='INFO')
        col.label(text="out of GPU memory is retried alone, from the frame that failed.")


def get_prefs(context):
    """The preferences, or None when the add-on runs without being enabled
    (e.g. loaded straight from source in tests)."""
    entry = context.preferences.addons.get(__package__)
    return entry.preferences if entry else None


_classes = (CLIRENDER_AP_preferences,)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
