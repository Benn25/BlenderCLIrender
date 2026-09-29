# BlenderCLIrender

**Render your animation from the command line, in the background, while you keep working in Blender.**

A Blender add-on (extension, Blender 4.2+ including 5.x) that launches separate background renders
from a panel in **Properties › Render**, with your frame ranges, file names and output folder.
Get the ready-to-install zip on [Gumroad](https://blenderbenn.gumroad.com/l/renderfromCL) (free, name a price if you like).

## Features

- **SubScenes** — save frame ranges as named presets, tick the ones you want and render them in one click,
  one by one or several at once (you choose how many). Optional subfolder per SubScene. The list order is the render order.
- **The queue runs on its own** — close Blender, the renders keep going.
- **Render the current state** — a temporary snapshot of the scene is rendered, so unsaved changes are
  included and edits made while the queue runs never leak into it. Deleted when the queue ends.
- **Compositor-only renders** — with Blender's *Save Output* off, only the compositor's File Output nodes write
  files; the add-on never modifies those nodes.
- **A clean render console** — each render opens its own console (with a Blender logo banner): one line per frame
  with frames to go, speed and time left; problems shown once; the noise from other add-ons hidden.
  The full raw log is kept in `cli_render_logs/` next to the `.blend`. A render that fails keeps its console open.
- **GPU memory protection** (NVIDIA, via `nvidia-smi`) — alarm above 66%, new SubScenes start only when there is room,
  above 90% one render pauses and later resumes at the same frame, and a render that runs out of GPU memory is
  killed before it can save a broken frame and retried alone from that frame.
- **Safety checks** before launching — file saved once, no backwards ranges (single frames are fine),
  output folder exists, SubScene folders can be created, and something will actually be written.
- **Clean file names** — `shot_0001-0100_0001.png`; videos are named by Blender only once.

## Install

Drag `cli_render_launcher-<version>.zip` into Blender, or *Edit › Preferences › Get Extensions › Install from Disk*.
To build the zip from this repository, zip the **contents** of `cli_render_launcher/` (the manifest must be at the zip root).

Console level (Clean / Detailed / Full), log files and the GPU thresholds are in the add-on's preferences.

## Repository layout

```
cli_render_launcher/   the add-on (Blender extension package)
  operators.py         the render button and the SubScene list operators
  panels.py            the panel in Properties > Render
  properties.py        scene settings
  preferences.py       add-on preferences (console level, logs, GPU thresholds)
  jobs.py              file names, command lines, validation (pure Python)
  runner.py            the render queue and per-render console watcher (runs outside Blender)
  scheduler.py         GPU-aware decisions: start / stop / retry (pure Python)
  console_filter.py    turns Blender's raw output into the clean console (pure Python)
  gpu_monitor.py       GPU memory through nvidia-smi (pure Python)
  banner.py            the Blender logo shown in each render console
tests/                 plain-Python tests + end-to-end scripts (see each file's docstring)
GUMROAD.md             what is published on the Gumroad product page
```

Run the fast tests with plain Python, e.g. `python tests/test_scheduler.py`.

## Author

Benjamin Davoult — GPL-3.0-or-later
