"""Visual theme of the native form: palette, fonts, generated artwork and ttk styles.

Rounded controls are rendered once into small RGBA PNG images with the standard
library only, so the form needs no image packages. Large surfaces are canvas
shapes, which Tk draws anti-aliased at the display's native resolution.
"""
from __future__ import annotations

import base64
import math
from pathlib import Path
import struct
import tkinter as tk
from tkinter import font as tkfont, ttk
import zlib


BACKGROUND = "#F1F4F9"
SURFACE = "#FFFFFF"
SURFACE_HOVER = "#F5F7FB"
SURFACE_PRESSED = "#EBEFF6"
BORDER = "#E2E7F0"
BORDER_STRONG = "#CCD4E2"
SHADOW = "#E1E6EF"
TEXT = "#0F172A"
MUTED = "#5B6B82"
SUBTLE = "#94A3B8"
DISABLED_FILL = "#F1F3F7"
DISABLED_TEXT = "#A4AEC0"
ACCENT = "#2F5BEA"
ACCENT_HOVER = "#2449CF"
ACCENT_PRESSED = "#1D3BAA"
ACCENT_SOFT = "#ECF1FF"
ACCENT_TEXT = "#1E3FAE"
SUCCESS = "#16A34A"
WARNING = "#D97706"
DANGER = "#DC2626"
DANGER_SOFT = "#FEF1F1"
CONSOLE = "#0F1626"
CONSOLE_RAISED = "#1B2436"
CONSOLE_TEXT = "#D5DDEA"
CONSOLE_MUTED = "#7D8BA6"
HERO_STOPS = ("#1336C4", "#2F5BEA", "#6C47E6")
# Status dots in the hero, keyed by connection tone.
HERO_TONES = {"idle": "#CBD5E1", "busy": "#FCD34D", "ok": "#4ADE80", "warn": "#FDBA74"}
# Task status colours: dot in the first column and row text.
STATUS_TONES = {"queued": SUBTLE, "running": ACCENT, "done": SUCCESS, "partial": WARNING,
                "failed": DANGER, "stopped": MUTED, "off": "#CBD5E1"}
# Console colours for the bracketed prefix and the rest of a log line.
LOG_TONES = {"time": "#4F5C75", "plain": CONSOLE_TEXT, "saved": "#4ADE80", "quiet": "#64748B",
             "error": "#F87171", "error_body": "#FCA5A5", "warn": "#FBBF24", "cycle": "#A5B4FC"}

ICON_PATH = Path(__file__).resolve().parent.parent / "assets"


def _rgb(color: str) -> tuple[int, int, int]:
    value = color.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def mix(first: str, second: str, amount: float) -> str:
    """Blend two #rrggbb colours; ``amount`` 0 keeps the first, 1 gives the second."""
    start, end = _rgb(first), _rgb(second)
    return "#%02x%02x%02x" % tuple(round(a + (b - a) * amount) for a, b in zip(start, end))


def gradient(stops: tuple[str, ...], position: float) -> str:
    position = min(1.0, max(0.0, position)) * (len(stops) - 1)
    index = min(int(position), len(stops) - 2)
    return mix(stops[index], stops[index + 1], position - index)


# Shapes are signed distance functions: negative inside, positive outside.

def _box(left, top, right, bottom, radius):
    cx, cy = (left + right) / 2, (top + bottom) / 2
    hx, hy = (right - left) / 2 - radius, (bottom - top) / 2 - radius

    def distance(x, y):
        qx, qy = abs(x - cx) - hx, abs(y - cy) - hy
        return math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - radius
    return distance


def _circle(cx, cy, radius):
    return lambda x, y: math.hypot(x - cx, y - cy) - radius


def _stroke(points, width):
    segments = list(zip(points, points[1:]))

    def distance(x, y):
        best = math.inf
        for (ax, ay), (bx, by) in segments:
            dx, dy = bx - ax, by - ay
            t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)))
            best = min(best, math.hypot(x - ax - t * dx, y - ay - t * dy))
        return best - width / 2
    return distance


def _union(*shapes):
    return lambda x, y: min(shape(x, y) for shape in shapes)


def _png(width: int, height: int, rgba: bytes) -> str:
    def chunk(kind, payload):
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))
    rows = b"".join(b"\0" + rgba[row * width * 4:(row + 1) * width * 4] for row in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows, 9))
            + chunk(b"IEND", b""))
    return base64.b64encode(data).decode("ascii")


def _grow(rgba: bytes, width: int, height: int, grow) -> tuple[bytes, int, int]:
    """Repeat one uniform middle column and row until the image has ``grow`` size.

    ttk fills a stretched image element by tiling the image centre, and Tk on
    macOS redraws the whole window on every change: a centre a few pixels
    wide costs hundreds of image draws per widget on each hover or click.
    """
    new_width, new_height, column, row = grow
    stride = width * 4
    lines = []
    for index in range(height):
        line = rgba[index * stride:(index + 1) * stride]
        lines.append(line[:column * 4] + line[column * 4:column * 4 + 4] * (new_width - width + 1)
                     + line[column * 4 + 4:])
    lines = lines[:row] + [lines[row]] * (new_height - height + 1) + lines[row + 1:]
    return b"".join(lines), new_width, new_height


def render(width: int, height: int, layers, grow=None) -> str:
    """Composite ``(shape, colour, opacity)`` layers, bottom first, into base64 PNG data.

    ``grow`` is ``(width, height, column, row)`` for 9-slice elements.
    """
    prepared = [(shape, _rgb(color), opacity) for shape, color, opacity in layers]
    rgba = bytearray()
    for row in range(height):
        y = row + 0.5
        for column in range(width):
            x = column + 0.5
            red = green = blue = alpha = 0.0
            for shape, (r, g, b), opacity in prepared:
                coverage = min(1.0, max(0.0, 0.5 - shape(x, y))) * opacity
                if coverage:
                    keep = 1.0 - coverage
                    red, green, blue = r * coverage + red * keep, g * coverage + green * keep, b * coverage + blue * keep
                    alpha = coverage + alpha * keep
            if alpha:
                rgba += bytes((round(red / alpha), round(green / alpha), round(blue / alpha), round(alpha * 255)))
            else:
                rgba += b"\0\0\0\0"
    if grow is not None:
        rgba, width, height = _grow(bytes(rgba), width, height, grow)
    return _png(width, height, bytes(rgba))


def _panel(size, radius, fill, outline=None, line=1.0):
    """A rounded square with an optional inner outline, for 9-slice elements."""
    shape = _box(0, 0, size, size, radius)
    if outline is None:
        return [(shape, fill, 1.0)]
    return [(shape, outline, 1.0), (_box(line, line, size - line, size - line, radius - line), fill, 1.0)]


def _switch(on, track, knob="#FFFFFF"):
    center = 27.0 if on else 11.0
    return [(_box(0, 1, 38, 23, 11), track, 1.0),
            (_circle(center, 12.6, 9.0), "#0F172A", 0.10),
            (_circle(center, 12, 8.5), knob, 1.0)]


def _chevron(direction, color, width=16, height=12):
    cx, cy = width / 2, height / 2
    points = {"up": [(cx - 3.5, cy + 1.8), (cx, cy - 1.8), (cx + 3.5, cy + 1.8)],
              "down": [(cx - 3.5, cy - 1.8), (cx, cy + 1.8), (cx + 3.5, cy - 1.8)],
              "right": [(cx - 1.8, cy - 3.5), (cx + 1.8, cy), (cx - 1.8, cy + 3.5)]}[direction]
    return [(_stroke(points, 1.6), color, 1.0)]


def _folder(body, tab):
    return [(_box(1.5, 3, 8.5, 7.5, 1.5), tab, 1.0),
            (_box(1.5, 5, 16.5, 14.5, 2.0), body, 1.0),
            (_box(1.5, 6.5, 16.5, 14.5, 2.0), mix(body, "#FFFFFF", 0.18), 1.0)]


def _cloud(color):
    shape = _union(_circle(6.5, 10, 3.6), _circle(10.2, 7.6, 4.6), _circle(13.6, 10.3, 3.2),
                   _box(4, 9.5, 15.5, 13.6, 2.0))
    return [(shape, color, 1.0)]


# Image name -> (width, height, layers[, grow]). Rendered once per process and
# cached, because tests and repeated windows create several Tk interpreters.
def _artwork():
    accent_disabled = mix(ACCENT, "#FFFFFF", 0.55)
    wide = (640, 44, 9, 9)          # buttons and fields: border 9
    pill = (320, 30, 11, 11)        # chips: border 11
    vertical, horizontal = (10, 400, 5, 5), (400, 10, 5, 5)

    def panel(*args):
        return (24, 24, _panel(*args), wide)

    def flat(color, grow):
        return (10, 10, [(_box(-1, -1, 11, 11, 0), color, 1.0)], grow[:2] + (1, 1))

    def thumb(color, grow):
        if grow is vertical:
            return (10, 24, [(_box(2, 1, 8, 23, 3), color, 1.0)], grow)
        return (24, 10, [(_box(1, 2, 23, 8, 3), color, 1.0)], grow)

    art = {
        "button": panel(24, 7, SURFACE, BORDER_STRONG),
        "button-hover": panel(24, 7, SURFACE_HOVER, BORDER_STRONG),
        "button-pressed": panel(24, 7, SURFACE_PRESSED, BORDER_STRONG),
        "button-focus": panel(24, 7, SURFACE, ACCENT, 1.5),
        "button-disabled": panel(24, 7, DISABLED_FILL, BORDER),
        "accent": panel(24, 7, ACCENT),
        "accent-hover": panel(24, 7, ACCENT_HOVER),
        "accent-pressed": panel(24, 7, ACCENT_PRESSED),
        "accent-disabled": panel(24, 7, accent_disabled),
        "danger": panel(24, 7, SURFACE, "#F1B5B5"),
        "danger-hover": panel(24, 7, DANGER_SOFT, "#EC9C9C"),
        "danger-pressed": panel(24, 7, "#FCE3E3", "#EC9C9C"),
        "field": panel(24, 7, SURFACE, BORDER_STRONG),
        "field-focus": panel(24, 7, SURFACE, ACCENT, 1.6),
        "field-disabled": panel(24, 7, DISABLED_FILL, BORDER),
        "chip": (24, 24, _panel(24, 12, ACCENT_SOFT), pill),
        "console-chip": (24, 24, _panel(24, 12, CONSOLE_RAISED), pill),
        "badge": (24, 24, [(_circle(12, 12, 11.5), ACCENT_SOFT, 1.0)]),
        "switch-off": (46, 24, _switch(False, "#CBD3E0")),
        "switch-on": (46, 24, _switch(True, ACCENT)),
        "switch-off-disabled": (46, 24, _switch(False, "#E4E8EF", "#F8FAFC")),
        "switch-on-disabled": (46, 24, _switch(True, accent_disabled, "#F8FAFC")),
        "up": (16, 12, _chevron("up", MUTED)),
        "up-disabled": (16, 12, _chevron("up", DISABLED_TEXT)),
        "down": (16, 12, _chevron("down", MUTED)),
        "down-disabled": (16, 12, _chevron("down", DISABLED_TEXT)),
        "tree-closed": (18, 16, _chevron("right", SUBTLE, 18, 16)),
        "tree-open": (18, 16, _chevron("down", MUTED, 18, 16)),
        "tree-leaf": (18, 16, []),
        "folder": (22, 16, _folder("#6E9BFF", "#4F7BE8")),
        "cloud": (22, 16, _cloud(ACCENT)),
        "vthumb": thumb("#CDD4E0", vertical),
        "vthumb-active": thumb("#9AA7BC", vertical),
        "hthumb": thumb("#CDD4E0", horizontal),
        "hthumb-active": thumb("#9AA7BC", horizontal),
        "vtrough": flat(SURFACE, vertical),
        "htrough": flat(SURFACE, horizontal),
        "console-vtrough": flat(CONSOLE, vertical),
        "console-vthumb": thumb("#2E3A52", vertical),
        "console-vthumb-active": thumb("#46557A", vertical),
        "heading": (8, 8, [(_box(-1, -1, 9, 9, 0), SURFACE, 1.0), (_box(-1, 7, 9, 9, 0), BORDER, 1.0)],
                    (640, 40, 2, 2)),
        "bar-trough": (16, 8, [(_box(0, 0, 16, 8, 4), CONSOLE_RAISED, 1.0)], (640, 8, 4, 4)),
        "bar": (16, 8, [(_box(0, 0, 16, 8, 4), "#5B82FF", 1.0)], (640, 8, 4, 4)),
    }
    for tone, color in STATUS_TONES.items():
        art["dot-" + tone] = (20, 14, [(_circle(9, 7, 4.5), color, 1.0)] if tone != "off"
                              else [(_circle(9, 7, 4.5), color, 1.0), (_circle(9, 7, 3), SURFACE, 1.0)])
    return art


_RENDERED: dict[str, str] = {}
# Layouts start with a plain background fill: image elements have transparent
# rounded corners, and Tk does not clear a widget before drawing its layout.
_FILL = ("background", {"sticky": "nswe"})


def _image_data() -> dict[str, str]:
    if not _RENDERED:
        _RENDERED.update({name: render(*spec) for name, spec in _artwork().items()})
    return _RENDERED


def _natural_sizes() -> dict[str, tuple[int, int]]:
    """Drawn size of grown images: ttk otherwise requests the whole image size."""
    return {name: spec[:2] for name, spec in _artwork().items() if len(spec) > 3}


def _make_fonts(root) -> dict[str, tkfont.Font]:
    default = tkfont.nametofont("TkDefaultFont", root=root)
    family = default.actual("family")
    available = set(tkfont.families(root))
    mono = next((name for name in ("SF Mono", "Menlo", "Cascadia Mono", "Consolas", "DejaVu Sans Mono")
                 if name in available), tkfont.nametofont("TkFixedFont", root=root).actual("family"))
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
        tkfont.nametofont(name, root=root).configure(size=13)
    return {
        "base": tkfont.Font(root=root, family=family, size=13),
        "small": tkfont.Font(root=root, family=family, size=12),
        "small_bold": tkfont.Font(root=root, family=family, size=12, weight="bold"),
        "button": tkfont.Font(root=root, family=family, size=13),
        "button_bold": tkfont.Font(root=root, family=family, size=14, weight="bold"),
        "section": tkfont.Font(root=root, family=family, size=15, weight="bold"),
        "title": tkfont.Font(root=root, family=family, size=20, weight="bold"),
        "subtitle": tkfont.Font(root=root, family=family, size=13),
        "mono": tkfont.Font(root=root, family=mono, size=11),
    }


def _load_icon(root, name):
    try:
        return tk.PhotoImage(master=root, file=str(ICON_PATH / name))
    except tk.TclError:
        return None


class Theme:
    """Styles for one Tk interpreter. Keep a reference: Tk images must stay alive."""

    def __init__(self, root: tk.Misc):
        # No reference back to the root: the root owns the theme.
        self.fonts = _make_fonts(root)
        self.images = {name: tk.PhotoImage(master=root, data=data) for name, data in _image_data().items()}
        self.natural_sizes = _natural_sizes()
        self.app_icon = _load_icon(root, "app-icon-256.png")
        self.hero_icon = _load_icon(root, "app-icon-hero.png")
        self.style = ttk.Style(root)
        self.style.theme_use("clam")
        self._configure()

    def _element(self, name, default, *states, **options):
        image = self.images
        specs = [(state, image[value]) for state, value in states]
        if default in self.natural_sizes:
            options.setdefault("width", self.natural_sizes[default][0])
            options.setdefault("height", self.natural_sizes[default][1])
        self.style.element_create(name, "image", image[default], *specs, **options)

    def _configure(self):
        style, fonts = self.style, self.fonts
        style.configure(".", background=SURFACE, foreground=TEXT, font=fonts["base"], bordercolor=BORDER,
                        lightcolor=SURFACE, darkcolor=SURFACE, troughcolor=SURFACE, focuscolor=ACCENT,
                        selectbackground=ACCENT_SOFT, selectforeground=TEXT, insertcolor=TEXT,
                        fieldbackground=SURFACE)
        style.configure("TFrame", background=SURFACE)
        style.configure("App.TFrame", background=BACKGROUND)
        style.configure("Console.TFrame", background=CONSOLE)

        style.configure("TLabel", background=SURFACE, foreground=TEXT, font=fonts["base"])
        style.configure("Muted.TLabel", foreground=MUTED, font=fonts["small"])
        style.configure("Field.TLabel", foreground=MUTED, font=fonts["base"])
        style.configure("Section.TLabel", font=fonts["section"])
        style.configure("Empty.TLabel", foreground=SUBTLE, font=fonts["base"], justify="center", anchor="center")
        style.configure("Badge.TLabel", foreground=ACCENT_TEXT, font=fonts["small_bold"], anchor="center")
        style.configure("Console.TLabel", background=CONSOLE, foreground=CONSOLE_TEXT, font=fonts["base"])
        style.configure("ConsoleTitle.TLabel", background=CONSOLE, foreground="#FFFFFF", font=fonts["section"])
        style.configure("ConsoleMuted.TLabel", background=CONSOLE, foreground=CONSOLE_MUTED, font=fonts["small"])
        for name, image, foreground, background in (("Chip", "chip", ACCENT_TEXT, SURFACE),
                                                    ("ConsoleChip", "console-chip", "#B4C6FF", CONSOLE)):
            self._element(name + ".chip", image, border=11, padding=0, sticky="nswe")
            style.layout(name + ".TLabel", [_FILL, (name + ".chip", {"sticky": "nswe", "children": [
                ("Label.padding", {"sticky": "nswe", "children": [("Label.label", {"sticky": "nswe"})]})]})])
            style.configure(name + ".TLabel", foreground=foreground, background=background,
                            font=fonts["small_bold"], padding=(12, 4))

        for variant, image in (("TButton", "button"), ("Accent.TButton", "accent"), ("Danger.TButton", "danger")):
            element = variant.replace("TButton", "") + "Button.bg"
            states = [("disabled", (image if image != "danger" else "button") + "-disabled"),
                      ("pressed", image + "-pressed"), ("active", image + "-hover")]
            if image == "button":
                states.append(("focus", "button-focus"))
            self._element(element, image, *states, border=9, padding=0, sticky="nswe")
            style.layout(variant, [_FILL, (element, {"sticky": "nswe", "children": [
                ("Button.padding", {"sticky": "nswe", "children": [("Button.label", {"sticky": "nswe"})]})]})])
        style.configure("TButton", padding=(12, 7), font=fonts["button"], foreground=TEXT, anchor="center")
        style.map("TButton", foreground=[("disabled", DISABLED_TEXT)])
        style.configure("Accent.TButton", foreground="#FFFFFF")
        style.map("Accent.TButton", foreground=[("disabled", "#FFFFFF")])
        style.configure("Danger.TButton", foreground=DANGER)
        style.map("Danger.TButton", foreground=[("disabled", DISABLED_TEXT)])
        style.configure("Large.Accent.TButton", padding=(22, 8), font=fonts["button_bold"])
        style.configure("Large.Danger.TButton", padding=(18, 8), font=fonts["button_bold"])

        self._element("Round.field", "field", ("disabled", "field-disabled"), ("focus", "field-focus"),
                      border=9, padding=2, sticky="nswe")
        style.layout("TEntry", [_FILL, ("Round.field", {"sticky": "nswe", "children": [
            ("Entry.padding", {"sticky": "nswe", "children": [("Entry.textarea", {"sticky": "nswe"})]})]})])
        style.configure("TEntry", padding=(10, 6), foreground=TEXT, insertcolor=TEXT,
                        selectbackground=ACCENT_SOFT, selectforeground=TEXT)
        style.map("TEntry", foreground=[("disabled", DISABLED_TEXT)])
        self._element("Chevron.uparrow", "up", ("disabled", "up-disabled"), sticky="e")
        self._element("Chevron.downarrow", "down", ("disabled", "down-disabled"), sticky="e")
        style.layout("TSpinbox", [_FILL, ("Round.field", {"side": "top", "sticky": "we", "children": [
            ("null", {"side": "right", "sticky": "", "children": [
                ("Chevron.uparrow", {"side": "top", "sticky": "e"}),
                ("Chevron.downarrow", {"side": "bottom", "sticky": "e"})]}),
            ("Spinbox.padding", {"sticky": "nswe", "children": [("Spinbox.textarea", {"sticky": "nswe"})]})]})])
        style.configure("TSpinbox", padding=(10, 5, 4, 5), foreground=TEXT, insertcolor=TEXT,
                        selectbackground=ACCENT_SOFT, selectforeground=TEXT)
        style.map("TSpinbox", foreground=[("disabled", DISABLED_TEXT)])

        self._element("Switch.indicator", "switch-off", ("disabled selected", "switch-on-disabled"),
                      ("disabled", "switch-off-disabled"), ("selected", "switch-on"), sticky="")
        style.layout("TCheckbutton", [_FILL, ("Checkbutton.padding", {"sticky": "nswe", "children": [
            ("Switch.indicator", {"side": "left", "sticky": ""}),
            ("Checkbutton.label", {"side": "left", "sticky": "nswe"})]})])
        style.configure("TCheckbutton", background=SURFACE, foreground=TEXT, padding=(0, 3))
        style.map("TCheckbutton", background=[("active", SURFACE)], foreground=[("disabled", DISABLED_TEXT)])

        for prefix, orient, trough, thumb in (("", "Vertical", "vtrough", "vthumb"),
                                              ("", "Horizontal", "htrough", "hthumb"),
                                              ("Console.", "Vertical", "console-vtrough", "console-vthumb")):
            element = f"{prefix}Slim.{orient}"
            self._element(element + ".trough", trough, border=1, sticky="ns" if orient == "Vertical" else "we")
            self._element(element + ".thumb", thumb, ("pressed", thumb + "-active"),
                          ("active", thumb + "-active"), border=5, sticky="nswe")
            style.layout(f"{prefix}{orient}.TScrollbar", [(element + ".trough", {"sticky": "nswe", "children": [
                (element + ".thumb", {"expand": "1", "sticky": "nswe"})]})])

        self._element("Bar.trough", "bar-trough", border=4, padding=0, sticky="nswe")
        self._element("Bar.pbar", "bar", border=4, padding=0, sticky="nswe")
        style.layout("Horizontal.TProgressbar", [_FILL, ("Bar.trough", {"sticky": "nswe", "children": [
            ("Bar.pbar", {"side": "left", "sticky": "ns"})]})])
        style.configure("Horizontal.TProgressbar", background=CONSOLE, thickness=8)

        style.configure("Treeview", background=SURFACE, fieldbackground=SURFACE, foreground=TEXT,
                        rowheight=30, borderwidth=0, font=fonts["base"], indent=16)
        style.map("Treeview", background=[("selected", ACCENT_SOFT)], foreground=[("selected", ACCENT_TEXT)])
        style.layout("Treeview", [_FILL, ("Treeview.treearea", {"sticky": "nswe"})])
        self._element("Chevron.indicator", "tree-closed", ("user2", "tree-leaf"), ("user1", "tree-open"),
                      sticky="")
        style.layout("Treeview.Item", [("Treeitem.padding", {"sticky": "nswe", "children": [
            ("Chevron.indicator", {"side": "left", "sticky": ""}),
            ("Treeitem.image", {"side": "left", "sticky": ""}),
            ("Treeitem.text", {"side": "left", "sticky": ""})]})])
        style.layout("Tasks.Treeview.Item", [("Treeitem.padding", {"sticky": "nswe", "children": [
            ("Treeitem.image", {"side": "left", "sticky": ""}),
            ("Treeitem.text", {"side": "left", "sticky": ""})]})])
        self._element("Slim.heading", "heading", border=2, padding=0, sticky="nswe")
        style.layout("Treeview.Heading", [("Slim.heading", {"sticky": "nswe", "children": [
            ("Treeheading.padding", {"sticky": "nswe", "children": [
                ("Treeheading.image", {"side": "right", "sticky": ""}),
                ("Treeheading.text", {"sticky": "we"})]})]})])
        style.configure("Treeview.Heading", foreground=MUTED, font=fonts["small_bold"], padding=(8, 6))

        style.configure("TSeparator", background=BORDER)
        style.configure("TPanedwindow", background=BACKGROUND)
        style.configure("Sash", sashthickness=16, gripcount=0, background=BACKGROUND,
                        bordercolor=BACKGROUND, lightcolor=BACKGROUND, darkcolor=BACKGROUND)


def theme_for(root: tk.Misc) -> Theme:
    """Install the theme once per window, like the clipboard support."""
    theme = getattr(root, "_disk_downloader_theme", None)
    if theme is None:
        theme = Theme(root)
        root._disk_downloader_theme = theme
    return theme


def _round_points(x1, y1, x2, y2, radius, steps=8):
    radius = max(0.0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    points = []
    for cx, cy, start in ((x2 - radius, y1 + radius, -90), (x2 - radius, y2 - radius, 0),
                          (x1 + radius, y2 - radius, 90), (x1 + radius, y1 + radius, 180)):
        for step in range(steps + 1):
            angle = math.radians(start + 90 * step / steps)
            points += (cx + radius * math.cos(angle), cy + radius * math.sin(angle))
    return points


class Card(tk.Canvas):
    """A rounded surface with a soft drop shadow; place content into ``body``."""

    def __init__(self, parent, *, fill=SURFACE, outline=BORDER, backdrop=BACKGROUND, radius=16,
                 padding=(18, 16), shadow=True, body_style="TFrame"):
        super().__init__(parent, background=backdrop, highlightthickness=0, borderwidth=0, width=1, height=1)
        self.fill, self.outline, self.radius, self.shadow = fill, outline, radius, shadow
        self.shadow_depth = 3 if shadow else 0
        self.body = ttk.Frame(self, style=body_style)
        horizontal, vertical = padding
        self.body.pack(fill="both", expand=True, padx=horizontal, pady=(vertical, vertical + self.shadow_depth))
        self.bind("<Configure>", self._draw)

    def _draw(self, _event=None):
        self.delete("surface")
        width, height = self.winfo_width(), self.winfo_height()
        if width < 4 or height < 4:
            return
        bottom = height - 1 - self.shadow_depth
        if self.shadow:
            self.create_polygon(_round_points(1, 2, width - 1, height - 0.5, self.radius),
                                fill=SHADOW, outline="", tags="surface")
        self.create_polygon(_round_points(0.5, 0.5, width - 0.5, bottom, self.radius),
                            fill=self.fill, outline=self.outline, tags="surface")
        self.tag_lower("surface")


class Hero(tk.Canvas):
    """The gradient banner with the app icon, title and a connection status pill."""

    def __init__(self, parent, theme: Theme, *, title, subtitle, status_var: tk.StringVar, height=78):
        super().__init__(parent, height=height, highlightthickness=0, borderwidth=0, background=HERO_STOPS[0])
        self.theme, self.title, self.subtitle, self.status_var = theme, title, subtitle, status_var
        self.tone = "idle"
        self.bind("<Configure>", self._draw)
        status_var.trace_add("write", lambda *_: self._draw_status())

    def set_tone(self, tone):
        self.tone = tone
        self._draw_status()

    def _draw(self, _event=None):
        self.delete("all")
        width, height = self.winfo_width(), self.winfo_height()
        for x in range(0, width, 2):
            self.create_rectangle(x, 0, x + 2, height, fill=gradient(HERO_STOPS, x / max(width, 1)), width=0)
        # Soft discs give the banner depth without images.
        for cx, cy, radius, amount in ((width - 150, -30, 150, 0.08), (width - 360, height + 40, 110, 0.06),
                                       (width * 0.42, -70, 95, 0.05)):
            base = gradient(HERO_STOPS, cx / max(width, 1))
            self.create_oval(cx - radius, cy - radius, cx + radius, cy + radius,
                             fill=mix(base, "#FFFFFF", amount), width=0)
        x = 28
        if self.theme.hero_icon is not None:
            self.create_image(x, height / 2, image=self.theme.hero_icon, anchor="w")
            x += self.theme.hero_icon.width() + 16
        self.create_text(x, height / 2 - 10, text=self.title, anchor="w", fill="#FFFFFF",
                         font=self.theme.fonts["title"])
        self.create_text(x + 1, height / 2 + 13, text=self.subtitle, anchor="w", fill="#DCE5FF",
                         font=self.theme.fonts["subtitle"])
        self._draw_status()

    def _draw_status(self):
        self.delete("status")
        width, height = self.winfo_width(), self.winfo_height()
        text = self.status_var.get()
        if width < 4 or not text:
            return
        font = self.theme.fonts["small_bold"]
        pill_height = 30
        right = width - 28
        left = right - font.measure(text) - 46
        top = height / 2 - pill_height / 2
        base = gradient(HERO_STOPS, (left + right) / 2 / max(width, 1))
        self.create_polygon(_round_points(left, top, right, top + pill_height, pill_height / 2),
                            fill=mix(base, "#FFFFFF", 0.16), outline=mix(base, "#FFFFFF", 0.32), tags="status")
        dot = HERO_TONES.get(self.tone, HERO_TONES["idle"])
        self.create_oval(left + 15, height / 2 - 4, left + 23, height / 2 + 4, fill=dot, width=0, tags="status")
        self.create_text(left + 32, height / 2, text=text, anchor="w", fill="#FFFFFF", font=font, tags="status")


class Tooltip:
    """A small delayed hint for a widget."""

    def __init__(self, widget, text, delay=450):
        self.widget, self.text, self.delay = widget, text, delay
        self.window = None
        self.pending = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None):
        self._hide()
        self.pending = self.widget.after(self.delay, self._show)

    def _show(self):
        self.pending = None
        try:
            x = self.widget.winfo_rootx() + 8
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
            self.window = tk.Toplevel(self.widget)
            self.window.wm_overrideredirect(True)
            if self.window.tk.call("tk", "windowingsystem") == "aqua":
                # As in IDLE's tooltips: otherwise the hint becomes the key
                # window, and the next click only re-activates the form.
                self.window.tk.call("::tk::unsupported::MacWindowStyle", "style", self.window._w,
                                    "help", "noActivates")
            self.window.wm_geometry(f"+{x}+{y}")
            tk.Label(self.window, text=self.text, justify="left", wraplength=320, background=TEXT,
                     foreground="#F1F5F9", padx=10, pady=6, font=theme_for(self.widget.winfo_toplevel()).fonts["small"]
                     ).pack()
        except tk.TclError:
            self.window = None

    def _hide(self, _event=None):
        if self.pending is not None:
            self.widget.after_cancel(self.pending)
            self.pending = None
        if self.window is not None:
            try:
                self.window.destroy()
            except tk.TclError:
                pass
            self.window = None
