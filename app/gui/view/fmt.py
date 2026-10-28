"""Turning model values into the words a person reads.

The brief is explicit that a user must never meet the internal vocabulary: no `PutMultiTouch`, no `operationId`,
no `tap`/`key_press` as the description of a step. Everything a screen prints about an action goes through here,
and every string comes from the translator, so the same rules hold in both languages.

Typed text is summarised by its length rather than echoed, the way the recorder and the log already do it; the
editor's properties panel is the one place that shows the text itself, because that is where it is being changed.
"""
from __future__ import annotations

import time
from decimal import ROUND_DOWN, Decimal

from app.decide import Condition, Decision, RunMacro, TapMatch, WaitUntil
from app.gui import explain
from app.gui import flow_edit as fe
from app.gui.macro_edit import Block, BlockKind
from app.observe import ColorRegionQuery, PixelQuery, TemplateQuery, TextLinesQuery
from app.playback.macro import ActionOp, MacroAction
from app.vision.roi import Roi

_TITLE = {
    ActionOp.TAP: "act_tap", ActionOp.SECONDARY_TAP: "act_secondary_tap", ActionOp.DOUBLE_TAP: "act_double_tap",
    ActionOp.MOVE: "act_move", ActionOp.BUTTON_DOWN: "act_button_down", ActionOp.BUTTON_UP: "act_button_up",
    ActionOp.SECONDARY_DOWN: "act_secondary_down", ActionOp.SECONDARY_UP: "act_secondary_up",
    ActionOp.KEY_PRESS: "act_key_press", ActionOp.KEY_DOWN: "act_key_down", ActionOp.KEY_UP: "act_key_up",
    ActionOp.TYPE_TEXT: "act_type_text",
}


def key_label(value: str) -> str:
    """"a" -> "A", "f5" -> "F5", "enter" -> "Enter": the key as it is written on a keyboard."""
    return value.upper() if len(value) == 1 or (value[0] == "f" and value[1:].isdigit()) else value.capitalize()


def clip(text: str, limit: int) -> str:
    """A name shortened for a place of fixed width (a title, a breadcrumb); the full name stays where it wraps."""
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def number(ui, value: float, digits: int = 1) -> str:
    text = f"{value:.{digits}f}".rstrip("0").rstrip(".")
    return (text or "0").replace(".", ui.t("decimal_sep"))


def confidence(ui, value: float, threshold: float | None = None, *, places: int = 3) -> str:
    """A confidence never shown as more than it is: cut, not rounded (0.99984 is "0.999", never "1"), to `places`
    or to as many as `threshold` has, so the number shown is on the same side of the threshold as the real one."""
    if threshold is not None:
        places = max(places, min(9, -Decimal(repr(float(threshold))).normalize().as_tuple().exponent))
    cut = Decimal(repr(float(value))).quantize(Decimal(1).scaleb(-places), rounding=ROUND_DOWN)
    text = f"{cut:f}".rstrip("0").rstrip(".")                      # always has places: never strips a whole number
    return (text or "0").replace(".", ui.t("decimal_sep"))


def seconds(ui, ms: int) -> str:
    return ui.t("unit_seconds").format(value=number(ui, ms / 1000.0, 2))


def action_title(ui, action: MacroAction) -> str:
    return ui.t(_TITLE.get(action.op, "act_unknown"))


def action_detail(ui, action: MacroAction) -> str:
    if action.x is not None:
        return ui.t("at_point").format(x=action.x, y=action.y)
    if action.key is not None:
        return key_label(action.key.value)
    if action.text is not None:
        return ui.t("text_of_length").format(count=len(action.text))
    return ""


def block_title(ui, block: Block) -> str:
    return ui.t("act_pause") if block.kind is BlockKind.PAUSE else action_title(ui, block.action)


def block_detail(ui, block: Block) -> str:
    return seconds(ui, block.delay_ms) if block.kind is BlockKind.PAUSE else action_detail(ui, block.action)


def block_line(ui, block: Block) -> str:
    """One readable line: "Click — at (450, 320)"."""
    detail = block_detail(ui, block)
    return f"{block_title(ui, block)}{' — ' + detail if detail else ''}"


def counted(ui, key: str, count: int) -> str:
    """"1 action", not "1 actions". Both languages get their own singular form; anything else uses the plural
    template, which in Russian is phrased so that it reads correctly for every number."""
    return ui.t(f"{key}_one" if count == 1 else key).format(count=count)


def places_lines(ui, places, width: int = 44, most: int = 4) -> list[str]:
    """The windows a Full Windows recording's clicks go into: a heading, then each recorded window once (by its
    first title and program), on a line of its own (shortened to `width` characters), at most `most` of them."""
    first: dict[int, object] = {}
    for p in places:
        first.setdefault(p.window, p)
    named = [ui.t("details_place").format(title=p.title or "—", program=p.program) for p in first.values()]
    lines = [ui.t("details_places")] + ["  " + clip(name, width) for name in named[:most]]
    if len(named) > most:
        lines.append("  " + ui.t("details_more_steps").format(count=len(named) - most))
    return lines


def group_label(ui, group: str) -> str:
    """A provider's group label in the person's language where Olive knows it ("Open Windows"); any other, and a
    name such as "Full Windows", as the provider wrote it."""
    return ui.t("group_open_windows") if group == "Open Windows" else group


def target_detail(ui, entry, *, with_group: bool = False) -> str:
    """The second line of a target row: which provider offers it and that provider's own note about it.

    Every part is a string the PROVIDER supplied (`provider_id`, `group`, `size`); the view never decides what a
    target is. This is what keeps two same-named candidates -- the same program seen as a device by one provider
    and as a window by another -- apart before one of them is chosen."""
    parts = [entry.provider_id]
    if with_group and entry.group:
        parts.append(group_label(ui, entry.group))
    size = dict(entry.size_in).get(ui.t.language, entry.size)     # the provider's note in the person's language
    if size:
        parts.append(size)
    return "  ·  ".join(part for part in parts if part)


def target_hint(ui, entry) -> str:
    """What choosing this target means for the person's own screen. Stated for BOTH kinds: running in the
    background is an ordinary supported case, not a warning, and needing a visible window is a plain fact."""
    if getattr(entry, "whole_screen", False):
        return ui.t("target_whole_screen")
    return ui.t("target_background" if entry.background else "target_needs_focus")


def action_count(ui, count: int) -> str:
    return counted(ui, "count_actions", count)


def target_count(ui, count: int) -> str:
    return counted(ui, "count_targets", count)


def duration(ui, total_seconds: float) -> str:
    total = max(int(total_seconds), 0)
    return f"{total // 60:02d}:{total % 60:02d}"


def relative_time(ui, timestamp: float, *, now: float | None = None) -> str:
    """"just now" / "5 min ago" / "yesterday" / a date: a person reads a macro's age, not a clock value."""
    if not timestamp:
        return ui.t("time_never")
    moment = time.time() if now is None else now
    delta = moment - timestamp
    if delta < 60:
        return ui.t("time_just_now")
    if delta < 3600:
        return ui.t("time_minutes_ago").format(count=int(delta // 60))
    if delta < 86400 and time.localtime(timestamp).tm_mday == time.localtime(moment).tm_mday:
        return ui.t("time_hours_ago").format(count=int(delta // 3600))
    if time.strftime("%Y-%m-%d", time.localtime(timestamp)) == \
            time.strftime("%Y-%m-%d", time.localtime(moment - 86400)):
        return ui.t("time_yesterday")
    return time.strftime("%d.%m.%Y", time.localtime(timestamp))


def clock_time(timestamp: float) -> str:
    return time.strftime("%H:%M", time.localtime(timestamp))


def clock_time_precise(timestamp: float) -> str:
    """The time of day to the second: two runs in the same minute are still told apart."""
    return time.strftime("%H:%M:%S", time.localtime(timestamp))


def full_time(timestamp: float) -> str:
    """Date and time to the second, as the history keeps it."""
    return time.strftime("%d.%m.%Y %H:%M:%S", time.localtime(timestamp))


def day_label(ui, day: str, *, now: float | None = None) -> str:
    moment = time.time() if now is None else now
    if day == time.strftime("%Y-%m-%d", time.localtime(moment)):
        return ui.t("day_today")
    if day == time.strftime("%Y-%m-%d", time.localtime(moment - 86400)):
        return ui.t("day_yesterday")
    return day


def outcome_label(ui, outcome: str) -> str:
    return ui.t(f"outcome_{outcome}")


# ---- version-2 steps ---------------------------------------------------------------------------------------
def macro_size(ui, info) -> str:
    """"12 actions" for a recorded macro, "3 steps" for a script: the unit says which kind it is, in the person's
    words -- never a format version number."""
    if getattr(info, "version", 1) >= 2:
        return counted(ui, "count_steps", info.actions)
    return action_count(ui, info.actions)


def region_text(ui, region: Roi | None) -> str:
    if region is None:
        return ui.t("region_whole")
    return ui.t("region_at").format(x=region.x, y=region.y, w=region.width, h=region.height)


def condition_kind_label(ui, kind: str) -> str:
    return ui.t(f"cond_{kind}")


def condition_line(ui, condition: Condition) -> str:
    """What the condition asks, in words: "Text “Collect”", "not: Pixel (10, 20) = 255, 0, 0". A label the
    person gave it wins over the generated description."""
    q = condition.query
    if condition.label:
        what = condition.label
    elif isinstance(q, PixelQuery):
        what = ui.t("cond_pixel_line").format(x=q.point[0], y=q.point[1], color=fe.format_color(q.color))
    elif isinstance(q, ColorRegionQuery):
        what = ui.t("cond_color_line").format(color=fe.format_color(q.color), percent=round(q.min_ratio * 100))
    elif isinstance(q, TemplateQuery):
        what = ui.t("cond_template_line").format(w=q.template.width, h=q.template.height)
    elif isinstance(q, TextLinesQuery):
        lines = ui.t("cond_lines_or").join(
            ui.t("cond_line_present" if line.present else "cond_line_absent").format(text=line.text)
            for line in q.lines)
        what = ui.t("cond_lines_line").format(lines=lines)
    else:
        what = ui.t("cond_text_line").format(text=q.text)
    return what if condition.expected else ui.t("cond_not").format(what=what)


def tap_line(ui, tap: TapMatch) -> str:
    text = ui.t(_TITLE[tap.op])
    if tap.offset != (0, 0):
        text += " " + ui.t("offset_by").format(dx=tap.offset[0], dy=tap.offset[1])
    return text


def _block_word(ui, block) -> str:
    """One block of a branch in a few words."""
    if isinstance(block, TapMatch):
        return tap_line(ui, block)
    if isinstance(block, RunMacro):
        return ui.t("branch_run").format(name=block.macro.name or ui.t("unnamed"))
    return step_title(ui, block)


def branch_line(ui, branch) -> str:
    """A branch -- nothing, one block, or several -- in a few words: the first block, and how many follow."""
    if isinstance(branch, (list, tuple)):
        blocks = tuple(branch)
    else:
        blocks = () if branch is None else (branch,)
    if not blocks:
        return ui.t("branch_nothing")
    text = _block_word(ui, blocks[0])
    if len(blocks) > 1:
        text = ui.t("branch_more").format(first=text, count=len(blocks) - 1)
    return text


def step_title(ui, step) -> str:
    return ui.t(f"step_{fe.kind_of(step)}")


def step_detail(ui, step) -> str:
    if isinstance(step, WaitUntil):
        return ui.t("step_wait_detail").format(what=condition_line(ui, step.condition),
                                               seconds=number(ui, step.timeout_s, 2))
    if isinstance(step, Decision):
        return ui.t("step_if_detail").format(what=condition_line(ui, step.condition),
                                             then=branch_line(ui, step.then), otherwise=branch_line(ui, step.otherwise))
    if isinstance(step, RunMacro):
        return ui.t("step_run_detail").format(name=step.macro.name or ui.t("unnamed"),
                                              actions=action_count(ui, len(step.macro.actions)))
    if isinstance(step, TapMatch):
        return tap_line(ui, step)
    return ""


def delay_text(ui, seconds: float) -> str:
    """A start delay in words: "at once", "in 2 h 30 min", "in 3 min", "in 1 min 30 s", "in 45 s"."""
    if not seconds:
        return ui.t("canvas_start_now")
    if seconds < 60:
        when = ui.t("time_s").format(s=number(ui, seconds, 1))
    else:
        hours, rest = divmod(int(round(seconds)), 3600)
        minutes, rest = divmod(rest, 60)
        when = " ".join(([ui.t("time_h").format(h=hours)] if hours else [])
                        + ([ui.t("time_min").format(m=minutes)] if minutes else [])
                        + ([ui.t("time_s").format(s=rest)] if rest else []))
    return ui.t("canvas_start_in").format(time=when)


def countdown(seconds: float) -> str:
    """m:ss -- h:mm:ss from an hour on -- for a run that waits to start."""
    total = max(0, int(seconds + 0.999))
    hours, rest = divmod(total, 3600)
    if hours:
        return f"{hours}:{rest // 60:02d}:{rest % 60:02d}"
    return f"{total // 60}:{total % 60:02d}"


def step_line(ui, step) -> str:
    return f"{step_title(ui, step)} — {step_detail(ui, step)}"


# ---- why a run stopped, where, what to do (the person's list of 2026-10-25, item 9) -----------------------------
_BLOCK_KEYS = {"start": "graph_block_start", "wait": "graph_block_wait", "check": "graph_block_check",
               "repeat": "graph_block_repeat", "click": "graph_block_click", "macro": "graph_block_macro",
               "stop": "graph_block_stop"}


def _say(ui, key: str, values: dict) -> str:
    """The sentence of `key`, or "" when there is none (or it needs a number the detail does not have)."""
    text = ui.t(key)
    if text == key:
        return ""
    try:
        return text.format(**values)
    except (KeyError, IndexError, ValueError):
        return ""


def explain_lines(ui, detail: str) -> list[str]:
    """A result's detail in the person's words: why it stopped, in which block, and what to do -- the lines a
    history entry and the editor show (none for a result with nothing to say)."""
    d = explain.parse(detail)
    if not d.reason and not d.message:
        return []
    message = d.params.get(f"message_{ui.t.language}") or d.message      # a provider's words in this language
    values = {"message": message, "issues": d.params.get("issues", message), **d.params}
    if d.reason.startswith("query_"):
        why, fix = _say(ui, f"err_query_{d.reason[6:]}", values), ""
    else:
        why, fix = _say(ui, f"why_{d.reason}", values), _say(ui, f"fix_{d.reason}", values)
    why = why or message or d.reason
    lines = [ui.t("why_label").format(text=why)]
    if d.node:
        block = ui.t(_BLOCK_KEYS[d.node_kind]) if d.node_kind in _BLOCK_KEYS else d.node
        lines.append(ui.t("where_label").format(text=ui.t("where_block").format(block=block, id=d.node)))
    if fix:
        lines.append(ui.t("fix_label").format(text=fix))
    return lines
