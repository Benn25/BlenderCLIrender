"""The Blender logo in console characters, shown when a render console opens.

Traced from the official Blender icon and stored as a pixel grid ('O'
orange, 'W' white, 'B' blue, ' ' empty). Each console cell shows TWO grid
pixels with the half-block character (top = foreground colour, bottom =
background colour), which doubles the vertical resolution. Where ANSI
colours are not available, the silhouette is drawn with plain half blocks.

No bpy imports.
"""
import os
import re
import sys

GRID = [
    '                        OOOO                      ',
    '                        OOOOO                     ',
    '                       OOOOOOOO                   ',
    '                        OOOOOOOO                  ',
    '                         OOOOOOOO                 ',
    '                          OOOOOOOO                ',
    '                           OOOOOOOOO              ',
    '                             OOOOOOOO             ',
    '                              OOOOOOOO            ',
    '         OOOOOOOOOOOOOOOOOOOOOOOOOOOOOOO          ',
    '        OOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOO         ',
    '       OOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOO        ',
    '       OOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOO       ',
    '        OOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOO      ',
    '                   OOOOOOOOOOOOWWWOOOOOOOOOOOO    ',
    '                  OOOOOOOOOOWWWWWWWWWWOOOOOOOO    ',
    '                 OOOOOOOOOWWWWWWWWWWWWWOOOOOOOO   ',
    '                OOOOOOOOOWWWWWWWWWWWWWWWOOOOOOOO  ',
    '              OOOOOOOOOOWWWWWBBBBBBBWWWWWOOOOOOO  ',
    '             OOOOOOOOOOWWWWWBBBBBBBBBBWWWWOOOOOOO ',
    '            OOOOOOOOOOOWWWWBBBBBBBBBBBWWWWOOOOOOO ',
    '           OOOOOOOOOOOWWWWWBBBBBBBBBBBBWWWWOOOOOOO',
    '         OOOOOOOOOOOOOWWWWBBBBBBBBBBBBBWWWWOOOOOOO',
    '        OOOOOOOOOOOOOOWWWWBBBBBBBBBBBBBBWWWOOOOOOO',
    '       OOOOOOOOOOOOOOOWWWWBBBBBBBBBBBBBBWWWOOOOOOO',
    '      OOOOOOOOOOOOOOOOWWWWBBBBBBBBBBBBBWWWWOOOOOOO',
    '    OOOOOOOOOO OOOOOOOWWWWWBBBBBBBBBBBBWWWWOOOOOOO',
    '   OOOOOOOOOO  OOOOOOOWWWWWBBBBBBBBBBBWWWWWOOOOOOO',
    '  OOOOOOOOOO   OOOOOOOOWWWWWBBBBBBBBBBWWWWOOOOOOOO',
    ' OOOOOOOOOO     OOOOOOOOWWWWWWBBBBBBWWWWWWOOOOOOOO',
    'OOOOOOOOO       OOOOOOOOWWWWWWWWWWWWWWWWWOOOOOOOO ',
    'OOOOOOOO        OOOOOOOOOWWWWWWWWWWWWWWWOOOOOOOOO ',
    'OOOOOOO          OOOOOOOOOOWWWWWWWWWWWWOOOOOOOOO  ',
    'OOOOOO           OOOOOOOOOOOOWWWWWWWWOOOOOOOOOOO  ',
    '  OO              OOOOOOOOOOOOOOOOOOOOOOOOOOOOO   ',
    '                   OOOOOOOOOOOOOOOOOOOOOOOOOOOO   ',
    '                    OOOOOOOOOOOOOOOOOOOOOOOOOO    ',
    '                     OOOOOOOOOOOOOOOOOOOOOOOO     ',
    '                      OOOOOOOOOOOOOOOOOOOOO       ',
    '                       OOOOOOOOOOOOOOOOOOO        ',
    '                         OOOOOOOOOOOOOOO          ',
    '                            OOOOOOOOO             ',
]

ANSI = {"O": 208, "B": 25, "W": 231}      # 256-colour codes: orange, blue, white
GREY = "[38;5;245m"
RESET = "[0m"
UPPER, LOWER, FULL = "▀", "▄", "█"


def enable_colour(stream=None):
    """True if ANSI colours will show. Turns them on in Windows consoles."""
    stream = stream or sys.stdout
    try:
        if not stream.isatty():
            return False
    except Exception:
        return False
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        handle = k32.GetStdHandle(-11)                 # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not k32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(k32.SetConsoleMode(handle, mode.value | 0x0004))  # VT processing
    except Exception:
        return False


def addon_version():
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blender_manifest.toml")
        m = re.search(r'^version = "([^"]+)"', open(path, encoding="utf-8").read(), re.M)
        return m.group(1) if m else ""
    except OSError:
        return ""


# A 7-pixel-tall font for the title, drawn with the same half-block pixels as
# the logo so both read as one picture.
FONT = {   # bold 7-pixel-tall letters: double-width strokes, like the logo
    "C": [".####", "##...", "##...", "##...", "##...", "##...", ".####"],
    "L": ["##...", "##...", "##...", "##...", "##...", "##...", "#####"],
    "I": ["####", ".##.", ".##.", ".##.", ".##.", ".##.", "####"],
    "R": ["####.", "##.##", "##.##", "####.", "##.#.", "##.##", "##.##"],
    "E": ["#####", "##...", "##...", "####.", "##...", "##...", "#####"],
    "N": ["##..##", "###.##", "######", "##.###", "##..##", "##..##", "##..##"],
    "D": ["####.", "##.##", "##.##", "##.##", "##.##", "##.##", "####."],
    " ": ["..", "..", "..", "..", "..", "..", ".."],
}
# top-to-bottom shading of the title: yellow -> Blender orange -> deep orange
TITLE_SHADES = [220, 220, 214, 214, 208, 208, 202, 202]
AUTHOR = "by Benjamin Davoult"
GAP = 4                                     # columns between logo and title


def _title_grid(text):
    """Pixel rows (8 tall) for `text`; 'T' marks a lit pixel."""
    rows = [""] * 8
    for i, ch in enumerate(text.upper()):
        glyph = FONT.get(ch, FONT[" "])
        for y in range(7):
            rows[y] += glyph[y].replace("#", "T") + ("." if i < len(text) - 1 else "")
    rows[7] = "." * len(rows[0])
    return rows


def _cell_colour(top, bottom, ty=0):
    def code(c, y):
        return TITLE_SHADES[y] if c == "T" else ANSI[c]
    if top in " ." and bottom in " .":
        return RESET + " "
    if top in " .":
        return RESET + "[38;5;%dm" % code(bottom, ty + 1) + LOWER
    if bottom in " .":
        return RESET + "[38;5;%dm" % code(top, ty) + UPPER
    return "[38;5;%dm[48;5;%dm" % (code(top, ty), code(bottom, ty + 1)) + UPPER


def _cell_plain(top, bottom, ty=0):
    t, b = top in "OBT", bottom in "OBT"        # the white ring stays empty
    return FULL if t and b else UPPER if t else LOWER if b else " "


def lines(subtitle="", colour=False):
    """The logo, with the big CLI RENDER title, the author and `subtitle`
    (version / job) to its right, vertically centred."""
    logo_w = len(GRID[0])
    cell = _cell_colour if colour else _cell_plain
    title = _title_grid("CLI RENDER")
    title_w = len(title[0])
    n_rows = len(GRID) // 2

    rule = max(3, (title_w - len(AUTHOR) - 2) // 2)
    if colour:
        byline = ("[38;5;208m" + "━" * rule + RESET + " [1;38;5;231m" + AUTHOR
                  + RESET + " [38;5;208m" + "━" * rule + RESET)
        sub = GREY + subtitle.center(title_w).rstrip() + RESET if subtitle else ""
    else:
        byline = "─" * rule + " " + AUTHOR + " " + "─" * rule
        sub = subtitle.center(title_w).rstrip() if subtitle else ""
    # right-hand block: 4 title rows, blank, byline, blank, subtitle
    block = ["title"] * 4 + ["", byline, "", sub]
    first = (n_rows - len(block)) // 2

    out = []
    for r in range(n_rows):
        top, bottom = GRID[2 * r], GRID[2 * r + 1]
        text = "".join(cell(t, b) for t, b in zip(top, bottom))
        if colour:
            text += RESET
        k = r - first
        right = ""
        if 0 <= k < len(block):
            if block[k] == "title":
                ty = 2 * k
                right = "".join(cell(t, b, ty) for t, b in zip(title[ty], title[ty + 1]))
                if colour:
                    right += RESET
            else:
                right = block[k]
        if right:
            out.append("  " + text + " " * GAP + right)
        else:
            out.append(("  " + text).rstrip() if not colour else "  " + text)
    out.append("")
    return out
