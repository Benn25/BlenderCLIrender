# Project: BlenderCLIrender
A Blender addon for batch rendering via command line launcher.

- Current version: **5.12.3**, a multi-file Blender extension in `cli_render_launcher/`
  (see README.md for the file-by-file layout; tests in `tests/`).
- The add-on is also sold (free) on Gumroad. **Read GUMROAD.md before touching the listing,
  and update it after any change to the listing.**

# My Working Preferences

## Who I am
- No command line experience — I do not use terminal commands myself
- I work across devices (PC, phone, browser)
- I have experience writing Arduino code and Blender addons, but in a basic/dirty way

## How I want Claude to work

### Git & GitHub
- Claude handles ALL git operations (commit, push, branch, etc.)
- I only do one thing on GitHub: click the banner → Create pull request → Merge
- Every logical change must be its own separate commit so I can accept or reject changes individually
- Never bundle unrelated changes into one commit

### File structure — mandatory on every session start
- At the start of every session, review the project file structure
- Identify what type of project it is (Blender addon, Arduino, etc.)
- Suggest what a standard/clean file structure should look like for that project type
- Wait for my explicit approval before moving, renaming, or creating any files

### Blender addon file structure rules
- Single file, under 500 lines → keep as one `.py` file, no folder, keep the addon name as the filename
- Single file, over 500 lines → convert to a folder with `__init__.py`, `operators.py`, `ui.py`, `utils.py`
- Already multi-file → wrap in a folder if not already done

### General
- Keep explanations simple — assume I am not a developer by trade
- Never ask me to run commands in a terminal
- If something needs doing, Claude does it
