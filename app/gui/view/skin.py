"""The look of every ttk control: rounded buttons, fields, check marks and cards, drawn as image elements.

Tk's built-in themes draw square, flat rectangles. Tk can do better without any dependency: a ttk "image" element
stretches a picture around fixed corners (nine-slice), and Tk 8.6 photo images carry an alpha channel. So this module
renders small anti-aliased rounded shapes into RGBA PNGs in memory (zlib + struct, nothing else), hands them to Tk as
photo images, and builds the button, field and indicator layouts on top of them. Every control keeps ttk's own
keyboard handling, focus traversal, states and accessibility; only how it is painted changes.

Each state has its own picture -- rest, hover, pressed, keyboard focus (a visible ring, never just a colour shift),
disabled -- and every picture has the same outer size, so a control never moves when its state changes. On a theme
change the same photo images are re-rendered in place (`PhotoImage.configure(data=...)`), so the elements, which are
created once per interpreter, follow automatically.

Pure drawing, no screen: `rounded_rgba()` and `encode_rgba_png()` are tested directly.
"""
from __future__ import annotations

import base64
import functools
import struct
import tkinter as tk
import zlib
from tkinter import ttk

from app.gui import theme as th

BUTTON_SIZE = (34, 34)      # the picture a button is stretched from; corners stay fixed
PILL_SIZE = (56, 34)        # a pill (chip) needs room beside its round ends for a stretchable middle
CHIP_LABEL_SIZE = (44, 30)
MIN_CENTRE = 4              # the stretched middle of a nine-slice picture: never empty (Tk cannot tile nothing)
MARGIN = 3                  # transparent room around every control picture, where the focus ring is drawn
FIELD_SIZE = (30, 30)
INDICATOR = 18
CARD_SIZE = (40, 40)
# Tk's image element TILES the stretchable middle of a picture: one copy per tile, each blended for its transparency.
# A 4- or 14-pixel middle on a full-screen card (3440 x 1440) meant tens of thousands of copies on every redraw --
# seconds per frame, the window "lagging" when maximised. So every stretched picture carries its middle already
# repeated out to at least STRETCH pixels (`widen_middle`): the same pixels, a few large copies. The ttk element
# still asks for the small size above, so no layout changes.
STRETCH = 256
FIELD_BORDER = (8, 8, 8, 8)
CARD_BORDER = (th.RADIUS_CARD + 1,) * 4
TOAST_BORDER = (11, 11, 11, 11)


# ---- pixels ----------------------------------------------------------------------------------------------------
def _hex(colour: str) -> tuple[int, int, int]:
    colour = colour.lstrip("#")
    return int(colour[0:2], 16), int(colour[2:4], 16), int(colour[4:6], 16)


def _coverage(width: int, height: int, inset: float, radius: float) -> list[float]:
    """For every pixel, how much of it the rounded rectangle inset by `inset` on all sides covers (0..1).

    Signed distance from the pixel centre to the shape's edge, turned into a one-pixel-wide ramp: the usual way to
    anti-alias a rounded rectangle, and one evaluation per pixel instead of a supersampled loop."""
    hx, hy = width / 2 - inset, height / 2 - inset
    if hx <= 0 or hy <= 0:
        return [0.0] * (width * height)
    r = max(0.0, min(radius, hx, hy))
    cx, cy = width / 2, height / 2
    out = []
    for y in range(height):
        qy = abs(y + 0.5 - cy) - (hy - r)
        for x in range(width):
            qx = abs(x + 0.5 - cx) - (hx - r)
            outside = (max(qx, 0.0) ** 2 + max(qy, 0.0) ** 2) ** 0.5
            distance = outside + min(max(qx, qy), 0.0) - r
            out.append(min(1.0, max(0.0, 0.5 - distance)))
    return out


@functools.lru_cache(maxsize=512)
def rounded_rgba(width: int, height: int, radius: float, fill: str | None, *, border: str | None = None,
                 border_width: float = 1.0, ring: str | None = None, ring_width: float = 2.0,
                 margin: float = 0.0) -> bytes:
    """RGBA bytes of a rounded rectangle: an optional focus `ring` in the outer `margin`, then the shape (corner
    radius `radius`), whose edge is `border` (if given) around `fill` (None: transparent inside). Every layer is
    concentric with the shape, so the ring follows its corners. Anti-aliased."""
    layers: list[tuple[float, str | None]] = []
    if ring is not None and margin > 0:
        layers.append((0.0, ring))                                  # the ring, from the outer edge ...
        layers.append((min(ring_width, margin - 0.5), None))        # ... a transparent gap down to the shape
    if border is not None:
        layers.append((margin, border))
        layers.append((margin + border_width, fill))
    else:
        layers.append((margin, fill))
    covers = [_coverage(width, height, inset, radius + margin - inset) for inset, _ in layers]
    colours = [None if colour is None else _hex(colour) for _, colour in layers]
    out = bytearray()
    for i in range(width * height):
        r = g = b = a = 0.0
        for index, colour in enumerate(colours):
            outer = covers[index][i]
            inner = covers[index + 1][i] if index + 1 < len(covers) else 0.0
            share = max(0.0, outer - inner)
            if colour is None or share <= 0:
                continue
            r += colour[0] * share
            g += colour[1] * share
            b += colour[2] * share
            a += share
        if a > 0:
            out += bytes((round(r / a), round(g / a), round(b / a), round(min(a, 1.0) * 255)))
        else:
            out += b"\x00\x00\x00\x00"
    return bytes(out)


def encode_rgba_png(width: int, height: int, rgba: bytes) -> bytes:
    """A minimal valid PNG (colour type 6, 8 bits, no interlace) of `rgba`."""
    if len(rgba) != width * height * 4:
        raise ValueError("rgba must hold width * height * 4 bytes.")
    raw = b"".join(b"\x00" + rgba[y * width * 4:(y + 1) * width * 4] for y in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def _check_mark(size: int, colour: str, fill: str, border: str, radius: float) -> bytes:
    """A check box: a filled rounded square with a check mark drawn over it."""
    base = bytearray(rounded_rgba(size, size, radius, fill, border=border))
    cr, cg, cb = _hex(colour)
    points = [(0.24, 0.52), (0.42, 0.70), (0.76, 0.32)]
    for y in range(size):
        for x in range(size):
            px, py = (x + 0.5) / size, (y + 0.5) / size
            best = min(_segment_distance(px, py, *points[0], *points[1]),
                       _segment_distance(px, py, *points[1], *points[2]))
            strength = max(0.0, min(1.0, (0.085 - best) * size / 1.2 + 0.5))
            if strength > 0:
                i = (y * size + x) * 4
                base[i] = round(base[i] * (1 - strength) + cr * strength)
                base[i + 1] = round(base[i + 1] * (1 - strength) + cg * strength)
                base[i + 2] = round(base[i + 2] * (1 - strength) + cb * strength)
    return bytes(base)


def _segment_distance(px, py, ax, ay, bx, by) -> float:
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return ((px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2) ** 0.5


def _radio(size: int, fill: str, border: str, dot: str | None) -> bytes:
    base = bytearray(rounded_rgba(size, size, size / 2, fill, border=border))
    if dot is not None:
        inner = rounded_rgba(size, size, size / 2, dot, margin=size * 0.28)
        for i in range(0, len(base), 4):
            alpha = inner[i + 3] / 255
            if alpha:
                for k in range(3):
                    base[i + k] = round(base[i + k] * (1 - alpha) + inner[i + k] * alpha)
    return bytes(base)


def _chevron(width: int, height: int, colour: str) -> bytes:
    cr, cg, cb = _hex(colour)
    out = bytearray(width * height * 4)
    a, b, c = (0.30, 0.40), (0.50, 0.60), (0.70, 0.40)
    for y in range(height):
        for x in range(width):
            px, py = (x + 0.5) / width, (y + 0.5) / height
            d = min(_segment_distance(px, py, *a, *b), _segment_distance(px, py, *b, *c))
            strength = max(0.0, min(1.0, (0.07 - d) * width / 1.1 + 0.5))
            if strength > 0:
                i = (y * width + x) * 4
                out[i:i + 4] = bytes((cr, cg, cb, round(strength * 255)))
    return bytes(out)


# ---- what each control looks like, per theme --------------------------------------------------------------------
def image_size(variant: str) -> tuple[int, int]:
    return PILL_SIZE if variant == "chip" else BUTTON_SIZE


def nine_slice(size: tuple[int, int], corner: float) -> tuple[int, int, int, int]:
    """The fixed borders (left, top, right, bottom) of a picture of `size` whose corners need `corner` pixels,
    always leaving a stretchable middle of at least MIN_CENTRE pixels each way: Tk's image element tiles that
    middle, and a middle of zero (or less) pixels never finishes tiling."""
    width, height = size
    horizontal = int(max(1, min(corner, (width - MIN_CENTRE) // 2)))
    vertical = int(max(1, min(corner, (height - MIN_CENTRE) // 2)))
    return horizontal, vertical, horizontal, vertical


def button_border(variant: str, radius: float) -> tuple[int, int, int, int]:
    return nine_slice(image_size(variant), MARGIN + radius + 1)


CHIP_BORDER = nine_slice(CHIP_LABEL_SIZE, 15)


def widen_middle(rgba: bytes, width: int, height: int, border: tuple[int, int, int, int], *, down: bool = False
                 ) -> tuple[int, int, bytes]:
    """The nine-slice picture `rgba` with its stretchable middle repeated -- in the bitmap itself, starting where
    Tk's own tiling starts -- until it is at least STRETCH pixels wide (and, with `down`, tall). Drawn stretched, it
    gives exactly the pixels the small picture gave, in a few large copies instead of thousands of small ones."""
    left, top, right, bottom = border
    middle_w, middle_h = width - left - right, height - top - bottom
    times_x = -(-STRETCH // middle_w) if middle_w > 0 else 1
    times_y = -(-STRETCH // middle_h) if down and middle_h > 0 else 1
    rows = []
    for y in range(height):
        row = rgba[y * width * 4:(y + 1) * width * 4]
        rows.append(row[:left * 4] + row[left * 4:(width - right) * 4] * times_x + row[(width - right) * 4:])
    rows = rows[:top] + rows[top:height - bottom] * times_y + rows[height - bottom:]
    return left + middle_w * times_x + right, len(rows), b"".join(rows)


_THEME_DISABLED = object()       # "the theme's own disabled fill" (None means: transparent when disabled)


def _button_states(fill, hover, pressed, *, border=None, hover_border=None, disabled_fill=_THEME_DISABLED,
                   disabled_border=None, radius: float = th.RADIUS_CONTROL):
    """(name, draw-kwargs) for the six pictures of one button variant."""
    c = th.ACTIVE
    common = dict(radius=radius, margin=MARGIN)
    disabled = c.disabled_bg if disabled_fill is _THEME_DISABLED else disabled_fill
    return {
        "normal": dict(fill=fill, border=border, **common),
        "hover": dict(fill=hover, border=hover_border or border, **common),
        "pressed": dict(fill=pressed, border=hover_border or border, **common),
        "focus": dict(fill=fill, border=border, ring=c.focus, **common),
        "focus_hover": dict(fill=hover, border=hover_border or border, ring=c.focus, **common),
        "disabled": dict(fill=disabled, border=disabled_border, **common),
    }


def button_variants() -> dict[str, dict[str, dict]]:
    c = th.ACTIVE
    return {
        "primary": _button_states(c.primary, c.primary_hover, c.primary_pressed),
        "secondary": _button_states(c.neutral_btn, c.neutral_btn_active, c.primary_soft, border=c.border_strong,
                                    hover_border=c.primary, disabled_border=c.border),
        "danger": _button_states(c.danger_fill, c.danger_fill_active, c.danger_fill_active),
        "soft_danger": _button_states(c.danger_bg, c.danger_bg_active, c.danger_bg_active),
        "quiet": _button_states(None, c.accent_hover, c.primary_soft, disabled_fill=None),
        "nav": _button_states(None, c.sidebar_hover, c.sidebar_active, disabled_fill=None),
        "nav_active": _button_states(c.sidebar_active, c.sidebar_active, c.sidebar_active),
        "chip": _button_states(c.surface, c.accent_hover, c.primary_soft, border=c.border_strong,
                               hover_border=c.primary, disabled_fill=c.surface, disabled_border=c.border,
                               radius=(BUTTON_SIZE[1] - 2 * MARGIN) / 2),
    }


STYLE_OF_VARIANT = {"primary": "Primary.TButton", "secondary": "TButton", "danger": "Danger.TButton",
                    "soft_danger": "SmallDanger.TButton", "quiet": "Icon.TButton", "nav": "Nav.TButton",
                    "nav_active": "NavActive.TButton", "chip": "Chip.TButton"}


def _element(style: ttk.Style, name: str, *args, **kwargs) -> None:
    """Create a ttk element once per interpreter: a second window in the same interpreter reuses it (the images it
    names are shared and re-rendered in place)."""
    try:
        style.element_create(name, *args, **kwargs)
    except tk.TclError as exc:
        if "Duplicate element" not in str(exc):
            raise


class Skin:
    """The images and ttk elements of one Tk interpreter. `apply()` (re)renders them for the active theme."""

    def __init__(self, root: tk.Misc) -> None:
        self._root = root
        self._images: dict[str, tk.PhotoImage] = {}
        self._created = False
        self._cache: dict[tuple, str] = {}

    def _image(self, name: str, width: int, height: int, rgba: bytes) -> tk.PhotoImage:
        key = (name, width, height, hash(rgba))
        data = self._cache.get(key)
        if data is None:
            data = base64.b64encode(encode_rgba_png(width, height, rgba)).decode("ascii")
            self._cache[key] = data
        image = self._images.get(name)
        if image is None:
            image = tk.PhotoImage(master=self._root, name=f"olive_{name}", data=data)
            self._images[name] = image
        else:
            image.configure(data=data)
        return image

    def _shape(self, name: str, size: tuple[int, int], radius: float, fill, border=None, ring=None,
               margin: float = 0.0, *, stretch: tuple[int, int, int, int], down: bool = False) -> tk.PhotoImage:
        """A rounded picture of `size`, its middle widened (`widen_middle`) for the nine-slice `stretch`."""
        width, height = size
        rgba = rounded_rgba(width, height, radius, fill, border=border, ring=ring, margin=margin)
        return self._image(name, *widen_middle(rgba, width, height, stretch, down=down))

    def apply(self, style: ttk.Style) -> None:
        c = th.ACTIVE
        variants = button_variants()
        for variant, states in variants.items():
            stretch = button_border(variant, states["normal"]["radius"])
            for state, spec in states.items():
                self._shape(f"btn_{variant}_{state}", image_size(variant), spec["radius"], spec["fill"],
                            border=spec.get("border"), ring=spec.get("ring"), margin=spec["margin"], stretch=stretch)
        for state, (border, fill, ring) in {"normal": (c.input_border, c.surface, None),
                                            "focus": (c.primary, c.surface, c.focus),
                                            "disabled": (c.border, c.disabled_bg, None)}.items():
            self._shape(f"field_{state}", FIELD_SIZE, 6, fill, border=border, ring=ring, margin=2,
                        stretch=FIELD_BORDER)
        self._shape("card", CARD_SIZE, th.RADIUS_CARD, c.surface, border=c.border, stretch=CARD_BORDER, down=True)
        self._shape("card_soft", CARD_SIZE, th.RADIUS_CARD, c.surface_alt, stretch=CARD_BORDER, down=True)
        self._shape("toast", CARD_SIZE, 10, c.overlay, border=c.border_strong, stretch=TOAST_BORDER, down=True)
        self._shape("chip", CHIP_LABEL_SIZE, 14, c.surface, border=c.border, stretch=CHIP_BORDER)
        s = INDICATOR
        self._image("check_off", s, s, rounded_rgba(s, s, 4, c.surface, border=c.input_border))
        self._image("check_on", s, s, _check_mark(s, c.text_on_primary, c.primary, c.primary, 4))
        self._image("check_off_disabled", s, s, rounded_rgba(s, s, 4, c.disabled_bg, border=c.border))
        self._image("check_on_disabled", s, s, _check_mark(s, c.disabled_bg, c.disabled_fg, c.disabled_fg, 4))
        self._image("check_off_hover", s, s, rounded_rgba(s, s, 4, c.accent_hover, border=c.primary))
        self._image("radio_off", s, s, _radio(s, c.surface, c.input_border, None))
        self._image("radio_on", s, s, _radio(s, c.surface, c.primary, c.primary))
        self._image("radio_off_disabled", s, s, _radio(s, c.disabled_bg, c.border, None))
        self._image("radio_on_disabled", s, s, _radio(s, c.disabled_bg, c.border, c.disabled_fg))
        self._image("radio_off_hover", s, s, _radio(s, c.accent_hover, c.primary, None))
        self._image("chevron", 20, 16, _chevron(20, 16, c.text_muted))
        self._image("chevron_disabled", 20, 16, _chevron(20, 16, c.disabled_fg))
        if not self._created:
            self._create(style, variants)
            self._created = True

    def _create(self, style: ttk.Style, variants) -> None:
        for variant, states in variants.items():
            border = button_border(variant, states["normal"]["radius"])
            width, height = image_size(variant)
            p = f"olive_btn_{variant}_"
            _element(style, f"Olive.{variant}.border", "image", p + "normal",
                                 ("disabled", p + "disabled"), ("pressed", p + "pressed"),
                                 ("focus", "active", p + "focus_hover"), ("active", p + "hover"),
                                 ("focus", p + "focus"), border=border, padding=MARGIN + 1, sticky="nsew",
                                 width=width, height=height)
            style.layout(STYLE_OF_VARIANT[variant], [
                ("Button.background", {"sticky": "nsew", "children": [
                    (f"Olive.{variant}.border", {"sticky": "nsew", "children": [
                        ("Button.padding", {"sticky": "nsew", "children": [
                            ("Button.label", {"sticky": "nsew"})]})]})]})])
        _element(style, "Olive.field", "image", "olive_field_normal", ("disabled", "olive_field_disabled"),
                             ("focus", "olive_field_focus"), border=FIELD_BORDER, padding=3, sticky="nsew",
                             width=FIELD_SIZE[0], height=FIELD_SIZE[1])
        _element(style, "Olive.chevron", "image", "olive_chevron", ("disabled", "olive_chevron_disabled"),
                             sticky="")
        style.layout("TEntry", [("Entry.background", {"sticky": "nsew", "children": [
            ("Olive.field", {"sticky": "nsew", "children": [
                ("Entry.padding", {"sticky": "nsew", "children": [("Entry.textarea", {"sticky": "nsew"})]})]})]})])
        style.layout("TCombobox", [("Combobox.background", {"sticky": "nsew", "children": [
            ("Olive.field", {"sticky": "nsew", "children": [
                ("Olive.chevron", {"side": "right", "sticky": "ns"}),
                ("Combobox.padding", {"expand": "1", "sticky": "nsew", "children": [
                    ("Combobox.textarea", {"sticky": "nsew"})]})]})]})])
        _element(style, "Olive.check", "image", "olive_check_off",
                             ("disabled", "selected", "olive_check_on_disabled"),
                             ("disabled", "olive_check_off_disabled"), ("selected", "olive_check_on"),
                             ("active", "olive_check_off_hover"), sticky="")
        _element(style, "Olive.radio", "image", "olive_radio_off",
                             ("disabled", "selected", "olive_radio_on_disabled"),
                             ("disabled", "olive_radio_off_disabled"), ("selected", "olive_radio_on"),
                             ("active", "olive_radio_off_hover"), sticky="")
        for name, indicator in (("Choice.TCheckbutton", "Olive.check"), ("TCheckbutton", "Olive.check"),
                                ("Choice.TRadiobutton", "Olive.radio"), ("TRadiobutton", "Olive.radio")):
            widget = "Checkbutton" if "Check" in name else "Radiobutton"
            style.layout(name, [(f"{widget}.padding", {"sticky": "nsew", "children": [
                (indicator, {"side": "left", "sticky": ""}),
                (f"{widget}.focus", {"side": "left", "sticky": "w", "children": [
                    (f"{widget}.label", {"sticky": "nsew"})]})]})])
        for name in ("card", "card_soft"):
            _element(style, f"Olive.{name}", "image", f"olive_{name}", border=CARD_BORDER, padding=1, sticky="nsew",
                     width=CARD_SIZE[0], height=CARD_SIZE[1])
        _element(style, "Olive.toast", "image", "olive_toast", border=TOAST_BORDER, padding=1, sticky="nsew",
                 width=CARD_SIZE[0], height=CARD_SIZE[1])
        _element(style, "Olive.chip", "image", "olive_chip", border=CHIP_BORDER, padding=2, sticky="nsew",
                 width=CHIP_LABEL_SIZE[0], height=CHIP_LABEL_SIZE[1])
        for style_name, element in (("Panel.TFrame", "Olive.card"), ("Card.TFrame", "Olive.card"),
                                    ("SoftCard.TFrame", "Olive.card_soft"), ("Toast.TFrame", "Olive.toast")):
            style.layout(style_name, [("Frame.background", {"sticky": "nsew", "children": [
                (element, {"sticky": "nsew"})]})])
        style.layout("Chip.TLabel", [("Label.background", {"sticky": "nsew", "children": [
            ("Olive.chip", {"sticky": "nsew", "children": [
                ("Label.padding", {"sticky": "nsew", "children": [("Label.label", {"sticky": "nsew"})]})]})]})])
