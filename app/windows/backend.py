"""Abstract window backend: the only thing the rest of the Windows provider knows about the operating system.

`Win32Backend` (win32.py) implements it with Win32 calls; tests implement it with a simulated desktop. All
methods are read-only: they observe windows and copy pixels, they never send messages that change a window.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

from app.core.errors import PlatformError
from app.providers.input import Key


class WindowCaptureError(PlatformError):
    """A window exists and is verified, but its pixels cannot be captured reliably right now."""


class InputDeliveryError(PlatformError):
    """The operating system did not accept the input event(s) for the window."""


@dataclass(frozen=True)
class WindowInfo:
    """A snapshot of one top-level window. Never leaves this provider: generic code only sees a Target."""
    hwnd: int
    title: str
    class_name: str
    pid: int
    process_start: int      # process creation time (100 ns ticks); with the pid it identifies ONE process instance, 0 = unknown
    process_name: str       # executable file name only (no path), "" = unknown
    top_level: bool
    visible: bool
    minimized: bool
    cloaked: bool           # hidden by the desktop manager (e.g. on another virtual desktop): pixels are not real
    width: int
    height: int
    enabled: bool = True    # a disabled window (for example behind a modal dialog) ignores mouse input
    # Client-area geometry (the drawable area inside frame, title bar and borders). This is the ONE coordinate space
    # shared by screenshots and input: (0, 0) is the client area's top-left pixel. `client_left` / `client_top` are
    # its offset inside the window rectangle (`width` x `height`); a negative size means "same as the window".
    client_width: int = -1
    client_height: int = -1
    client_left: int = 0
    client_top: int = 0
    # True when the window's own coordinate scale cannot be established (a DPI-unaware or system-DPI-aware program
    # on a monitor whose scale differs from what it assumes): pixel coordinates are then ambiguous and both capture
    # and input are refused instead of guessed.
    dpi_ambiguous: bool = False

    def __post_init__(self) -> None:
        if self.client_width < 0:
            object.__setattr__(self, "client_width", self.width)
        if self.client_height < 0:
            object.__setattr__(self, "client_height", self.height)


@dataclass(frozen=True)
class RawBitmap:
    width: int
    height: int
    bgra: bytes             # top-down, 4 bytes per pixel


@dataclass(frozen=True)
class DesktopCapture:
    """The whole virtual screen -- every monitor -- as one image. `left` and `top` say where the image's (0, 0)
    pixel is on the virtual screen (negative when a monitor sits left of or above the primary one); the image's own
    pixels are what everything downstream measures in."""
    left: int
    top: int
    bitmap: RawBitmap


DESKTOP_HWND = None   # `WindowObservationBackend.start` with no window: the WHOLE desktop (Full Windows)


class WindowBackend(ABC):
    @abstractmethod
    def enumerate(self) -> list[WindowInfo]:
        """Visible top-level windows (not cloaked, non-empty)."""

    @abstractmethod
    def query(self, hwnd: int) -> WindowInfo | None:
        """The window's current state, or None if `hwnd` is not (or no longer) a window."""

    def capture_desktop(self, max_side: int) -> DesktopCapture:
        """The whole desktop as pixels -- read-only, like everything here. A desktop wider or taller than `max_side`
        pixels (the most the caller will use) is refused BEFORE anything is taken or allocated. Optional: a backend
        that cannot do it says so, and a desktop target then simply cannot be captured."""
        raise WindowCaptureError("This backend cannot capture the whole desktop.")

    def virtual_screen(self) -> tuple[int, int, int, int]:
        """(left, top, width, height) of the virtual screen -- every monitor -- in physical pixels: the screen
        layout a Full Windows recording, picture and run are bound to."""
        raise WindowCaptureError("This backend cannot measure the desktop.")

    def owner_pid(self, hwnd: int) -> int | None:
        """The process that owns window `hwnd`, or None if it is not a window (read only)."""
        return None

    def monitors(self) -> tuple[tuple[int, int, int, int], ...]:
        """(left, top, width, height) of each monitor, in the virtual screen's physical pixels. A backend that
        cannot tell monitors apart reports the whole virtual screen as one."""
        return (tuple(self.virtual_screen()),)

    def console_windows(self) -> tuple[int, ...]:
        """The top-level windows showing THIS process's console, if it has one (they belong to another process --
        the console host or a terminal -- but typing or clicking there reaches this application). Read only."""
        return ()

    @abstractmethod
    def capture(self, hwnd: int, width: int, height: int) -> RawBitmap:
        """Copy the pixels of the window's CLIENT area, which must currently be `width` x `height`; the result's
        pixel (x, y) is client coordinate (x, y). Raise WindowCaptureError if that cannot be done."""


def crop_bgra(bgra: bytes, width: int, height: int, left: int, top: int, crop_w: int, crop_h: int) -> bytes:
    """The `crop_w` x `crop_h` rectangle at (`left`, `top`) of a top-down 4-byte-per-pixel image (pure function)."""
    if len(bgra) != width * height * 4:
        raise WindowCaptureError("The rendered image does not match its size.")
    if crop_w <= 0 or crop_h <= 0 or left < 0 or top < 0 or left + crop_w > width or top + crop_h > height:
        raise WindowCaptureError("The client area does not lie inside the rendered window image.")
    stride = width * 4
    return b"".join(bgra[(top + y) * stride + left * 4:(top + y) * stride + (left + crop_w) * 4]
                    for y in range(crop_h))


class MouseEvent(Enum):
    """Single pointer messages a window can be sent. Primary button only for press/release/double click."""
    MOVE = "move"
    MOVE_LEFT_HELD = "move_left_held"     # a move while the primary button is held (needed to compose drags later)
    LEFT_DOWN = "left_down"
    LEFT_UP = "left_up"
    LEFT_DOUBLE = "left_double"           # the double-click message that follows a press+release pair
    RIGHT_DOWN = "right_down"
    RIGHT_UP = "right_up"


class KeyEvent(Enum):
    DOWN = "down"
    UP = "up"


class WindowInputBackend(ABC):
    """The only operating-system input surface of the Windows provider.

    For a WINDOW target: single messages aimed at ONE window handle (`post_*`), composed into user-level operations
    and gated by `WindowInputSession`. For the WHOLE DESKTOP ("Full Windows", allowed on the person's explicit
    decision of 2026-10-19): `desktop_*` events at a screen point or to whatever holds the keyboard focus, used ONLY
    by `DesktopInputSession` (app/windows/desktop.py), which gates every one (screen layout unchanged, never on this
    application's own windows, the person's Esc or own mouse stops the run). Nothing here reads or moves the real
    pointer on its own, or activates a window for the desktop. A backend that cannot send to the desktop refuses.
    """

    @abstractmethod
    def client_size(self, hwnd: int) -> tuple[int, int] | None:
        """Current (width, height) of the window's client area in pixels, or None if it cannot be read."""

    @abstractmethod
    def post_mouse(self, hwnd: int, event: MouseEvent, x: int, y: int) -> None:
        """Post ONE pointer message to that window at client coordinates (x, y). Raise InputDeliveryError."""

    @abstractmethod
    def post_key(self, hwnd: int, event: KeyEvent, key: Key) -> None:
        """Post ONE key-down or key-up message for a neutral `Key` to that window. Raise InputDeliveryError."""

    @abstractmethod
    def post_char(self, hwnd: int, char: str) -> None:
        """Post ONE character message (a single printable BMP character) to that window."""

    # ---- the whole desktop (Full Windows): see the class docstring --------------------------------------------
    def window_at(self, screen_x: int, screen_y: int) -> int | None:
        """The top-level window under a screen point (the system's own hit-test), or None when no window is there."""
        return None

    def foreground_window(self) -> int | None:
        """The top-level window that holds the keyboard focus, or None."""
        return None

    def desktop_mouse(self, event: MouseEvent, screen_x: int, screen_y: int) -> None:
        """ONE real pointer event at a screen point, wherever it lands. Raise InputDeliveryError."""
        raise InputDeliveryError("This backend cannot send input to the whole desktop; nothing sent.")

    def desktop_key(self, event: KeyEvent, key: Key) -> None:
        """ONE real key-down or key-up for whatever holds the keyboard focus. Raise InputDeliveryError."""
        raise InputDeliveryError("This backend cannot send input to the whole desktop; nothing sent.")

    def desktop_char(self, char: str) -> None:
        """ONE real character (a single printable BMP character) for whatever holds the keyboard focus."""
        raise InputDeliveryError("This backend cannot send input to the whole desktop; nothing sent.")

    # ---- letting go at the end of a live run (the person's decision of 2026-10-20): called ONLY by
    # `WindowInputSession.release_held`, for what that session itself pressed. A release presses nothing. ----------
    def release_primary(self) -> None:
        """ONE real release of the primary button where the pointer is now: no move, no focus change, marked as this
        application's own. Raise InputDeliveryError."""
        raise InputDeliveryError("This backend cannot let go of the button; nothing sent.")

    def release_secondary(self) -> None:
        """ONE real release of the secondary button where the pointer is now: no move, no focus change, marked as
        this application's own. Raise InputDeliveryError."""
        raise InputDeliveryError("This backend cannot let go of the button; nothing sent.")

    def release_key(self, key: Key) -> None:
        """ONE real key-up for whatever holds the keyboard focus: no focus change, marked as this application's own.
        Raise InputDeliveryError."""
        raise InputDeliveryError("This backend cannot let go of a key; nothing sent.")


class MouseObservationKind(Enum):
    MOVE = "move"
    PRIMARY_DOWN = "primary_down"
    PRIMARY_UP = "primary_up"
    SECONDARY_DOWN = "secondary_down"
    SECONDARY_UP = "secondary_up"


@dataclass(frozen=True)
class MouseObservation:
    """One observed pointer event with the facts needed to scope it, all read when it happened (never re-read).

    `hit_hwnd` is the top-level window under the pointer (None if unknown); `client_origin` / `client_size` are the
    selected window's client area at that moment in the same screen coordinate space as (screen_x, screen_y)."""
    kind: MouseObservationKind
    screen_x: int
    screen_y: int
    t_ns: int                                   # `time.perf_counter_ns` (monotonic, sub-millisecond), nanoseconds
    hit_hwnd: int | None
    client_origin: tuple[int, int] | None
    client_size: tuple[int, int] | None
    injected: bool = False
    own: bool = False                           # sent by this application itself (marked), never by a person


@dataclass(frozen=True)
class KeyObservation:
    is_down: bool
    vk: int
    t_ns: int
    foreground_hwnd: int | None                 # top-level window that has the keyboard focus
    secure_focus: bool = False                  # the focused control is a password-style entry control
    injected: bool = False
    own: bool = False                           # sent by this application itself (marked), never by a person


@dataclass(frozen=True)
class DoubleClickSettings:
    time_ms: int
    dx: int
    dy: int


class WindowObservationBackend(ABC):
    """Read-only observation of user input, aimed at one window handle (or, with `DESKTOP_HWND`, at the whole
    desktop). It can never modify, block, generate or redirect input, and never changes focus or any window.
    Callbacks are delivered in order on a thread that is NOT the operating system's hook thread. `stop()` is
    idempotent and may be called from inside a callback."""

    @abstractmethod
    def double_click_settings(self) -> DoubleClickSettings: ...

    @abstractmethod
    def start(self, hwnd: int | None, on_mouse, on_key, on_idle) -> None:
        """Begin observing (`hwnd` DESKTOP_HWND: the whole desktop). Raise PlatformError if observation cannot be
        established."""

    @abstractmethod
    def stop(self) -> bool:
        """Stop observing and release every operating-system resource. Returns True if shutdown is known to be
        complete, False if some part of it (for example a worker thread) did not finish within its own timeout and
        may still be active; never raises for that reason alone."""
