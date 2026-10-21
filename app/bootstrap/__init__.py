"""Composition root: the only place where the GUI is connected to a concrete provider.

`app.gui` is provider-neutral; this package builds the `AutomationServices` for a provider (today: Windows windows)
and starts the application. It is deliberately outside `app.gui` and `app.windows`, so neither of them depends on
the other and a future provider gets its own module next to `windows_services.py`.
"""
