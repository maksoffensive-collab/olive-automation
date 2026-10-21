"""Windows provider: top-level application windows as platform targets (screenshot capability only).

An adapter, independent of every other provider. It implements the generic contracts in `app.providers`
(`Target`, `ScreenshotProvider`) and may import only `app.core`, `app.providers` and itself. Every Windows API
call stays inside `win32.py`; the rest of the package works against the abstract `WindowBackend`.
"""
