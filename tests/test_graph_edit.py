"""Editing a version-4 script on the canvas: every edit a new, valid script; undo and redo; a loop drawn by hand gets
its repeat circle (the person's design of 2026-10-25). Pure functions, no window."""
import unittest

from app.decide.graph import AGAIN, DONE, FOUND, MANUAL, MISSING, NEXT, TIMES, CheckNode, RepeatNode, StartNode
from app.gui import graph_edit as ge
from app.gui.macro_edit import EditError


def session():
    return ge.GraphSession(ge.new_script("Test"))


class Edits(unittest.TestCase):
    def test_blocks_are_added_where_they_are_dropped_with_ids_of_their_own(self):
        s = session()
        first = s.apply(ge.add_node, "wait", (10, 20))
        second = s.apply(ge.add_node, "wait", (30, 40))
        mixed = s.apply(ge.add_node, ge.MIXED, (0, 0))
        self.assertEqual((first, second, mixed), ("wait1", "wait2", "check1"))
        self.assertEqual(s.document.node("wait2").at, (30, 40))
        self.assertTrue(s.document.node("check1").mixed)
        with self.assertRaises(EditError):
            s.apply(ge.add_node, "python", (0, 0))

    def test_an_output_leads_to_one_block_a_new_arrow_replaces_the_old(self):
        s = session()
        a, b = s.apply(ge.add_node, "wait", (0, 100)), s.apply(ge.add_node, "wait", (0, 200))
        s.apply(ge.connect, "start", NEXT, a)
        s.apply(ge.connect, "start", NEXT, b)
        self.assertEqual([(e.source, e.target) for e in s.document.edges], [("start", b)])
        s.apply(ge.disconnect, "start", NEXT)
        self.assertEqual(s.document.edges, ())
        with self.assertRaises(EditError):
            s.apply(ge.connect, a, NEXT, "start")                          # nothing leads back to the start
        with self.assertRaises(EditError):
            s.apply(ge.connect, a, FOUND, b)                               # a wait has no "found"

    def test_an_arrow_that_closes_a_loop_gets_a_repeat_circle(self):
        s = session()
        check = s.apply(ge.add_node, "check", (0, 100))
        wait = s.apply(ge.add_node, "wait", (0, 200))
        s.apply(ge.connect, "start", NEXT, check)
        s.apply(ge.connect, check, MISSING, wait)
        s.apply(ge.connect, wait, NEXT, check)
        repeat = next(n for n in s.document.nodes if isinstance(n, RepeatNode))
        self.assertEqual(repeat.mode, MANUAL)
        self.assertEqual({(e.source, e.output, e.target) for e in s.document.edges},
                         {("start", NEXT, check), (check, MISSING, wait), (wait, NEXT, repeat.id),
                          (repeat.id, AGAIN, check)})
        s.apply(ge.connect, check, FOUND, check)                          # a block to itself: a circle too
        self.assertEqual(sum(isinstance(n, RepeatNode) for n in s.document.nodes), 2)

    def test_a_repeat_set_to_until_stopped_by_hand_loses_its_done_arrow(self):
        s = session()
        repeat = s.apply(ge.add_node, "repeat", (0, 100), mode=TIMES, times=3)
        wait = s.apply(ge.add_node, "wait", (0, 200))
        s.apply(ge.connect, "start", NEXT, repeat)
        s.apply(ge.connect, repeat, DONE, wait)
        s.apply(ge.replace_node, RepeatNode(repeat, at=(0, 100)))           # back to "until stopped by hand"
        self.assertEqual([(e.source, e.output) for e in s.document.edges], [("start", NEXT)])

    def test_deleting_takes_the_arrows_with_it_but_never_the_start(self):
        s = session()
        a = s.apply(ge.add_node, "wait", (0, 100))
        s.apply(ge.connect, "start", NEXT, a)
        s.apply(ge.delete_nodes, {a, "start"})
        self.assertEqual([n.id for n in s.document.nodes], ["start"])
        self.assertEqual(s.document.edges, ())

    def test_undo_redo_and_unsaved_changes(self):
        s = session()
        self.assertFalse(s.dirty)
        a = s.apply(ge.add_node, "wait", (0, 100))
        s.apply(ge.move_nodes, {a: (50, 60)})
        self.assertTrue(s.dirty and s.can_undo)
        s.undo()
        self.assertEqual(s.document.node(a).at, (0, 100))
        s.redo()
        self.assertEqual(s.document.node(a).at, (50, 60))
        s.mark_saved()
        self.assertFalse(s.dirty)
        with self.assertRaises(EditError):
            s.apply(ge.replace_node, CheckNode(a))                         # a block keeps its kind
        with self.assertRaises(EditError):
            s.apply(ge.rename, "  ")
        self.assertIsInstance(s.document.nodes[0], StartNode)


if __name__ == "__main__":
    unittest.main()
