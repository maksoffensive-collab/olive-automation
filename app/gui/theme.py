"""Design tokens and the application themes: Light Green (the default) and Dark Green.

Every colour the view draws comes from a named token on a `Theme` instance -- never a raw hex literal scattered
through the screens. `ACTIVE` holds whichever theme is currently in effect; `set_theme(id)` swaps it, after which the
view re-styles itself (`App._apply_theme`) and re-renders. Typography, spacing and corner radius are declared once at
module level: both themes are one product, so only the colours vary.

The light theme is deliberately calm: near-white and very light green surfaces, ONE deep green for the primary
action, selection and progress, and separate, accessible colours for success, warning, error and the emergency stop.
Contrast was measured (WCAG 2.x relative luminance) for every pair the screens draw:

    text #203329 on bg / surface / selected           12.5 / 13.4 / 11.3
    text_muted #56655B on bg / surface_alt / selected   5.7 /  5.4 /  5.2
    white on primary #2D6A4F, on primary_hover #245A41  6.4 /  8.0
    primary on bg (links, active navigation)             6.0
    error #C62828 on surface / error_bg #FDECEA          5.6 /  4.9
    warning #8A5A00 on bg / warning_bg #FFF4E0           5.5 /  5.4
    input_border #7D9483 on surface (a field's edge)     3.3   (WCAG 1.4.11 asks 3:1)

Themes from before the redesign ("light_olive", "dark_olive", "olive_fusion") are no longer offered; a preferences file
that still names one falls back to the default, like any unknown id, and is not rewritten until a setting changes.
"""
from __future__ import annotations

from dataclasses import dataclass

FONT = "Segoe UI"
FONT_SEMIBOLD = "Segoe UI Semibold"
MONO = "Consolas"

# ---- shared design-system scale (identical across every theme) ------------------------------------------------
SPACE_XS, SPACE_SM, SPACE_MD, SPACE_LG, SPACE_XL, SPACE_2XL = 4, 8, 12, 16, 20, 24
RADIUS_CONTROL = 8         # buttons and fields
RADIUS_CARD = 12           # cards and panels
RADIUS_PILL = 999
CONTROL_HEIGHT = 36        # the comfortable height of a button or a field at 96 dpi
NAV_WIDTH = 216            # the application's navigation rail

# Typography: (family, size, weight). One scale for every screen.
TYPE_CAPTION = (FONT, 9)
TYPE_BODY = (FONT, 10)
TYPE_BODY_STRONG = (FONT_SEMIBOLD, 10)
TYPE_LABEL = (FONT_SEMIBOLD, 9)
TYPE_SUBTITLE = (FONT, 11)
TYPE_HEADING = (FONT_SEMIBOLD, 13)
TYPE_TITLE = (FONT_SEMIBOLD, 18)
TYPE_BRAND = (FONT_SEMIBOLD, 13)


@dataclass(frozen=True)
class Theme:
    id: str
    label: str                    # display name in the Appearance switcher (translated separately)
    dark: bool

    # application-level surfaces
    bg: str                       # main workspace background
    sidebar_bg: str               # the navigation rail
    topbar_bg: str                # the page header
    surface: str                  # card / panel background
    surface_alt: str              # nested surface: table heading, inputs at rest, chips
    overlay: str                  # dialog / tooltip / toast surface

    # borders
    border: str                   # dividers and card edges (decorative)
    border_strong: str            # a control's edge at rest (secondary buttons)
    input_border: str             # a field's edge: >= 3:1 against the surface it sits on

    # text
    text: str                     # primary text on `surface` / `bg`
    text_muted: str
    text_on_primary: str          # text on a primary-filled control
    sidebar_text: str             # primary text on `sidebar_bg` / `topbar_bg`
    sidebar_text_muted: str

    # brand / primary action
    primary: str
    primary_hover: str
    primary_pressed: str
    primary_soft: str             # selected rows, active navigation, soft fills
    accent: str                   # small light-green accents (chips, badges)
    accent_hover: str             # hover of quiet controls
    focus: str                    # the keyboard focus ring

    # semantic status (must stay legible on THIS theme's surfaces)
    success: str
    success_bg: str
    warning: str
    warning_bg: str
    error: str
    error_bg: str
    info: str
    info_bg: str
    neutral: str                  # offline / disabled / inactive

    # buttons
    neutral_btn: str              # secondary button fill
    neutral_btn_active: str
    danger_bg: str                # the emergency band and soft destructive buttons
    danger_bg_active: str
    danger_fill: str              # the Emergency Stop button itself
    danger_fill_active: str
    disabled_bg: str
    disabled_fg: str

    # sidebar navigation
    sidebar_active: str           # active nav item background
    sidebar_hover: str
    sidebar_icon: str
    sidebar_icon_active: str

    # workflow blocks of the script editor
    block_wait: str
    block_if: str
    block_run: str
    canvas_bg: str
    canvas_dot: str

    scrollbar: str


LIGHT_GREEN = Theme(
    id="light_green", label="Light Green", dark=False,
    bg="#F4F8F3", sidebar_bg="#FBFDFB", topbar_bg="#F4F8F3", surface="#FFFFFF", surface_alt="#EAF3EB",
    overlay="#FFFFFF",
    border="#D8E5DA", border_strong="#B9CDBE", input_border="#7D9483",
    text="#203329", text_muted="#56655B", text_on_primary="#FFFFFF",
    sidebar_text="#203329", sidebar_text_muted="#56655B",
    primary="#2D6A4F", primary_hover="#245A41", primary_pressed="#1B4332", primary_soft="#E0F0E4",
    accent="#B7E4C7", accent_hover="#EAF3EB", focus="#2D6A4F",
    success="#256B29", success_bg="#E3F4E5", warning="#8A5A00", warning_bg="#FFF4E0",
    error="#C62828", error_bg="#FDECEA", info="#1E5A8A", info_bg="#E7F0F8", neutral="#6C7A70",
    neutral_btn="#FFFFFF", neutral_btn_active="#EAF3EB",
    danger_bg="#FDECEA", danger_bg_active="#F9D7D3", danger_fill="#C62828", danger_fill_active="#A61F1F",
    disabled_bg="#EEF2EE", disabled_fg="#8A978D",
    sidebar_active="#E0F0E4", sidebar_hover="#F0F6F1", sidebar_icon="#56655B", sidebar_icon_active="#2D6A4F",
    block_wait="#A96500", block_if="#1E5A8A", block_run="#2D6A4F", canvas_bg="#F7FAF6", canvas_dot="#DCE7DE",
    scrollbar="#C5D6C9",
)

DARK_GREEN = Theme(
    id="dark_green", label="Dark Green", dark=True,
    bg="#151D18", sidebar_bg="#1A231D", topbar_bg="#151D18", surface="#1E2822", surface_alt="#26322A",
    overlay="#24302A",
    border="#2F3D34", border_strong="#3D4E43", input_border="#7E9585",
    text="#E6EFE8", text_muted="#A8B9AD", text_on_primary="#0E1A13",
    sidebar_text="#E6EFE8", sidebar_text_muted="#A8B9AD",
    primary="#74C69D", primary_hover="#95D5B2", primary_pressed="#52B788", primary_soft="#24382C",
    accent="#2D4A39", accent_hover="#26322A", focus="#95D5B2",
    success="#8FD19E", success_bg="#1F3326", warning="#E3B35A", warning_bg="#3A2F1A",
    error="#F28B82", error_bg="#3D2220", info="#8EC1EA", info_bg="#1D2C3A", neutral="#8E9C92",
    neutral_btn="#26322A", neutral_btn_active="#2F3D34",
    danger_bg="#3D2220", danger_bg_active="#4A2926", danger_fill="#E5534B", danger_fill_active="#C94038",
    disabled_bg="#222C26", disabled_fg="#66756B",
    sidebar_active="#24382C", sidebar_hover="#202B24", sidebar_icon="#A8B9AD", sidebar_icon_active="#95D5B2",
    block_wait="#E3B35A", block_if="#8EC1EA", block_run="#74C69D", canvas_bg="#18211B", canvas_dot="#26322A",
    scrollbar="#3D4E43",
)

THEMES: dict[str, Theme] = {t.id: t for t in (LIGHT_GREEN, DARK_GREEN)}
THEME_ORDER: tuple[str, ...] = (LIGHT_GREEN.id, DARK_GREEN.id)
DEFAULT_THEME = LIGHT_GREEN.id

ACTIVE: Theme = LIGHT_GREEN


def set_theme(theme_id: str) -> Theme:
    """Swap the active theme (falling back to the default for an unknown id -- a preferences file from another
    version, or one of the themes from before the redesign) and return it."""
    global ACTIVE
    ACTIVE = THEMES.get(theme_id, THEMES[DEFAULT_THEME])
    return ACTIVE


def level_colour(level: str) -> str:
    return {"info": ACTIVE.info, "ok": ACTIVE.success, "warn": ACTIVE.warning, "error": ACTIVE.error}[level]


def level_background(level: str) -> str:
    return {"info": ACTIVE.info_bg, "ok": ACTIVE.success_bg, "warn": ACTIVE.warning_bg,
            "error": ACTIVE.error_bg}[level]


def target_colour(status: str) -> str:
    return {"none": ACTIVE.neutral, "selected": ACTIVE.warning, "verified": ACTIVE.success,
            "invalid": ACTIVE.error, "stale": ACTIVE.error}[status]


def dot_colour(name: str) -> str:
    return {"green": ACTIVE.success, "olive": ACTIVE.warning, "red": ACTIVE.error, "grey": ACTIVE.neutral}[name]
