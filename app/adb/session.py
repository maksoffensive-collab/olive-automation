"""Read-only ADB session bound to a VerifiedTarget.

The session is the only object through which later features (screenshots, input) will reach
a device. It is created from a `VerifiedTarget`, never from a bare serial. It runs nothing
itself: every command goes through `AdbClient`, the only module that starts adb.exe.

Freshness: a target proves identity at one moment. Before each query the session checks that
the proof is younger than `max_age_s`; otherwise it re-verifies through the MappingService
and refuses if the result is not the same logical instance on the same serial (for example
after an emulator restart). It also refuses if the device is no longer in state `device`.

Scope of this module: READ-ONLY queries and screenshots. No input, no app launch, no file transfer.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from app.core.cancel import EmergencyStop
from app.core.errors import TargetNotVerified
from app.instances.mapping import MappingService, VerifiedTarget

from . import client as _client
from .client import AdbClient, AdbError, DeviceState
from .png import png_size

_SIZE_RE = re.compile(r"Physical size:\s*(\d+)x(\d+)")
_DENSITY_RE = re.compile(r"Physical density:\s*(\d+)")
_PKG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")


@dataclass(frozen=True)
class DisplayInfo:
    width: int
    height: int
    dpi: int


_SAFE_NAME = re.compile(r"[^A-Za-z0-9_-]")


@dataclass(frozen=True)
class AdbCapture:
    """A validated screenshot at the ADB level (carries the serial). Generic layers never see this type:
    app/ldplayer/provider.py converts it into a provider-independent Screenshot."""
    png: bytes
    width: int
    height: int
    serial: str
    logical_id: str


class AdbSession:
    """Read-only session bound to one VerifiedTarget."""

    def __init__(self, target: VerifiedTarget, adb: AdbClient, mapping: MappingService, stop: EmergencyStop, *,
                 max_age_s: float = 60.0, clock: Callable[[], float] = time.monotonic) -> None:
        if not isinstance(target, VerifiedTarget):
            raise TargetNotVerified("An AdbSession can only be created from a VerifiedTarget.",
                                    reason_code="not_verified_target")
        if max_age_s <= 0:
            raise ValueError("max_age_s must be positive")
        self._target = target
        self._adb = adb
        self._mapping = mapping
        self._stop = stop
        self._max_age = max_age_s
        self._clock = clock

    @property
    def target(self) -> VerifiedTarget:
        return self._target

    # ---- safety gate ----------------------------------------------------
    def ensure_online(self) -> str:
        """Public form of the gate every query in this session runs before it reaches the device: re-verifies
        through MappingService if the proof is older than max_age_s, voids the session if the serial changed, and
        refuses if the device is not in state `device`. Returns the currently valid serial.

        Exposed (as opposed to only the private `_gate()` every read method already calls) for
        `app.adb.input.InputSession` operations that have no coordinate/display query of their own to piggyback
        the gate on the way `tap()` piggybacks it on `display()` -- key presses and text have nothing to bound-check
        against the screen, but must still prove the target is CURRENTLY valid before anything is sent."""
        return self._gate()

    def _gate(self) -> str:
        """Run before every device access. Returns the serial that may be used."""
        self._stop.check()
        if self._target.age_s(self._clock()) > self._max_age:
            fresh = self._mapping.revalidate(self._target)
            if (fresh.logical_id, fresh.adb_serial) != (self._target.logical_id, self._target.adb_serial):
                raise TargetNotVerified(
                    f"{self._target.ld_name} is now on a different ADB serial than before; the session is void.",
                    reason_code="serial_changed").saying(
                    ru=f"{self._target.ld_name} теперь на другом серийном номере ADB, чем раньше; сеанс недействителен.")
            self._target = fresh
        state = self._device_state()
        if state is not DeviceState.DEVICE:
            raise TargetNotVerified(f"{self._target.ld_name} is not online over ADB (state: {state.value}).",
                                    reason_code="device_not_online").saying(
                ru=f"{self._target.ld_name} не на связи по ADB (состояние: {state.value}).")
        return self._target.adb_serial

    def _device_state(self) -> DeviceState:
        for dev in self._adb.list_devices():
            if dev.serial == self._target.adb_serial:
                return dev.state
        raise TargetNotVerified(f"{self._target.ld_name} is no longer listed by ADB.", reason_code="device_gone").saying(
            ru=f"{self._target.ld_name} больше нет в списке устройств ADB.")

    # ---- read-only queries ----------------------------------------------
    def state(self) -> DeviceState:
        self._stop.check()
        return self._device_state()

    def getprop(self, name: str) -> str:
        return self._adb.getprop(self._gate(), name)

    def display(self) -> DisplayInfo:
        serial = self._gate()
        size = _SIZE_RE.search(self._adb.shell_query(serial, "wm", "size"))
        density = _DENSITY_RE.search(self._adb.shell_query(serial, "wm", "density"))
        if not size or not density:
            raise AdbError("Could not parse the display size or density reported by the device.").saying(
                ru="Не удалось разобрать размер экрана или плотность, которые сообщило устройство.")
        return DisplayInfo(int(size.group(1)), int(size.group(2)), int(density.group(1)))

    def is_package_installed(self, package: str) -> bool:
        if not _PKG_RE.match(package):
            raise AdbError(f"'{package}' is not a valid package name.")
        out = self._adb.shell_query(self._gate(), "pm", "list", "packages", package)
        return f"package:{package}" in out.splitlines()

    def android_version(self) -> str:
        return self.getprop("ro.build.version.release")

    # ---- screenshot ----------------------------------------------------------
    def screenshot(self) -> AdbCapture:
        """Capture the screen of the verified target. The PNG is checked for integrity and its size must
        equal the device's reported display; otherwise nothing is returned."""
        serial = self._gate()
        display = self.display()
        png = self._adb.screencap_png(serial, grant=_client.CaptureGrant(serial, _client._INPUT_MINT))
        width, height = png_size(png)
        if (width, height) != (display.width, display.height):
            raise AdbError(f"Screenshot is {width}x{height} but the device reports {display.width}x{display.height}; discarded.").saying(
                ru=f"Снимок экрана {width}x{height}, а устройство сообщает {display.width}x{display.height}; снимок отброшен.")
        return AdbCapture(png, width, height, serial, self._target.logical_id)

    def save_screenshot(self, base_dir: Path) -> Path:
        """Capture and write to <base_dir>/<logical id>/<timestamp>.png. `base_dir` is chosen by the caller
        (the project's data/screenshots); the file name is generated here and never taken from input."""
        shot = self.screenshot()
        base = Path(base_dir).resolve()
        folder = (base / _SAFE_NAME.sub("_", shot.logical_id)).resolve()
        if base not in folder.parents:
            raise AdbError("Refusing to write a screenshot outside the screenshot folder.")
        folder.mkdir(parents=True, exist_ok=True)
        stem = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        for n in range(100):  # the clock can repeat a value; never overwrite, never loop forever
            path = folder / (f"{stem}.png" if n == 0 else f"{stem}_{n}.png")
            try:
                with open(path, "xb") as fh:
                    fh.write(shot.png)
                break
            except FileExistsError:
                continue
        else:
            raise AdbError("Could not find a free screenshot file name.")
        return path
