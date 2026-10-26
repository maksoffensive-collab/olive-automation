"""One target's view of its own screen: capture once, ask many questions, wait with a bound.

`ObservationContext` is the only part of this package that touches a provider, and it is deliberately scoped to
ONE target -- it holds that target's `ScreenshotProvider` and that target's last frame, and nothing else. There
is no module-level cache, no registry and no shared image anywhere in this package, so N targets observing at
once are N independent objects that cannot see each other's pixels. That is what makes observation safe under
the parallel execution the platform already does; it is the same rule the playback engines follow.

**One screenshot, many observations.** Capturing is by far the most expensive step (measured on this machine:
about 0.4 s to capture a device screen, against 0.06 s to scan a whole frame for a colour), so `observe_all()`
takes one image and answers every query from it. That also makes the answers mutually consistent: three queries
about the same moment really are about the same moment, instead of three moments a second apart.

`max_age_s` controls reuse. A frame younger than that is reused; anything older is recaptured. `0.0` -- the
default -- means "always take a fresh one", so reuse is something a caller opts into deliberately rather than
something that happens behind its back.

**Waiting is always bounded.** `wait_until` takes a timeout and cannot be called without one; it sleeps on the
emergency stop rather than on the clock, so triggering the stop wakes it immediately instead of after the
interval, and it re-raises `EmergencyStopped` so observation halts for exactly the same reason and by exactly
the same mechanism as everything else in the platform. There is no unbounded loop in this package.

Nothing here ever acts on what it sees: there is no input session, no engine and no tap. Turning an
observation into an action stays with the caller and goes through the existing playback/input path.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Iterable, Sequence

from app.core.cancel import EmergencyStop
from app.core.errors import EmergencyStopped, PlatformError
from app.ocr.provider import OcrProvider, OcrResult
from app.providers.base import FrameSource, ScreenshotProvider
from app.providers.size import check_size, factors, fit
from app.vision.frame import Frame
from app.vision.roi import Roi

from .errors import ObservationCancelled, ObservationError, ObservationTimeout, ObservationUnavailable
from .observer import check_template_query, check_text_reader, observe, read_text
from .query import Query, TemplateQuery, TextLinesQuery, TextQuery
from .result import ObservationResult
from .scale import scaled_query

MAX_TIMEOUT_S = 600.0        # ten minutes: long enough for any real wait, short enough that a typo cannot hang
MIN_INTERVAL_S = 0.05        # never poll a provider faster than this, whatever a caller asks for
DEFAULT_INTERVAL_S = 0.5
MAX_PICTURE_BYTES = 128 << 20   # pictures kept at another size per context (a WAIT UNTIL looks again and again)
UNCOVERED_WAIT_S = 0.25      # a target that `prepare` had to uncover gets this long to draw itself before the look


class ObservationContext:
    """Bound to one verified target, through the screenshot provider that target's services built."""

    def __init__(self, provider: ScreenshotProvider, *, stop: EmergencyStop | None = None,
                 ocr: OcrProvider | None = None, clock: Callable[[], float] = time.monotonic,
                 picked_on: tuple[int, int] | None = None, prepare: Callable[[], bool] | None = None) -> None:
        if not isinstance(provider, ScreenshotProvider):
            raise ObservationUnavailable("An observation context needs a ScreenshotProvider bound to a "
                                         "verified target.")
        self._provider = provider
        # the size the queries' points, areas and pictures were picked at: each is scaled to the frame it is asked of
        # (app/observe/scale.py); None asks them as they are
        self.picked_on = check_size(picked_on, "picked_on")
        # called before each fresh screenshot of a run: the target made seen on this computer's screen (its window
        # brought to the front, say); True when it was hidden or covered -- it then gets UNCOVERED_WAIT_S to draw
        # itself, on the stop -- a program that stops drawing while covered would otherwise show a stale picture
        self._prepare = prepare
        self._settle_pending = False           # `settle_next`: the next fresh look waits first, whatever `prepare` says
        self._pictures: dict = {}              # (picture, w, h) -> it at that size: each made once, this context's own
        self._picture_bytes = 0
        self._stop = stop
        self._ocr = ocr
        self._clock = clock
        self._lock = threading.Lock()          # this context's own; contexts never share one
        self._frame: Frame | None = None
        self._captured_at = 0.0                # wall clock, for results a person reads
        self._captured_mono = 0.0              # monotonic, for age decisions a clock change must not break
        self.captures = 0                      # how many images this target actually cost

    # ---- identity ---------------------------------------------------------------------------------
    @property
    def source(self) -> FrameSource:
        return self._provider.identity.source()

    @property
    def target_id(self) -> str:
        return self._provider.target_id

    @property
    def provider_id(self) -> str:
        return self._provider.provider_id

    @property
    def has_ocr(self) -> bool:
        return self._ocr is not None

    # ---- frames -----------------------------------------------------------------------------------
    def _check_stop(self) -> None:
        if self._stop is not None:
            self._stop.check()                 # raises EmergencyStopped; never swallowed here

    def capture(self) -> Frame:
        """Take a new screenshot now, whatever is cached."""
        return self.frame(max_age_s=0.0)

    def frame(self, *, max_age_s: float = 0.0) -> Frame:
        """The current frame, reusing the last one if it is younger than `max_age_s`."""
        frame, _ = self._frame_with_time(max_age_s)
        return frame

    def _frame_with_time(self, max_age_s: float) -> tuple[Frame, float]:
        if isinstance(max_age_s, bool) or not isinstance(max_age_s, (int, float)) or max_age_s < 0:
            raise ObservationError("max_age_s must be a number of seconds, 0 or more.")
        self._check_stop()
        with self._lock:
            if self._frame is not None and max_age_s > 0 and (self._clock() - self._captured_mono) <= max_age_s:
                return self._frame, self._captured_at
            try:
                uncovered = self._prepare() if self._prepare is not None else False
                if uncovered or self._settle_pending:
                    self._settle_pending = False
                    self._settle()
                shot = self._provider.capture()
                frame = Frame.from_screenshot(shot)
            except EmergencyStopped:
                raise                          # cancellation is not a capture failure
            except PlatformError as exc:
                raise ObservationUnavailable(f"Could not capture the screen of {self.target_id}: {exc}") from exc
            except Exception as exc:
                raise ObservationUnavailable(f"Could not capture the screen of {self.target_id}: "
                                             f"{type(exc).__name__}: {exc}") from exc
            self._frame = frame
            self._captured_at = time.time()
            self._captured_mono = self._clock()
            self.captures += 1
            return frame, self._captured_at

    def settle_next(self) -> None:
        """The next fresh look waits UNCOVERED_WAIT_S first: the target was uncovered by someone else just before
        (at the start of a run), so by the time of that look it is no longer covered, yet may not have drawn."""
        self._settle_pending = True

    def _settle(self) -> None:
        """Let a target that was just uncovered draw itself: UNCOVERED_WAIT_S, ended at once by the stop."""
        if self._stop is not None:
            if self._stop.wait(UNCOVERED_WAIT_S):
                self._stop.check()
        else:
            time.sleep(UNCOVERED_WAIT_S)

    def forget(self) -> None:
        """Drop the cached frame, so the next observation captures a fresh one."""
        with self._lock:
            self._frame = None

    # ---- observing --------------------------------------------------------------------------------
    def read_text(self, *, region: Roi | None = None, language: str = "",
                  cancel: threading.Event | None = None) -> OcrResult:
        """Capture a fresh frame of this target and return all the text in it (or in `region`): what a person
        wants when they ask "what does it say?". The same checks and errors as a text query, the same stop and
        cancel behaviour; nothing is acted on."""
        return self.read_text_with_frame(region=region, language=language, cancel=cancel)[1]

    def read_text_with_frame(self, *, region: Roi | None = None, language: str = "",
                             cancel: threading.Event | None = None) -> tuple[Frame, OcrResult]:
        """The same reading, together with the frame it was read from -- for a person who wants to SEE where each
        piece of text is: every block's bounds are in that frame's own pixels, so the frame is what they belong
        to. Exactly the checks, errors, stop and cancel behaviour of `read_text`."""
        check_text_reader(self._ocr, TextQuery("", region=region, language=language))   # before any capture
        if cancel is not None and cancel.is_set():
            self._check_stop()                  # the stop outranks the cancel
            raise ObservationCancelled("The text reading was cancelled before it started.")
        frame, _ = self._frame_with_time(0.0)
        try:
            return frame, read_text(frame, ocr=self._ocr, region=region, language=language, cancel=cancel)
        except ObservationCancelled:
            self._check_stop()                  # the stop outranks a cancel that happened at the same time
            raise

    def check_can_answer(self, query: Query) -> None:
        """Raise now if this context could never answer `query` -- a text query with no OCR reader, or in a language
        the reader cannot be asked for; a picture in any size without the fast search, or of one flat colour --
        without capturing or reading anything."""
        if isinstance(query, (TextQuery, TextLinesQuery)):
            check_text_reader(self._ocr, query)
        if isinstance(query, TemplateQuery):
            check_template_query(query)

    def observe(self, query: Query, *, max_age_s: float = 0.0, cancel: threading.Event | None = None,
                picked_on: tuple[int, int] | None = None) -> ObservationResult:
        """`cancel` is the caller's own cancel: an OCR read under way ends promptly when it is set
        (`ObservationCancelled`). If the emergency stop is set too, the stop is what is raised. `picked_on`: the size
        THIS query's point, area or picture was picked at, when it has one of its own (else the context's)."""
        frame, captured_at = self._frame_with_time(max_age_s)
        try:
            return observe(frame, self._fitted(query, frame, picked_on), ocr=self._ocr, captured_at=captured_at,
                           cancel=cancel, check=lambda: self._interrupt(cancel))
        except ObservationCancelled:
            self._check_stop()                  # the stop outranks a cancel that happened at the same time
            raise

    def observe_all(self, queries: Sequence[Query] | Iterable[Query], *,
                    max_age_s: float = 0.0) -> tuple[ObservationResult, ...]:
        """Every query against ONE image, in the order given. The image is captured once even when a hundred
        queries are asked, and every answer describes the same instant."""
        queries = tuple(queries)
        if not queries:
            raise ObservationError("observe_all needs at least one query; an empty list observes nothing.")
        frame, captured_at = self._frame_with_time(max_age_s)
        return tuple(observe(frame, self._fitted(q, frame), ocr=self._ocr, captured_at=captured_at,
                             check=lambda: self._interrupt(None)) for q in queries)

    def _interrupt(self, cancel: threading.Event | None) -> None:
        """Between the parts of one look: the stop, then the caller's cancel, end it."""
        self._check_stop()
        if cancel is not None and cancel.is_set():
            raise ObservationCancelled("The look was cancelled.")

    def _resized(self, picture, w: int, h: int):
        key = (picture, w, h)
        found = self._pictures.get(key)
        if found is None:
            found = picture.resized(w, h, check=self._check_stop)     # a large picture: the stop is heard inside
            if self._picture_bytes + len(found.rgb) > MAX_PICTURE_BYTES:
                self._pictures.clear()
                self._picture_bytes = 0
            if len(found.rgb) <= MAX_PICTURE_BYTES:
                self._pictures[key] = found
                self._picture_bytes += len(found.rgb)
        return found

    def _fitted(self, query: Query, frame: Frame, picked_on=None) -> Query:
        """`query` at this frame's size, when its own size (`picked_on`, else the context's) is known and differs --
        in any proportions, each direction by its own factor: a query only looks (app/providers/size.py)."""
        picked_on = self.picked_on if picked_on is None else check_size(picked_on, "picked_on")
        if picked_on is None:
            return query
        scale = factors(picked_on, (frame.width, frame.height))
        return query if scale is None else scaled_query(query, *scale, frame.width, frame.height,
                                                        resize=self._resized)

    def scale(self) -> tuple[float, float]:
        """(sx, sy) from the size the queries were picked at to the frame last looked at: what a distance set at
        that size -- a click beside the match -- is multiplied by. (1, 1) when nothing is scaled. Only for the same
        proportions (`SizeChanged` otherwise): a look follows any size, but a click at a distance from what was
        found lands on a point nobody looked at, and stretched it would land somewhere else."""
        with self._lock:
            frame = self._frame
        if self.picked_on is None or frame is None:
            return 1.0, 1.0
        return fit(self.picked_on, (frame.width, frame.height)) or (1.0, 1.0)

    def check_size(self, queries: Iterable[Query] = ()) -> None:
        """Raise now -- a fresh look, before anything is done -- if one of `queries` cannot be asked at the size the
        target has now (a picture that would be too small, or made flat: `InvalidQuery`)."""
        if self.picked_on is not None:
            frame, _ = self._frame_with_time(0.0)
            for query in queries:
                fitted = self._fitted(query, frame)
                if isinstance(fitted, TemplateQuery):
                    check_template_query(fitted)      # a picture made flat by its new size

    def check_sizes(self, parts) -> None:
        """`check_size` for queries each picked at a size of its own: (query, size) pairs -- a fresh look, before
        anything is done; `InvalidQuery` for a picture that would be too small, or made flat, at the size now."""
        frame, _ = self._frame_with_time(0.0)
        for query, picked_on in parts:
            fitted = self._fitted(query, frame, picked_on)
            if isinstance(fitted, TemplateQuery):
                check_template_query(fitted)

    def wait_until(self, query: Query, *, timeout_s: float, interval_s: float = DEFAULT_INTERVAL_S,
                   ) -> ObservationResult:
        """Poll until the query is satisfied, or raise `ObservationTimeout`.

        Bounded by construction: `timeout_s` is required and capped, the interval has a floor, and every sleep
        happens on the emergency stop so a stop ends the wait at once. The first look happens immediately, so a
        condition that is already true costs one screenshot and no delay."""
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) \
                or not 0 < timeout_s <= MAX_TIMEOUT_S:
            raise ObservationError(f"timeout_s must be greater than 0 and at most {MAX_TIMEOUT_S:g} seconds; "
                                   "a wait without a bound is not allowed.")
        if isinstance(interval_s, bool) or not isinstance(interval_s, (int, float)) or interval_s <= 0:
            raise ObservationError("interval_s must be a positive number of seconds.")
        interval = max(float(interval_s), MIN_INTERVAL_S)
        started = self._clock()
        attempts = 0
        last: ObservationResult | None = None
        while True:
            self._check_stop()
            last = self.observe(query)          # always a fresh frame: a cached one cannot become true
            attempts += 1
            if last.satisfied:
                return last
            waited = self._clock() - started
            remaining = timeout_s - waited
            if remaining <= 0:
                raise ObservationTimeout(
                    f"Waited {waited:.1f}s on {self.target_id} without the condition becoming true.",
                    waited_s=waited, attempts=attempts, last=last)
            if self._stop is not None:
                if self._stop.wait(min(interval, remaining)):
                    self._stop.check()          # triggered while sleeping: raise EmergencyStopped now
            else:
                time.sleep(min(interval, remaining))
