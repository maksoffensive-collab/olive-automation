"""Provider-neutral macro model and safe playback engine.

A `Macro` wraps a finished `Recording` (app.providers.recording); `PlaybackEngine` runs its compiled actions through
the neutral input interfaces of ONE explicitly bound target. This package imports only `core` and `providers`: no
operating-system, device or game knowledge lives here. (`app/macros` is the older, device-specific record-file
package and is unrelated.) See docs/ARCHITECTURE.md, "Macro engine".
"""
