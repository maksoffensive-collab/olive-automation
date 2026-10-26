"""Editing a version-4 script (blocks joined by arrows): every edit a pure function returning a NEW `Script`.

`GraphSession` keeps the document with the same undo / redo / unsaved rules as the other editors, and accepts an
edit only once `validate_script` agrees it would be saved and read back as exactly that script -- so the editor never
holds a drawing it could not save, and never needs a second copy of the drawing's rules (`app.decide.graph`).

The edits a person makes on the canvas:

    new_script(name)               a start block, nothing else
    add_node(doc, kind, at)        a new block of that kind where it was dropped (its id made here)
    move_nodes(doc, {id: at})      blocks dragged to where they are now
    connect(doc, source, out, to)  an arrow from an output to a block -- replacing the arrow that output had; an arrow
                                   that would close a loop gets a repeat circle on it (a loop is only drawn through
                                   one, `app.decide.graph`), "until stopped by hand" until it is set otherwise
    disconnect(doc, source, out)   that output's arrow removed
    delete_nodes(doc, ids)         blocks and every arrow to and from them (never the start)
    replace_node(doc, node)        a block's settings changed (its id and place kept)
"""
from __future__ import annotations

from dataclasses import replace

from app.decide import (AGAIN, CHECK, CLICK, MACRO, REPEAT, START, STOP, WAIT, CheckNode, ClickNode, Edge,
                        GraphError, MacroNode, Node, RepeatNode, Script, StartNode, StopNode, WaitNode, validate_script)
from app.decide.format import MacroFormatError
from app.gui.macro_edit import UNDO_DEPTH, EditError

MIXED = "mixed"                  # the palette's "Mixed" block: a check whose parts may be of several kinds
_MAKERS = {WAIT: WaitNode, CHECK: CheckNode, MIXED: CheckNode, REPEAT: RepeatNode, CLICK: ClickNode,
           MACRO: MacroNode, STOP: StopNode}
REPEAT_OFFSET = (-150, 0)        # where a repeat circle put on a loop's arrow is drawn, from the arrow's source


def _refused(exc: Exception) -> EditError:
    return EditError(f"Not accepted: {exc}", "err_graph_rejected", reason=str(exc))


def new_script(name: str) -> Script:
    """A new script: its start block and nothing else. A name is required (the format's own rule)."""
    name = str(name).strip()
    if not name:
        raise EditError("A script needs a name.", "err_script_name")
    try:
        return Script(name, (StartNode("start", at=(0, 0)),))
    except GraphError as exc:
        raise _refused(exc) from None


def new_id(doc: Script, kind: str) -> str:
    taken = {n.id for n in doc.nodes}
    index = 1
    while f"{kind}{index}" in taken:
        index += 1
    return f"{kind}{index}"


def add_node(doc: Script, kind: str, at: tuple[int, int], **settings) -> tuple[Script, str]:
    """(the script with a new block, its id)."""
    maker = _MAKERS.get(kind)
    if maker is None:
        raise EditError(f"Unknown kind of block: {kind}.", "err_graph_kind")
    node_id = new_id(doc, REPEAT if kind == REPEAT else (CHECK if kind == MIXED else kind))
    if kind == MIXED:
        settings.setdefault("mixed", True)
    try:
        node = maker(node_id, at=(int(at[0]), int(at[1])), **settings)
        return replace(doc, nodes=doc.nodes + (node,)), node_id
    except (GraphError, TypeError, ValueError) as exc:
        raise _refused(exc) from None


def move_nodes(doc: Script, places: dict[str, tuple[int, int]]) -> Script:
    nodes = tuple(replace(n, at=(int(places[n.id][0]), int(places[n.id][1]))) if n.id in places else n
                  for n in doc.nodes)
    return replace(doc, nodes=nodes)


def _closes_loop(doc: Script, source: str, target: str) -> bool:
    """Whether an arrow source -> target would close a loop that passes through no repeat's "again"."""
    following: dict[str, list[str]] = {n.id: [] for n in doc.nodes}
    for edge in doc.edges:
        if not (isinstance(doc.node(edge.source), RepeatNode) and edge.output == AGAIN):
            following[edge.source].append(edge.target)
    seen, todo = set(), [target]
    while todo:                                        # can `source` be reached from `target`?
        node = todo.pop()
        if node == source:
            return True
        if node not in seen:
            seen.add(node)
            todo.extend(following[node])
    return False


def connect(doc: Script, source: str, output: str, target: str) -> Script:
    try:
        src = doc.node(source)
        doc.node(target)
    except GraphError as exc:
        raise _refused(exc) from None
    if output not in src.outputs:
        raise EditError(f"A {src.kind} block has no output {output}.", "err_graph_no_output")
    if isinstance(doc.node(target), StartNode):
        raise EditError("Nothing leads back to the start.", "err_graph_into_start")
    edges = tuple(e for e in doc.edges if not (e.source == source and e.output == output))
    rest = replace(doc, edges=edges)
    loop = not (isinstance(src, RepeatNode) and output == AGAIN) and (source == target
                                                                      or _closes_loop(rest, source, target))
    if not loop:
        return replace(rest, edges=edges + (Edge(source, output, target),))
    # a loop: through a repeat circle, "until stopped by hand" until the person sets it otherwise
    repeat_id = new_id(rest, REPEAT)
    at = (src.at[0] + REPEAT_OFFSET[0], src.at[1] + REPEAT_OFFSET[1])
    circle = RepeatNode(repeat_id, at=at)
    return replace(rest, nodes=rest.nodes + (circle,),
                   edges=edges + (Edge(source, output, repeat_id), Edge(repeat_id, AGAIN, target)))


def disconnect(doc: Script, source: str, output: str) -> Script:
    return replace(doc, edges=tuple(e for e in doc.edges if not (e.source == source and e.output == output)))


def delete_nodes(doc: Script, ids) -> Script:
    ids = {i for i in ids if not isinstance(doc.node(i), StartNode)}
    if not ids:
        return doc
    return replace(doc, nodes=tuple(n for n in doc.nodes if n.id not in ids),
                   edges=tuple(e for e in doc.edges if e.source not in ids and e.target not in ids))


def replace_node(doc: Script, node: Node) -> Script:
    """`node` in place of the block with its id (its arrows kept, but an output it no longer has loses its arrow:
    a repeat set to "until stopped by hand" has no "done")."""
    old = doc.node(node.id)
    if type(old) is not type(node):
        raise EditError("A block keeps its kind.", "err_graph_kind")
    nodes = tuple(node if n.id == node.id else n for n in doc.nodes)
    edges = tuple(e for e in doc.edges if e.source != node.id or e.output in node.outputs)
    return replace(doc, nodes=nodes, edges=edges)


def rename(doc: Script, name: str) -> Script:
    name = str(name).strip()
    if not name:
        raise EditError("A script needs a name.", "err_graph_name")
    return replace(doc, name=name)


class GraphSession:
    """One open version-4 editor: `apply`, `undo`, `redo`, `dirty`, `document`, `mark_saved` -- the interface the
    other editors have."""

    def __init__(self, script: Script) -> None:
        if not isinstance(script, Script):
            raise EditError("This is not a version-4 script.", "err_graph_not_v4")
        self.document = script
        self._saved = script
        self._undo: list[Script] = []
        self._redo: list[Script] = []

    @property
    def dirty(self) -> bool:
        return self.document != self._saved

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def apply(self, edit, *args, **kwargs):
        """Apply `edit(document, ...)`; returns what it returned besides the script (a new block's id), if any."""
        try:
            result = edit(self.document, *args, **kwargs)
        except (GraphError, MacroFormatError) as exc:
            raise _refused(exc) from None
        new, extra = (result if isinstance(result, tuple) else (result, None))
        if not isinstance(new, Script):
            raise EditError("This is not a version-4 script.", "err_graph_not_v4")
        try:
            validate_script(new)
        except (GraphError, MacroFormatError) as exc:
            raise _refused(exc) from None
        if new != self.document:
            self._undo.append(self.document)
            del self._undo[:-UNDO_DEPTH]
            self._redo.clear()
            self.document = new
        return extra

    def undo(self) -> bool:
        if not self._undo:
            return False
        self._redo.append(self.document)
        self.document = self._undo.pop()
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        self._undo.append(self.document)
        self.document = self._redo.pop()
        return True

    def mark_saved(self) -> None:
        self._saved = self.document

    # ---- what the controller asks of every editor -------------------------------------------------------------
    @property
    def empty(self) -> bool:
        """Only the start block: nothing to save yet (a new script left untouched leaves nothing behind)."""
        return len(self.document.nodes) == 1

    def macro(self) -> Script:
        if self.empty:
            raise EditError("Add at least one block before saving the script.", "err_script_empty")
        return self.document

    def rename(self, name: str) -> None:
        """Another name for the whole session -- its undo and redo states too, so undoing an edit never brings back
        a name that was given up (one a file has taken, say). Still unsaved afterwards."""
        renamed = rename(self.document, name)
        try:
            validate_script(renamed)
        except (GraphError, MacroFormatError) as exc:
            raise _refused(exc) from None

        def again(document):
            return replace(document, name=renamed.name)
        self._undo = [again(d) for d in self._undo]
        self._redo = [again(d) for d in self._redo]
        self.document = renamed


__all__ = ["GraphSession", "new_script", "add_node", "move_nodes", "connect", "disconnect", "delete_nodes",
           "replace_node", "rename", "new_id", "MIXED", "START", "STOP"]
