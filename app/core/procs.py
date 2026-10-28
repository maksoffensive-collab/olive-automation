"""Read-only process facts: whether a process is still running.

For a provider that asks another program to close and must not touch that program's files before it is really
gone: a program may write its files while it closes, before or after its own list says "stopped" (seen on
2026-10-27; the provider's own docs say which). Asks the system only; starts nothing.

`hold_processes` takes the processes BEFORE they are asked to end and then asks those held handles: a process id
that another program is given afterwards can never look like them (the second review of 2026-10-27).
`process_running` asks by id, for a process that could not be held.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt

_SYNCHRONIZE = 0x00100000
_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_INVALID_PARAMETER = 87          # what the system answers for an id no process has


def _kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype, kernel32.OpenProcess.argtypes = wt.HANDLE, [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel32.GetExitCodeProcess.restype = wt.BOOL
    kernel32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
    kernel32.CloseHandle.restype, kernel32.CloseHandle.argtypes = wt.BOOL, [wt.HANDLE]
    return kernel32


def _is_id(pid: object) -> bool:
    return isinstance(pid, int) and not isinstance(pid, bool) and pid > 0


def _still_active(kernel32, handle) -> bool:
    code = wt.DWORD()
    if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
        return True                                    # not said: the cautious answer
    return code.value == _STILL_ACTIVE


def process_running(pid: int) -> bool:
    """True while process `pid` runs -- and when the system will not say (no right to ask): the cautious answer,
    so a caller waits (boundedly) rather than act on a program that may still run. False once it has ended."""
    if not _is_id(pid):
        return False
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ctypes.get_last_error() != _ERROR_INVALID_PARAMETER
    try:
        return _still_active(kernel32, handle)
    finally:
        kernel32.CloseHandle(handle)


class HeldProcesses:
    """Processes held by their handles from before they were asked to end. `running()` asks the held handles; a
    process that could not be held is gone already (not counted) or may not be asked about (then asked by its id:
    the cautious answer). `close()` lets go of the handles; also as a context manager."""

    def __init__(self, pids) -> None:
        self._kernel32 = _kernel32()
        self._handles: list = []
        self._unheld: list[int] = []
        for pid in pids:
            if not _is_id(pid):
                continue
            handle = self._kernel32.OpenProcess(_SYNCHRONIZE | _QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                self._handles.append(handle)
            elif ctypes.get_last_error() != _ERROR_INVALID_PARAMETER:
                self._unheld.append(pid)

    def running(self) -> bool:
        return (any(_still_active(self._kernel32, handle) for handle in self._handles)
                or any(process_running(pid) for pid in self._unheld))

    def close(self) -> None:
        handles, self._handles = self._handles, []
        for handle in handles:
            self._kernel32.CloseHandle(handle)

    def __enter__(self) -> "HeldProcesses":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def hold_processes(pids) -> HeldProcesses:
    return HeldProcesses(pids)
