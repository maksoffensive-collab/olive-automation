"""Why a run stopped, where, and what to do about it (the person's list of 2026-10-25, item 9) -- as data.

A target's result keeps one line of detail (`TargetRun.detail`, stored in the history). A version-4 run writes it
as

    <reason>@<block id> {"param": value, ...}: <the English words>

-- the code a screen translates, the block it happened in, the numbers the sentence needs, and the words for the log
-- and older runs as "<code>: <words>". `parse` reads both back; the view turns a `Detail` into the person's words
(`app.gui.view.fmt.explain_*`). Pure: no Tk, no files.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from app.core.errors import said_params

_HEAD = re.compile(r"(?P<reason>[A-Za-z][A-Za-z0-9_]*)(?:@(?P<node>[A-Za-z0-9_-]{1,32}))?")
# the kind of a block, from the id the editor gives it (kind + number)
_KIND = re.compile(r"(start|wait|check|repeat|click|macro|stop)\d*\Z")


@dataclass(frozen=True)
class Detail:
    reason: str = ""
    node: str = ""
    params: dict = field(default_factory=dict)
    message: str = ""

    @property
    def node_kind(self) -> str:
        found = _KIND.match(self.node or "")
        return found.group(1) if found else ""


def of_error(exc: BaseException) -> str:
    """An error as a detail line: "<its type>: <words>", with its words in other languages when it carries them
    (`PlatformError.said`), so a screen can say it in the person's language."""
    said = said_params(exc)
    return encode(type(exc).__name__, None, said, str(exc)) if said else f"{type(exc).__name__}: {exc}"


def encode(reason: str, node: str | None = None, params=None, message: str = "") -> str:
    head = reason + (f"@{node}" if node else "")
    if params:
        head += " " + json.dumps(dict(params), ensure_ascii=False, sort_keys=True, default=str)
    return f"{head}: {message}" if message else head


def parse(detail: str) -> Detail:
    """A result's detail line, read back. A line in no known shape is kept whole as the message."""
    text = (detail or "").strip()
    head = _HEAD.match(text)
    if head is None:
        return Detail(message=text)
    reason, node = head.group("reason"), head.group("node") or ""
    rest = text[head.end():]
    params: dict = {}
    if rest.startswith(" {"):
        try:
            params, used = json.JSONDecoder().raw_decode(rest[1:])
            rest = rest[1 + used:]
        except ValueError:
            params = {}
        if not isinstance(params, dict):
            params = {}
    if rest.startswith(": "):
        return Detail(reason, node, params, rest[2:].strip())
    if not rest:
        return Detail(reason, node, params, "")
    return Detail(message=text)
