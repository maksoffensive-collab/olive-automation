"""What a provider can do about a refusal -- only when the person says yes (the person's decision of 2026-10-27).

A refusal (a `PlatformError`) may carry a `Remedy` in its `remedy`. The screens never know what it does: they show
its `question` in the person's language, call `run` on a worker thread only after an explicit yes, and then say
`done`. `run` belongs to the provider: every wait in it is bounded, the emergency stop ends it (raised as the stop,
never as another error), and a failure is raised in the person's words (`PlatformError.said`), saying what was done
before it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping


@dataclass(frozen=True, eq=False)
class Remedy:
    title: Mapping[str, str]                 # by language code: "en", "ru"
    question: Mapping[str, str]              # what will happen, and what it costs (a program closed, say)
    run: Callable[[], None]
    doing: Mapping[str, str] = field(default_factory=dict)    # said while it runs
    done: Mapping[str, str] = field(default_factory=dict)     # said when it has

    def said(self, words: Mapping[str, str], language: str) -> str:
        """`words` in `language`, else in English."""
        return words.get(language) or words.get("en", "")


def remedy_of(exc: BaseException) -> Remedy | None:
    remedy = getattr(exc, "remedy", None)
    return remedy if isinstance(remedy, Remedy) else None
