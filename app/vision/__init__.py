"""Deterministic, local computer-vision primitives over a provider `Frame`: colour, template matching and
change detection, plus the PNG codec they need. Standard library only.

This layer answers questions about PIXELS and knows nothing above itself: no observation queries, no OCR, no AI
and no provider. `app.observe` composes these primitives into the platform's observation contract.
"""
