"""Optional OCR capability: `OcrProvider` (see `provider.py`) is a stable, provider-neutral contract.

One engine is connected: `winrt.WindowsOcrProvider`, Windows' own offline OCR, chosen by the composition root
(`app.bootstrap.main._build_ocr`) and reached only through the observation layer (`app.observe`). It stays optional:
the application keeps working with none configured, and nothing in the core imports this package."""
