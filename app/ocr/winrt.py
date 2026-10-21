"""One concrete `OcrProvider`: the OCR engine that is already part of Windows.

Why this engine, before any other. It is the only option evaluated that needs **nothing installed** -- no Python
package, no model download, no external installer, no GPU -- which is the only kind of dependency this project
has ever accepted (see docs/DEPENDENCIES.md: the environment holds `pip` and nothing else). It is fully offline,
it ships with the operating system this platform already targets, and on this machine it already has both of the
languages the interface itself supports. Tesseract, EasyOCR and PaddleOCR were considered and are recorded in
docs/OBSERVATION.md with the reasons they were not chosen first; nothing here prevents adding one later, because
the automation only ever sees `OcrProvider`.

How it is reached without a dependency: Windows exposes the engine through WinRT, and Windows PowerShell can
call WinRT directly. So the bridge is a PowerShell script -- run through the platform's OWN `CommandRunner`,
which kills it on the emergency stop, on the caller's cancel (the `cancel` keyword) and at a hard timeout, exactly
like every other external command the platform runs. The script is a fixed, ASCII-only file written by this module
(never assembled from caller input; Windows PowerShell 5.1 reads a file without a byte-order mark in the ANSI code
page, so it must stay ASCII); the frame is handed over as a PNG in a private temporary file and deleted afterwards,
and the only thing that crosses back is one line of pure-ASCII JSON, validated strictly before anything is believed.

Lifetime. Nothing touches the file system until the first read, and every read re-checks the script (byte for byte,
replaced atomically when it is missing or different), so a temp-folder cleaner, a corrupted copy or a briefly
unavailable folder costs the reads made meanwhile -- each one an explicit `OcrEngineError` -- and never the rest of
the application's life. Temporary images are deleted after every read, whatever happened; ones left behind by a
process that was killed outright are swept on the first read once they are older than `STALE_TEMP_S`.

This provider uses a Windows API but is NOT tied to any kind of target: it reads a `Frame`, so it cannot tell --
and does not ask -- which provider produced those pixels, a device on the other end of a link or a window on
this desktop. That is the boundary the architecture cares about, and it is why this lives in `app.ocr` rather
than inside any one target provider.

Limitations, stated rather than hidden:
  * No confidence. Windows.Media.Ocr reports none (OcrResult: Lines, Text, TextAngle; OcrLine: Text, Words;
    OcrWord: BoundingRect, Text -- checked by reflection on this machine), so `reports_confidence` is False and
    every result says `confidence_measured=False`: its blocks carry the contract's placeholder 1.0, never a number
    that was measured.
  * Recognition is limited to the OCR language packs installed on the machine; any other language is refused
    (`OcrLanguageUnavailable`), never read in a different one.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path

from app.core.errors import EmergencyStopped, PlatformError
from app.vision.bitmap import encode_png
from app.vision.frame import Frame
from app.vision.roi import Roi, resolve_roi

from .provider import LANGUAGE_TAG, LanguageNotSupported, OcrResult, OcrTextBlock, ReadCancelled

DEFAULT_TIMEOUT_S = 30.0
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_LINES = 10_000                    # more lines than any screen holds: anything beyond is not a real answer
MAX_LINE_TEXT = 10_000
MAX_COORD = 1_000_000
MAX_LANGUAGES = 200
STALE_TEMP_S = 600.0                  # far beyond any read's timeout, so a live read's image is never swept
# A language tag is checked (`LANGUAGE_TAG`, from the contract) before it becomes an argument: it must never start
# with "-", where PowerShell would bind it as a parameter name instead of a value.
_LANGUAGE_EXIT = 3                    # the script's exit code for "no OCR engine for that language"
_LINE_KEYS = frozenset({"text", "x", "y", "w", "h"})

# How the script writes its one line of JSON: pure ASCII, every character above U+007F as a JSON \uXXXX escape.
# Windows PowerShell 5.1 writes redirected output in the console's code page (not UTF-8) and its ConvertTo-Json
# does not escape non-ASCII, so without this "Hämta" arrived as b"H\x84mta" and Cyrillic as "???" -- lost before
# Python could decode anything. Escaped, the text survives any code page and `json.loads` restores it exactly
# (surrogate pairs included). Kept apart so a test can run this very function in a real PowerShell.
_JSON_WRITER = r"""function Write-Json($Value) {
  $json = ConvertTo-Json -InputObject $Value -Depth 4 -Compress
  $out = New-Object System.Text.StringBuilder
  foreach ($ch in $json.ToCharArray()) {
    if ([int]$ch -lt 128) { [void]$out.Append($ch) } else { [void]$out.Append(('\u{0:x4}' -f [int]$ch)) }
  }
  Write-Output $out.ToString()
}
"""

# The box of one recognized line, in whole pixels: the smallest box that covers every word of it. Left and top are
# rounded DOWN, right and bottom UP, and only then is the size taken (right - left), so the box never falls a pixel
# short of the text -- rounding the size itself, ceil(right - left), did for a word spanning 10.6..20.2 (10, not 11).
# A word of zero width still covers the pixel it stands on. Kept apart so a test can run it in a real PowerShell.
_LINE_BOX = r"""function Get-LineBox($Words) {
  $l = $null; $t = $null; $r = $null; $b = $null
  foreach ($w in $Words) {
    $rect = $w.BoundingRect
    $wl = [double]$rect.X; $wt = [double]$rect.Y
    $wr = $wl + [double]$rect.Width; $wb = $wt + [double]$rect.Height
    if ($null -eq $l -or $wl -lt $l) { $l = $wl }
    if ($null -eq $t -or $wt -lt $t) { $t = $wt }
    if ($null -eq $r -or $wr -gt $r) { $r = $wr }
    if ($null -eq $b -or $wb -gt $b) { $b = $wb }
  }
  if ($null -eq $l) { return $null }
  $x = [int][math]::Floor($l); $y = [int][math]::Floor($t)
  $width = [math]::Max(1, [int][math]::Ceiling($r) - $x); $height = [math]::Max(1, [int][math]::Ceiling($b) - $y)
  [pscustomobject]@{ x = $x; y = $y; w = $width; h = $height }
}
"""

# Fixed script. It reads the image at -Path (optionally in -Language), or with -ListLanguages reports the OCR
# languages installed on this machine; either way it writes one line of JSON (through Write-Json, always). It
# contains no interpolated value from any caller. Exit codes: 3 = the language cannot be read here, 4 = any other
# failure, 5 = the image is larger than the engine reads.
_SCRIPT = r"""param([string]$Path = '', [string]$Language = '', [switch]$ListLanguages)
$ErrorActionPreference = 'Stop'
""" + _JSON_WRITER + _LINE_BOX + r"""try {
  Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null
  $asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() |
    Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
                   $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | Select-Object -First 1
  function Await($op, $type) {
    $t = $asTask.MakeGenericMethod($type).Invoke($null, @($op))
    $t.Wait(-1) | Out-Null
    $t.Result
  }
  [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
  [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
  [Windows.Storage.StorageFile, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
  [Windows.Globalization.Language, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null

  if ($ListLanguages) {
    $tags = @([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages | ForEach-Object { $_.LanguageTag })
    Write-Json ([pscustomobject]@{ languages = $tags })
    exit 0
  }
  if (-not $Path) {
    Write-Json ([pscustomobject]@{ error = 'no image was given to read' })
    exit 4
  }
  if ($Language) {
    try {
      $lang = New-Object Windows.Globalization.Language($Language)
    } catch {
      Write-Json ([pscustomobject]@{ error = "'$Language' is not a language Windows recognizes" })
      exit 3
    }
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
    if ($null -eq $engine) {
      $have = ([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages | ForEach-Object { $_.LanguageTag }) -join ','
      Write-Json ([pscustomobject]@{ error = "no OCR language pack for '$Language' (available: $have)" })
      exit 3
    }
  } else {
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
    if ($null -eq $engine) {
      $have = ([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages | ForEach-Object { $_.LanguageTag }) -join ','
      Write-Json ([pscustomobject]@{ error = "none of the user's languages has an OCR language pack (available: $have)" })
      exit 3
    }
  }

  $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)) ([Windows.Storage.StorageFile])
  $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
  $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
  $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
  $max = [Windows.Media.Ocr.OcrEngine]::MaxImageDimension
  if ($bitmap.PixelWidth -gt $max -or $bitmap.PixelHeight -gt $max) {
    $size = "the image is {0}x{1}; this OCR engine reads at most {2}x{2}" -f $bitmap.PixelWidth, $bitmap.PixelHeight, $max
    Write-Json ([pscustomobject]@{ error = $size })
    exit 5
  }
  $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])

  $lines = @()
  foreach ($line in $result.Lines) {
    $box = Get-LineBox $line.Words
    if ($null -ne $box) {
      $lines += [pscustomobject]@{ text = $line.Text; x = $box.x; y = $box.y; w = $box.w; h = $box.h }
    }
  }
  Write-Json ([pscustomobject]@{ lang = $engine.RecognizerLanguage.LanguageTag; lines = $lines })
} catch {
  Write-Json ([pscustomobject]@{ error = ($_.Exception.GetType().Name + ': ' + $_.Exception.Message) })
  exit 4
}
"""
# Named by its own content: two versions of the platform running at once never share -- or rewrite -- one script
# file, so a read always runs exactly the script of the code that started it.
_SCRIPT_NAME = f"winrt_ocr-{hashlib.sha256(_SCRIPT.encode('ascii')).hexdigest()[:16]}.ps1"


class OcrEngineError(PlatformError):
    """The engine could not be reached or could not read the image."""


class OcrLanguageUnavailable(OcrEngineError, LanguageNotSupported):
    """The requested language cannot be read here: not a language tag, not a language Windows knows, no Windows OCR
    language pack for it, or the engine answered in another language. Refused rather than read in another
    language, which would turn "not on the screen" into a wrong answer."""


class OcrEngineTimeout(OcrEngineError):
    """The engine did not answer within its time limit. Still an engine failure ("could not read"), never a
    statement about the screen, and never the same thing as a WAIT UNTIL running out of time."""


def _checked_language(language: object) -> str:
    """'' (no preference) or a well-formed language tag; anything else is refused before it reaches a command."""
    if language == "":
        return ""
    if not isinstance(language, str) or not LANGUAGE_TAG.match(language):
        raise OcrLanguageUnavailable(f"{language!r} is not a language tag such as 'en', 'ru' or 'sv-SE'.")
    return language


def _script_subtag(tag: str) -> str:
    """The script part of a tag ("Cyrl" in "sr-Cyrl-RS"), or ""."""
    parts = tag.split("-")
    return parts[1].casefold() if len(parts) > 1 and len(parts[1]) == 4 and parts[1].isalpha() else ""


def _same_language(requested: str, answered: object) -> bool:
    """The engine may answer with a more specific tag ("sv" -> "sv-SE"); the language must be the same, and so must
    the script when one was asked for ("sr-Cyrl" is not answered by "sr-Latn-RS")."""
    if not isinstance(answered, str) or answered.split("-")[0].casefold() != requested.split("-")[0].casefold():
        return False
    wanted = _script_subtag(requested)
    return not wanted or _script_subtag(answered) == wanted


# ---- strict JSON -----------------------------------------------------------------------------------------------
def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r}")
        out[key] = value
    return out


def _no_constant(name: str) -> object:
    raise ValueError(f"{name} is not a value an OCR result may contain")


def _finite(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"{text} is not a finite number")
    return value


def _parse_json(line: str) -> object:
    """`json.loads` that refuses NaN, Infinity, numbers too large to be finite and repeated keys."""
    return json.loads(line, object_pairs_hook=_no_duplicate_keys, parse_constant=_no_constant, parse_float=_finite)


# ---- the script and the temporary files ------------------------------------------------------------------------
def _ensure_script(directory: Path) -> Path:
    """The fixed script in `directory`, written (atomically) only if it is missing or not byte-for-byte the one
    this module holds. Raises OSError when the folder cannot be used right now."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / _SCRIPT_NAME
    wanted = _SCRIPT.encode("ascii")
    try:
        if path.read_bytes() == wanted:
            return path
    except OSError:
        pass                                            # missing or unreadable: write it
    tmp = directory / f"{_SCRIPT_NAME}.{uuid.uuid4().hex}.tmp"
    try:
        tmp.write_bytes(wanted)
        try:
            os.replace(tmp, path)
        except OSError:
            # another process may be running the script and holding it open; that is fine if it is the right one
            if path.read_bytes() != wanted:
                raise
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    return path


def _sweep(directory: Path, now: float) -> None:
    """Delete this module's temporary files that are older than `STALE_TEMP_S` (left by a process that died
    mid-read). Best effort: anything that cannot be removed now is tried again by the next process."""
    for pattern in ("ocr-*.png", "probe-*.png", "winrt_ocr*.tmp"):
        for path in directory.glob(pattern):
            try:
                if now - path.stat().st_mtime > STALE_TEMP_S:
                    path.unlink()
            except OSError:
                pass


class WindowsOcrProvider:
    """`OcrProvider` backed by Windows' own OCR engine.

    `runner` is the platform's `CommandRunner`, so a read is stop-aware, cancellable and time-bounded like every
    other external command; `work_dir` is where the script and the temporary PNG live and must be a directory this
    process owns. Building one touches nothing; the folder is prepared (and re-checked) by every read."""

    reports_confidence = False            # see the module docstring: Windows.Media.Ocr has no confidence

    def __init__(self, runner, work_dir: str | os.PathLike | None = None, *,
                 powershell: str = "powershell.exe", timeout_s: float = DEFAULT_TIMEOUT_S,
                 language: str = "") -> None:
        if runner is None:
            raise OcrEngineError("The Windows OCR provider needs the platform's CommandRunner.")
        self._runner = runner
        self._powershell = powershell
        self._timeout = float(timeout_s)
        self._language = _checked_language(language)
        base = Path(work_dir) if work_dir is not None else Path(tempfile.gettempdir()) / "lap-ocr"
        self._dir = Path(base)
        self._lock = threading.Lock()     # several targets may read at once: one of them prepares the folder
        self._swept = False

    # ---- OcrProvider -------------------------------------------------------------------------------
    def read_text(self, frame: Frame, *, roi: Roi | None = None, language: str = "",
                  cancel: threading.Event | None = None) -> OcrResult:
        """`language` (a BCP-47 tag) selects the recognizer for this read; "" uses the one this provider was
        built with, else the user's profile languages. A language Windows cannot read is refused, never ignored.
        `cancel`, when set, ends the read at once (`ReadCancelled`); the engine process is killed, not waited for."""
        if not isinstance(frame, Frame):
            raise OcrEngineError("read_text needs a Frame from the verified screenshot pipeline.")
        if cancel is not None and cancel.is_set():
            raise ReadCancelled("The OCR read was cancelled before it started.")   # nothing written, nothing started
        language = _checked_language(language) or self._language
        region = resolve_roi(roi, frame.width, frame.height)
        bitmap = (frame.bitmap.crop(region.x, region.y, region.width, region.height)
                  if roi is not None else frame.bitmap)
        png = encode_png(bitmap)
        if len(png) > MAX_IMAGE_BYTES:
            raise OcrEngineError("The image is too large to hand to the OCR engine.")
        script = self._prepare()
        path = self._dir / f"ocr-{uuid.uuid4().hex}.png"
        try:
            path.write_bytes(png)
            read_in, lines = self._run(script, path, language, cancel)
        except OSError as exc:
            raise OcrEngineError(f"The OCR engine's temporary image could not be written: "
                                 f"{exc.strerror or exc}.") from exc
        finally:
            try:
                path.unlink()
            except OSError:
                pass                                    # a leftover temporary file must never fail a read
        return OcrResult(self._blocks(lines, bitmap.width, bitmap.height, region), confidence_measured=False,
                         language=read_in)

    def languages(self, *, cancel: threading.Event | None = None) -> tuple[str, ...]:
        """The OCR languages installed on this machine, as Windows reports them (BCP-47 tags). Asking this runs the
        same fixed script (in its list mode) through the same runner: stop-aware, cancellable and time-bounded."""
        if cancel is not None and cancel.is_set():
            raise ReadCancelled("Listing the OCR languages was cancelled before it started.")
        payload, returncode = self._invoke(self._prepare(), ["-ListLanguages"], cancel)
        if not isinstance(payload, dict) or "error" in payload:
            self._checked_lines(payload, returncode, "")              # raises the engine's own error
        if not isinstance(payload, dict) or set(payload) != {"languages"}:
            raise OcrEngineError("The OCR engine returned an unexpected list of languages.")
        tags = payload["languages"]
        if isinstance(tags, str):
            tags = [tags]                                           # ConvertTo-Json may collapse a single item
        if tags is None:
            tags = []
        if not isinstance(tags, list) or len(tags) > MAX_LANGUAGES \
                or not all(isinstance(t, str) and LANGUAGE_TAG.match(t) for t in tags):
            raise OcrEngineError("The OCR engine returned an unexpected list of languages.")
        return tuple(dict.fromkeys(tags))

    # ---- plumbing ----------------------------------------------------------------------------------
    def _prepare(self) -> Path:
        with self._lock:
            try:
                script = _ensure_script(self._dir)
            except OSError as exc:
                raise OcrEngineError(f"The OCR engine could not be prepared in {self._dir}: {exc.strerror or exc}. "
                                     "It is tried again on the next read.") from exc
            if not self._swept:
                self._swept = True
                _sweep(self._dir, time.time())
        return script

    def _run(self, script: Path, image: Path, language: str = "",
             cancel: threading.Event | None = None) -> tuple[str, list]:
        """Read one image: (the language the engine read in, its lines)."""
        extra = ["-Path", str(image)] + (["-Language", language] if language else [])
        payload, returncode = self._invoke(script, extra, cancel)
        return self._checked_lines(payload, returncode, language)

    def _invoke(self, script: Path, extra: list[str],
                cancel: threading.Event | None = None) -> tuple[object, object]:
        """Run the fixed script once and return (its parsed JSON answer, its exit code). Every way the run itself
        can end -- the caller's cancel, the emergency stop, the time limit, not starting, saying nothing, saying
        something that is not strict JSON -- is decided here, once, for every use of the script."""
        args = [self._powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", str(script), *extra]
        result = self._runner.run(args, timeout=self._timeout, cancel=cancel)
        if getattr(result, "cancelled", False):
            # the runner killed the engine: for the caller's own cancel, or else for the emergency stop
            if cancel is not None and cancel.is_set():
                raise ReadCancelled("The OCR read was cancelled; the engine was ended before it answered.")
            raise EmergencyStopped("The emergency stop ended the OCR engine before it answered.")
        if getattr(result, "timed_out", False):
            raise OcrEngineTimeout(f"The OCR engine did not answer within {self._timeout:g} s.")
        if getattr(result, "error", None):
            raise OcrEngineError(f"The OCR engine could not be started ({self._powershell}): {result.error}")
        text = (getattr(result, "stdout_text", "") or "").strip()
        returncode = getattr(result, "returncode", None)
        if not text:
            stderr = (getattr(result, "stderr_text", "") or "").strip()
            raise OcrEngineError(f"The OCR engine returned nothing (exit code {returncode})"
                                 + (f": {stderr[:300]}" if stderr else ".")
                                 + " Windows PowerShell with WinRT support is required.")
        try:
            payload = _parse_json(text.splitlines()[-1])
        except (ValueError, RecursionError) as exc:
            raise OcrEngineError(f"The OCR engine returned something that is not valid JSON: {exc}") from exc
        return payload, returncode

    @staticmethod
    def _checked_lines(payload: object, returncode: object, language: str) -> tuple[str, list]:
        if not isinstance(payload, dict):
            raise OcrEngineError("The OCR engine returned an unexpected shape.")
        if "error" in payload:
            error = payload["error"]
            if set(payload) != {"error"} or not isinstance(error, str) or not error:
                raise OcrEngineError("The OCR engine returned a malformed error.")
            if returncode == _LANGUAGE_EXIT:
                raise OcrLanguageUnavailable(error)
            raise OcrEngineError(error)
        if set(payload) != {"lang", "lines"} or not isinstance(payload["lang"], str):
            raise OcrEngineError(f"The OCR engine returned an unexpected shape (keys {sorted(payload)}).")
        if payload["lang"] and not LANGUAGE_TAG.match(payload["lang"]):
            raise OcrEngineError("The OCR engine named the language it read in an unexpected way.")
        if language and not _same_language(language, payload["lang"]):
            raise OcrLanguageUnavailable(f"asked to read {language!r} text, but the engine read "
                                         f"{payload['lang']!r}; nothing read in another language is used.")
        lines, read_in = payload["lines"], payload["lang"]
        if lines is None:
            return read_in, []
        if isinstance(lines, dict):
            return read_in, [lines]                     # ConvertTo-Json collapses a single item to an object
        if not isinstance(lines, list) or len(lines) > MAX_LINES:
            raise OcrEngineError("The OCR engine returned an unexpected shape for its text lines.")
        return read_in, lines

    @staticmethod
    def _blocks(lines: list, width: int, height: int, region: Roi) -> tuple[OcrTextBlock, ...]:
        """Every line, validated, its box clipped to the image that was read and shifted into the FRAME's own
        coordinates. One malformed line fails the whole read: dropping it silently could hide exactly the text a
        condition is looking for, and a decision taken on that would be wrong."""
        blocks = []
        for index, line in enumerate(lines):
            if not isinstance(line, dict) or set(line) != _LINE_KEYS:
                raise OcrEngineError(f"The OCR engine returned a malformed line ({index}).")
            text, values = line["text"], (line["x"], line["y"], line["w"], line["h"])
            if not isinstance(text, str) or len(text) > MAX_LINE_TEXT \
                    or any(isinstance(v, bool) or not isinstance(v, int) or abs(v) > MAX_COORD for v in values):
                raise OcrEngineError(f"The OCR engine returned a malformed line ({index}).")
            x, y, w, h = values
            if w <= 0 or h <= 0:
                raise OcrEngineError(f"The OCR engine returned a line with no area ({index}).")
            x0, y0, x1, y1 = max(0, x), max(0, y), min(width, x + w), min(height, y + h)
            if x1 <= x0 or y1 <= y0:
                raise OcrEngineError(f"The OCR engine returned a line outside the image it read ({index}).")
            blocks.append(OcrTextBlock(text, Roi(x0 + region.x, y0 + region.y, x1 - x0, y1 - y0), 1.0))
        return tuple(blocks)

    def available(self) -> bool:
        """True if this machine can actually run the engine. Never raises: it is a question, not an action."""
        try:
            from app.vision.bitmap import Bitmap
            script = self._prepare()
            path = self._dir / f"probe-{uuid.uuid4().hex}.png"
            try:
                path.write_bytes(encode_png(Bitmap.solid(8, 8, (255, 255, 255))))
                self._run(script, path)
            finally:
                try:
                    path.unlink()
                except OSError:
                    pass
            return True
        except EmergencyStopped:
            raise                               # the stop is never turned into an answer, not even "no"
        except Exception:
            return False
