# Gumroad listing — what is published

Record of the Gumroad product page for this add-on, so future sessions can update it
without re-discovering everything. **Update this file whenever the listing changes.**

Last updated: 2026-10-04. Prepared, not saved yet: the 5.14 text (below) and the
`cli_render_launcher-5.14.1.zip` download replacing 5.13.0. The 5.13 overrides text was saved by the owner.

## Product

| Field | Value |
|---|---|
| Edit page | https://gumroad.com/products/cpbec/edit |
| Public URL | https://blenderbenn.gumroad.com/l/renderfromCL (custom URL `renderfromCL`) |
| Name | render from command line (Blender addon) |
| Summary | A Blender extension (.zip): drag it into Blender to install. |
| Price | **Free** — pay what you want, $0 minimum, $5 suggested (was a joke $69.69 until 2026-10-02). *Keep it free* (owner's decision). |
| Content (download) | `cli_render_launcher-5.14.1.zip` only (prepared 2026-10-04) (older zips and the old `.py` removed) |
| Size (additional details) | filled in automatically by Gumroad from the files |

## Images

| Where | Image | Notes |
|---|---|---|
| Cover | `cli_render_cover.png` (1600×900) | Blender-style card: "v5.3 · Blender add-on", red CLI RENDER title, 4 selling points, real panel on the right. |
| Thumbnail | `cli_render_thumbnail.png` (1000×1000) | Since 2026-10-03, in the style of the owner's other thumbnails: "CLI / Render" in Quicksand Bold with a black outline, Blender logo with a white glow cut by the right edge, three render consoles behind. Source: `screenshots/raw/thumbnail_source.html`. Previous one (the real panel): `old_thumbnail_v5.3_panel_backup.png`. |
| Description | `cli_render_panel.png` | Full panel, demo SubScenes intro/closeup/orbit/outro. |
| Description | `cli_render_subscenes.png` | Close-up of the SubScenes list. |
| Description | `cli_render_overrides.png` | 5.13 panel: row icons (render result / camera / monkey), Overrides box open on "ending". |
| Description | `cli_render_output_off.png` | Output section with Save Output off. |
| Description | `queue_details_3outputs.jpg` | Queue window, "second part" open: mp4 + single-layer EXR list + jpg thumbnails, 2 view layers. Spares: `queue_overview_during.jpg`, `queue_job_details_during.jpg`, `queue_all_done.jpg`. |
| Description | `console_banner_preview.png` | The render console banner (logo + CLI RENDER + by Benjamin Davoult). |

All images live locally in `D:\blender\addon making\CLI render\screenshots\` (backups of the
old V6 cover/thumbnail there too). **Known gap:** the cover, thumbnail and panel screenshots
still show the header **v5.3.0** — they should be retaken with 5.12.3 installed. The queue-window
screenshots (2026-10-02, 5.12.3, crystals3.blend) are new and current.

## Description — structure and claims

1. Intro (bold): render from the command line in the background while you keep working.
   Second line: separate renders in their own consoles; you can close Blender, the queue keeps going.
2. Image: full panel.
3. **🚀 Version 5.14 — big update** — "grown a lot since this page was first written", free update
   in the Gumroad library. List (newest first): SubScene overrides (5.13; 5.14 adds "only the frames you list") ·
   queue window (5.14: Open folder, stays available after the render) · queue window · one queue per .blend ·
   renders the scene you launched from · VRAM gauge · clean render console · GPU memory protection · out-of-memory
   recovery · failed render keeps its console open · SubScenes at once · queue runs on its own ·
   render the current state · compositor-only renders · a subfolder per SubScene · cleaner file
   names · safety checks · proper Blender extension · macOS & Linux fixes.
4. **What's in the panel** (Properties › Render, red CLI RENDER panel):
   Name & frame range · SubScenes: batch your shots (+ image) · SubScene overrides (samples /
   every N frames / only frames (5.14, with the video note) / camera, "your .blend is never changed",
   row icons; + image) · Output & file names ·
   Compositor-only renders (+ image) · Render the current state · Safety checks.
4b. **The queue window** (5.12) — image `queue_details_3outputs.jpg` + bullets (jobs, progress,
   ETA, log, rendered frames; 5.14: "Open folder" button, stays available after the render until closed;
   127.0.0.1 only; on by default, switch in preferences). Safety checks list also names the Only Frames check.
5. **The render console** — banner image, an example console block (code block), bullets
   (progress, problems only, nothing lost, Clean/Detailed/Full in preferences).
   **GPU memory protection** subsection (alarm 66%, admission, pause at 90% + resume same frame,
   out-of-memory killed before a broken frame and retried alone). 5.12.2 wording: admission
   "judged on the last minute's peak", and after a pause "runs one render fewer at a time". Thresholds in preferences.
6. **Good to know** — Blender 4.2+ incl. 5.x, install by drag & drop; queue is its own process;
   images and videos; consoles per OS (developed/tested on Windows); GPU memory for several
   SubScenes; renders the scene you launched from; GPU features need `nvidia-smi` (NVIDIA), AMD/Intel say they cannot watch; free.

**Rule:** every claim must match the code. Removed in 5.3 because they were false on Blender 5.2:
"compositor activated automatically" and the File Output missing-folder warning.
The owner trimmed the "big update" list once: only list what is genuinely new or newly
documented; the older SubScene features are described under "What's in the panel".

## How the page is edited (tips for future sessions)

- Gumroad's description editor is TipTap: `document.querySelector('.ProseMirror').editor`
  exposes `getJSON()` / `commands.setContent(doc, true)`. Build the doc as JSON, put
  `[[IMG:name]]` placeholder paragraphs, then upload images at each placeholder through the
  editor's hidden image `<input type=file>` (select the placeholder text first). Remove the
  empty paragraph Gumroad leaves after each inserted image.
- Content tab: the tab link often needs a click by coordinates; add files through the hidden
  `input[type=file]` (accept = any); delete a file via the ⋮ handle left of its card → Delete.
- Cover: "Add cover" → the popover's "Computer files" label holds the file input; old covers
  are removed with the red × on the thumbnail. Thumbnail: "Remove", then an upload input appears.
  Cover/thumbnail changes may apply immediately (not only on Save) — keep backups.
- Never press **Save changes**: the owner reviews and saves.
