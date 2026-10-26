"""The version-4 script editor in the real (never shown) application window (the person's design of 2026-10-25).

The rules a new script had in the editor before carry over: it writes nothing until it has a block and is saved, a
name taken meanwhile is offered another and never written over, leaving with unsaved work asks. And what is new:
it fills the screen and gives it back, its blocks come from the palette (a click or a drag), arrows join them, a
loop drawn by hand gets its repeat circle, the block that runs is lit. The simulated desktop of tests.test_gui;
nothing is sent anywhere.
"""
import unittest

from app.gui import graph_edit as ge
from tests import test_redesign as redesign
from tests.test_redesign import flow, tk


@unittest.skipIf(tk is None, "tkinter is not available")
class GraphEditor(redesign.Window):
    def setUp(self):
        super().setUp()
        self.graph = self.app.screens["graph"]

    def new_script(self, name="Daily"):
        self.stub_dialog("NewScript", (name, ""))
        self.app.show_screen("scripts")
        self.app.screens["scripts"]._new_script()
        self.assertEqual(self.app.screen, "graph")
        self.app.render()

    def test_a_new_script_opens_full_screen_and_gives_the_screen_back(self):
        self.new_script()
        self.assertFalse(self.app.nav.winfo_manager())             # the navigation and the header step aside
        self.assertFalse(self.app.header.winfo_manager())
        self.graph.go_back()                                         # nothing in it: leaves without a question
        self.assertEqual(self.app.screen, "scripts")
        self.assertTrue(self.app.nav.winfo_manager() and self.app.header.winfo_manager())
        self.assertIsNone(self.c.editor)

    def test_saving_waits_for_the_first_block(self):
        self.new_script()
        self.assertEqual(self.graph.lbl_state.cget("text"), self.app.t("editor_new_empty"))
        self.assertIn("disabled", self.graph.btn_save.state())
        self.graph.add_block("wait", (0, 150))                       # a click in the palette
        self.app.render()
        self.assertNotIn("disabled", self.graph.btn_save.state())
        self.app._key_save()
        saved = [i for i in self.library.list() if i.version == 4]
        self.assertEqual([i.name for i in saved], ["Daily"])
        self.assertEqual([n.id for n in self.library.load(saved[0].path).nodes], ["start", "wait1"])

    def test_a_block_added_after_the_chosen_one_is_joined_to_it_and_a_loop_gets_its_circle(self):
        self.new_script()
        self.graph._select("start")
        self.graph.add_block("check", (0, 150))
        self.graph.add_block("wait", (0, 300))                       # after the check's first free output
        doc = self.c.editor.document
        self.assertEqual({(e.source, e.output, e.target) for e in doc.edges},
                         {("start", "next", "check1"), ("check1", "found", "wait1")})
        self.graph._connect("wait1", "next", "check1")              # drawn back up by hand
        doc = self.c.editor.document
        self.assertIn("repeat1", {n.id for n in doc.nodes})
        self.assertEqual(doc.edge_from("repeat1", "again").target, "check1")

    def test_a_block_dropped_from_the_palette_lands_where_it_was_dropped(self):
        self.new_script()
        self.graph._dropped("stop", (240, 480))
        self.assertEqual(self.c.editor.document.node("stop1").at, (240, 480))

    def test_delete_removes_the_chosen_block_or_arrow_but_never_the_start(self):
        self.new_script()
        self.graph._select("start")
        self.graph.add_block("wait", (0, 150))
        self.graph._select_edge(("start", "next"))
        self.graph.delete_selected()
        self.assertEqual(self.c.editor.document.edges, ())
        self.graph._select("start")
        self.graph.delete_selected()
        self.graph._select("wait1")
        self.graph.delete_selected()
        self.assertEqual([n.id for n in self.c.editor.document.nodes], ["start"])

    def test_a_block_not_set_yet_is_marked(self):
        self.new_script()
        self.graph.add_block("macro", (0, 150))
        self.graph.add_block("click", (0, 300))
        views = {v.id: v for v in self.graph.canvas_view.nodes.values()}
        self.assertTrue(views["macro1"].warning and views["click1"].warning)

    def test_each_block_opens_its_own_settings(self):
        from app.gui.view import graph_panels as gp
        self.new_script()
        for kind, panel in (("wait", gp.WaitPanel), ("check", gp.CheckPanel), (ge.MIXED, gp.CheckPanel),
                            ("repeat", gp.RepeatPanel), ("click", gp.ClickPanel), ("macro", gp.MacroPanel),
                            ("stop", gp.StopPanel)):
            node_id = self.c.editor_apply_graph(ge.add_node, kind, (0, 0))
            self.graph._select(node_id)
            self.assertIsInstance(self.graph.panel, panel, kind)
        self.graph._select("start")
        self.assertIsInstance(self.graph.panel, gp.StartPanel)

    def test_a_setting_typed_in_a_panel_is_one_edit_that_can_be_undone(self):
        self.new_script()
        self.graph.add_block("wait", (0, 150))
        self.graph._select("wait1")
        self.graph.panel._seconds("2,5")
        self.assertEqual(self.c.editor.document.node("wait1").seconds, 2.5)
        self.graph.panel._seconds("0")                                # refused, in words; nothing changes
        self.assertEqual(self.c.editor.document.node("wait1").seconds, 2.5)
        self.assertTrue(self.graph.lbl_message.cget("text"))
        self.c.editor_undo()
        self.assertEqual(self.c.editor.document.node("wait1").seconds, 1.0)

    def built_but_taken(self):
        """A new script with a block, whose name a file made meanwhile has taken."""
        self.new_script()
        self.graph.add_block("wait", (0, 150))
        taken = self.library.save(flow("Daily"))
        return taken, taken.read_bytes()

    def test_saving_under_a_name_taken_meanwhile_asks_for_another_and_keeps_the_work(self):
        taken, before = self.built_but_taken()
        self.stub_dialog("TextPrompt", "Daily 2")
        self.graph.save()
        self.assertEqual(taken.read_bytes(), before)
        self.assertFalse(self.c.editing_new)
        saved = self.library.load(self.library.path_for("Daily 2"))
        self.assertEqual((saved.name, [n.id for n in saved.nodes]), ("Daily 2", ["start", "wait1"]))

    def test_declining_another_name_keeps_the_new_script_open_and_unsaved(self):
        taken, before = self.built_but_taken()
        self.stub_dialog("TextPrompt", None)
        self.graph.save()
        self.assertTrue(self.c.editing_new and self.c.editor_dirty)
        self.assertEqual(self.c.status.category, "exists")
        self.assertEqual(taken.read_bytes(), before)

    def test_leaving_with_save_asks_for_a_free_name_too(self):
        self.built_but_taken()
        self.stub_dialog("UnsavedChanges", "save")
        self.stub_dialog("TextPrompt", "Daily 2")
        self.graph.go_back()
        self.assertIsNone(self.c.editor)
        self.assertTrue(self.library.path_for("Daily 2").exists())

    def test_the_list_of_scripts_shows_a_new_script_and_its_blocks(self):
        self.new_script("Shown")
        self.graph._select("start")
        self.graph.add_block("wait", (0, 150))
        self.graph.save()
        self.graph.go_back()
        scripts = self.app.screens["scripts"]
        info = next(i for i in self.c.macros if i.name == "Shown")
        scripts.select_row(info.key)
        self.app.render()
        text = scripts.lbl_steps.cget("text")
        self.assertIn(self.app.t("graph_block_start"), text)
        self.assertIn(self.app.t("graph_block_wait"), text)

    def test_leaving_another_page_with_unsaved_work_asks_first(self):
        self.new_script()
        self.graph.add_block("wait", (0, 150))
        self.stub_dialog("UnsavedChanges", None)                       # the question cancelled: stay
        self.app.show_screen("history")
        self.assertEqual(self.app.screen, "graph")
        self.assertTrue(self.c.editor_dirty)


class GraphRuns(redesign.gui_tests.Base):
    """A version-4 script run by the controller on the simulated desktop: its blocks along the arrows, a recorded
    macro named by a block played through the provider's own path, and the blocks lit as they run."""

    COLOUR = (200, 60, 30)                     # the simulated window's pixels (tests.test_windows_provider)

    def save(self, script):
        key = self.library.key_of(self.library.save(script))
        self.c.refresh_macros()
        return key

    def run_script(self, key, *, dry_run=False):
        import time
        self.pick("Notes")
        self.c.select_macro(key)
        self.c.set_run_refs([self.entry("Notes").ref])
        self.assertTrue(self.c.start_run(dry_run=dry_run), self.c.status)
        deadline = time.time() + 10
        while self.c.run_active and time.time() < deadline:
            self.c.poll()
            time.sleep(0.01)
        self.assertFalse(self.c.run_active)
        return self.c.run_results[0]

    def script(self, *nodes, edges=()):
        from app.decide import Edge, Script, StartNode
        return Script("Graph", (StartNode("start"),) + nodes, tuple(Edge(*e) for e in edges))

    def test_a_check_leads_along_its_arrow_and_the_blocks_are_lit(self):
        from app.decide import CheckNode, Condition, Part, StopNode
        there = Part(Condition.pixel((1, 1), self.COLOUR))
        key = self.save(self.script(CheckNode("check1", parts=(there,)), StopNode("stop1"),
                                    StopNode("stop2", success=False, message="no"),
                                    edges=(("start", "next", "check1"), ("check1", "found", "stop1"),
                                           ("check1", "missing", "stop2"))))
        result = self.run_script(key)
        self.assertEqual(result.state.value, "completed", result.detail)
        self.assertEqual(self.c.graph_lit, {"start": "visited", "check1": "visited", "stop1": "visited"})

    def test_a_macro_block_plays_the_recorded_macro_it_names(self):
        from app.decide import MacroNode
        from tests.test_gui_v2 import recorded
        self.pick("Notes")
        inner = recorded(provider="windows", target=self.c.target.target_id, name="Inner")
        inner_key = self.library.key_of(self.library.save(inner))
        key = self.save(self.script(MacroNode("macro1", macro=inner_key), edges=(("start", "next", "macro1"),)))
        result = self.run_script(key)
        self.assertEqual(result.state.value, "completed", result.detail)
        self.assertTrue([e for e in self.world.events if e[0] == "mouse"])         # it clicked, as recorded

    def test_a_macro_block_whose_macro_is_gone_refuses_the_run_before_anything(self):
        from app.decide import MacroNode
        key = self.save(self.script(MacroNode("macro1", macro="gone.macro.json"), edges=(("start", "next", "macro1"),)))
        result = self.run_script(key)
        self.assertEqual(result.state.value, "refused")
        self.assertIn("macro_missing", result.detail)
        self.assertEqual(self.world.events, [])

    def test_running_from_the_editor_needs_it_saved_and_a_target(self):
        from app.gui import graph_edit as ge
        self.assertTrue(self.c.new_script("Editor run"))
        self.c.editor_apply_graph(ge.add_node, "wait", (0, 100), seconds=0.05)
        self.c.editor_apply_graph(ge.connect, "start", "next", "wait1")
        self.assertFalse(self.c.run_from_editor())
        self.assertEqual(self.c.status.key, "msg_graph_save_first")
        self.assertTrue(self.c.save_editor(), self.c.status)
        self.assertFalse(self.c.run_from_editor())
        self.assertEqual(self.c.status.key, "msg_graph_need_target")
        self.pick("Notes")
        self.assertTrue(self.c.run_from_editor(), self.c.status)
        self.c.cancel()



# ---- the third review (2026-10-25): each test failed before its fix ------------------------------------------
@unittest.skipIf(tk is None, "tkinter is not available")
class ReviewOfTheEditor(redesign.Window):
    def setUp(self):
        super().setUp()
        self.graph = self.app.screens["graph"]

    def recorded(self, name, folder=""):
        from tests.test_gui_v2 import recorded
        return self.library.key_of(self.library.save(recorded(name=name), folder=folder))

    def test_the_old_editor_still_offers_the_recorded_macros(self):
        """M1: the new choices method shadowed the old one; the v2 editor's macro chooser broke."""
        from app.gui import flow_edit as fe
        self.recorded("Inner")
        key = self.library.key_of(self.library.save(flow("Collect")))
        self.c.refresh_macros()
        self.assertTrue(self.c.open_editor(key))
        self.app.show_screen("editor")
        self.app.screens["editor"].tree.selection_set("s:0")
        self.app.screens["editor"]._add_step(fe.RUN)                   # raised AttributeError before
        self.assertTrue(all(hasattr(i, "folder") for i in self.c.recorded_macros()))

    def test_two_macros_of_one_name_are_told_apart_and_the_one_chosen_is_kept(self):
        """M2: choosing by name took the first macro of that name."""
        first, second = self.recorded("Daily"), self.recorded("Daily", folder="Other")
        self.c.refresh_macros()
        labels = [label for _key, label in self.c.recorded_macro_choices()]
        self.assertEqual(len(set(labels)), len(labels))
        self.stub_dialog("NewScript", ("Uses", ""))
        self.app.show_screen("scripts")
        self.app.screens["scripts"]._new_script()
        self.graph.add_block("macro", (0, 150))
        panel = self.graph.panel
        index = [key for key, _label in panel.choices].index(second)
        panel.box.current(index)
        panel._chosen()
        self.assertEqual(self.c.editor.document.node("macro1").macro, second)
        self.assertNotEqual(first, second)

    def test_the_editor_shows_and_chooses_its_target(self):
        """M3: the target chip was hidden with the page header."""
        self.stub_dialog("NewScript", ("Target", ""))
        self.app.show_screen("scripts")
        self.app.screens["scripts"]._new_script()
        self.assertEqual(self.graph.btn_target.cget("text"), self.app.t("graph_target_none"))
        self.c.select(self.entry("Notes").ref)
        self.app.render()
        self.assertEqual(self.graph.btn_target.cget("text"), self.app.t("graph_target").format(name="Notes"))

    def test_leaving_by_another_page_goes_to_that_page(self):
        """Minor 9: leaving a changed script by a shortcut and giving the changes up landed on Scripts."""
        self.stub_dialog("NewScript", ("Leave", ""))
        self.app.show_screen("scripts")
        self.app.screens["scripts"]._new_script()
        self.graph.add_block("wait", (0, 150))
        self.stub_dialog("UnsavedChanges", "discard")
        self.app.show_screen("history")
        self.assertEqual(self.app.screen, "history")
        self.assertIsNone(self.c.editor)
        self.assertTrue(self.app.nav.winfo_manager())

    def test_a_value_the_block_refuses_is_said_not_raised(self):
        """Minor 8: an invisible character pasted into a stop's words raised from the Tk callback."""
        self.stub_dialog("NewScript", ("Words", ""))
        self.app.show_screen("scripts")
        self.app.screens["scripts"]._new_script()
        self.graph.add_block("stop", (0, 150))
        self.graph.panel.safely(self.graph.panel._message, "Нет\u200bкнопки")
        self.assertTrue(self.graph.lbl_message.cget("text"))
        self.assertEqual(self.c.editor.document.node("stop1").message, "")

    def test_picking_on_the_whole_screen_steps_olive_out_of_the_way_and_back(self):
        """M8: a picture of the whole screen taken from the full-screen editor was a picture of Olive itself."""
        from app.gui.view import graph_panels
        self.stub_dialog("NewScript", ("Whole", ""))
        self.app.show_screen("scripts")
        self.app.screens["scripts"]._new_script()
        self.c.select(next(e for e in self.c.entries if e.whole_screen).ref)
        self.graph.add_block("check", (0, 150))
        calls = []
        self.app.root.iconify = lambda: calls.append("iconify")
        self.app.root.deiconify = lambda: calls.append("deiconify")
        self.app.root.after = lambda _ms, fn: fn()                      # the wait for the desktop to repaint
        self.app.run_job = lambda job, done: done(job())
        original = graph_panels.OverlayPicker
        graph_panels.OverlayPicker = lambda *a, **k: type("Picker", (), {"show": lambda self: None})()
        self.addCleanup(setattr, graph_panels, "OverlayPicker", original)
        self.graph.panel.pick("template", lambda screen, answer: None)
        self.assertEqual(calls, ["iconify", "deiconify"])

    def test_a_macro_recorded_from_the_editor_goes_to_the_list_and_into_a_block(self):
        """M7 (untested): record from the editor, name it, and its block is put after the chosen block."""
        from app.windows.backend import MouseObservationKind as M
        self.stub_dialog("NewScript", ("Recorded here", ""))
        self.app.show_screen("scripts")
        self.app.screens["scripts"]._new_script()
        self.c.select(self.entry("Notes").ref)
        self.graph._select("start")
        self.graph.record_macro()
        self.assertTrue(self.c.recording_active)
        self.observer.mouse(M.MOVE, 10, 20, 10)
        self.observer.mouse(M.PRIMARY_DOWN, 10, 20, 20)
        self.observer.mouse(M.PRIMARY_UP, 10, 20, 30)
        self.stub_dialog("TextPrompt", "Click the button")
        self.graph.record_macro()                                        # pressed again: stop, name, save, block
        self.assertFalse(self.c.recording_active)
        info = next(i for i in self.c.macros if i.name == "Click the button")
        node = next(n for n in self.c.editor.document.nodes if n.kind == "macro")
        self.assertEqual(node.macro, info.key)
        self.assertEqual(self.c.editor.document.edge_from("start", "next").target, node.id)

    def test_a_drag_keeps_its_place_while_the_window_redraws(self):
        """Minor 9: the window redraws every tenth of a second during a run, which put a dragged block back."""
        from app.gui.view.graph_canvas import NodeView
        view = self.graph.canvas_view
        view.show([NodeView("a", "wait", "wait", "Wait", at=(0, 0))], [])
        view._press = {"node": "a", "x": 0, "y": 0, "at": (0, 0), "moving": True}
        view.nodes["a"] = NodeView("a", "wait", "wait", "Wait", at=(120, 60))
        view.show([NodeView("a", "wait", "wait", "Wait", at=(0, 0))], [])
        self.assertEqual(view.nodes["a"].at, (120, 60))


class ReviewOfTheRuns(GraphRuns):
    def test_a_file_changed_outside_is_not_run_from_the_editor(self):
        """M4: Run from the editor ran the version saved outside, with the old drawing shown."""
        from app.gui import graph_edit as ge
        self.pick("Notes")
        self.assertTrue(self.c.new_script("Outside"))
        self.c.editor_apply_graph(ge.add_node, "wait", (0, 100), seconds=0.05)
        self.assertTrue(self.c.save_editor(), self.c.status)
        path = self.c.macro_by_key(self.c.editor_key).path
        path.write_text(path.read_text(encoding="utf-8").replace('"seconds": 0.05', '"seconds": 0.06'),
                        encoding="utf-8")
        self.assertFalse(self.c.run_from_editor())
        self.assertEqual(self.c.status.key, "msg_editor_changed_outside")
        self.assertFalse(self.c.run_active)

    def test_a_macro_that_knows_its_size_follows_it_and_refuses_other_proportions(self):
        """M5: a macro block played its macro unscaled at another size, other proportions not refused."""
        from app.decide import MacroNode
        from app.playback.macro import Macro
        from app.providers.recording import PointerButton as B, RecordedEvent as E, RecordedEventKind as K
        from app.providers.recording import Recording
        self.pick("Notes")                                               # 60x40 now
        thin = Macro(Recording("windows", self.c.target.target_id, (E(K.BUTTON_DOWN, 0, 5, 5, B.PRIMARY, 1),
                                                                    E(K.BUTTON_UP, 20, 5, 5, B.PRIMARY, 1)),
                               size=(300, 10)), "Thin")
        inner = self.library.key_of(self.library.save(thin))
        key = self.save(self.script(MacroNode("macro1", macro=inner), edges=(("start", "next", "macro1"),)))
        result = self.run_script(key)
        self.assertEqual(result.state.value, "refused")
        self.assertIn("proportions", result.detail)
        self.assertEqual(self.world.events, [])

    def test_a_click_at_a_point_of_other_proportions_is_refused_before_any_block_acts(self):
        """M7: the check of `GraphJob._acting` before the first block had no test."""
        from app.decide import ClickNode, WaitNode
        key = self.save(self.script(WaitNode("wait1", seconds=0.05), ClickNode("click1", point=(1, 1), size=(300, 10)),
                                    edges=(("start", "next", "wait1"), ("wait1", "next", "click1"))))
        result = self.run_script(key)
        self.assertEqual(result.state.value, "failed")
        self.assertIn("size_proportions_changed", result.detail)
        self.assertEqual(result.completed, 1)                           # only the start was reached
        self.assertEqual(self.world.events, [])

    def test_a_target_gone_during_the_start_delay_ends_the_run_before_anything(self):
        """M7: the check of the target after a start delay had no test."""
        import time
        from app.decide import ClickNode, Edge, Script, StartNode
        script = Script("Later", (StartNode("start", delay_s=0.3), ClickNode("click1", point=(1, 1))),
                        (Edge("start", "next", "click1"),))
        key = self.save(script)
        self.pick("Notes")
        self.c.select_macro(key)
        self.c.set_run_refs([self.entry("Notes").ref])
        self.assertTrue(self.c.start_run(), self.c.status)
        del self.world.windows[100]                                      # closed while it waits to start
        deadline = time.time() + 10
        while self.c.run_active and time.time() < deadline:
            self.c.poll()
            time.sleep(0.01)
        self.assertEqual(self.c.run_results[0].state.value, "failed")
        self.assertEqual(self.world.events, [])

    def test_stop_ends_a_v4_run_through_the_controller(self):
        import time
        from app.decide import CheckNode, Condition, Part, RepeatNode
        absent = Part(Condition.pixel((1, 1), (1, 2, 3)))
        key = self.save(self.script(CheckNode("check1", parts=(absent,)), RepeatNode("repeat1"),
                                    edges=(("start", "next", "check1"), ("check1", "missing", "repeat1"),
                                           ("repeat1", "again", "check1"))))
        self.pick("Notes")
        self.c.select_macro(key)
        self.c.set_run_refs([self.entry("Notes").ref])
        self.assertTrue(self.c.start_run(), self.c.status)
        time.sleep(0.3)
        self.c.cancel()
        deadline = time.time() + 10
        while self.c.run_active and time.time() < deadline:
            self.c.poll()
            time.sleep(0.01)
        self.assertEqual(self.c.run_results[0].state.value, "stopped")
        self.assertTrue(self.c.graph_lit)

    def test_what_a_playback_could_not_let_go_of_is_said(self):
        """M6: "still held" was never reported for a version-4 run."""
        from app.decide import GraphReport
        from app.gui.flows import GraphJob
        job = GraphJob.__new__(GraphJob)
        job.error, job.cancel_reason = None, ""
        job.report = GraphReport("cancelled", "t", "click1", "cancelled", released=("left button",), held=("Shift",))
        state, _done, detail = job.outcome()
        self.assertEqual(state, "stopped")
        self.assertIn("still held: Shift", detail)
        self.assertEqual(job.let_go(), (("left button",), ("Shift",)))


class GraphRefusals(unittest.TestCase):
    """M7: what a target cannot do refuses a version-4 script before anything."""

    def test_a_wrong_provider_and_a_missing_capability(self):
        from app.decide import ClickNode, Edge, MacroNode, Script, StartNode
        from app.gui.flows import graph_refusal
        from app.providers.target import Capability as C
        from tests.test_gui_v2 import recorded
        from tests.test_playback import FakeTarget
        target = FakeTarget(frozenset({C.TAP, C.SCREENSHOT, C.POINTER_BUTTON}))     # a button, but no pointer move
        macro_script = Script("M", (StartNode("start"), MacroNode("macro1", macro="m")), (Edge("start", "next", "macro1"),))
        self.assertIn("wrong_provider", graph_refusal(macro_script, target, {"m": recorded(provider="other")}))
        drag = Script("D", (StartNode("start"), ClickNode("click1", how="drag", point=(1, 1), to=(5, 5))), ())
        self.assertIn("unsupported_capability", graph_refusal(drag, target, {}))
        tap = Script("T", (StartNode("start"), ClickNode("click1", point=(1, 1))), ())
        self.assertEqual(graph_refusal(tap, target, {}), "")


class Details(unittest.TestCase):
    def test_a_detail_is_written_and_read_back(self):
        from app.gui import explain
        line = explain.encode("query_picture_larger_than_area", "check1", {"picture": "30×20", "area": "10×10"},
                              "does not fit: here")
        self.assertEqual(explain.parse(line), explain.Detail("query_picture_larger_than_area", "check1",
                                                             {"area": "10×10", "picture": "30×20"},
                                                             "does not fit: here"))
        self.assertEqual(explain.parse("timeout: Waited 2s"), explain.Detail("timeout", "", {}, "Waited 2s"))
        self.assertEqual(explain.parse("emergency_stop@wait2").node_kind, "wait")
        self.assertEqual(explain.parse("odd: words: here").message, "words: here")
        self.assertEqual(explain.parse("Something odd").message, "Something odd")
        self.assertEqual(explain.parse('x@y {"broken": : here').reason, "")       # kept whole


class ScreenPlaces(unittest.TestCase):
    """M7: where a target's picture lies on the screen (the picker lays its picture there)."""

    def test_a_window_the_whole_screen_and_a_device(self):
        from app.windows.target import FullWindowsTarget
        from tests.test_composite_services import two_provider_composite
        composite, _ = two_provider_composite()
        entries = composite.discover()
        window = composite.verify(next(e for e in entries if e.title == "Notes"))
        whole = composite.verify(next(e for e in entries if e.whole_screen))
        device = composite.verify(next(e for e in entries if e.title.startswith("LDPlayer")))
        self.assertIsNone(composite.screen_rect(window))                # the fake desktop cannot say: no overlay
        self.assertIsInstance(whole, FullWindowsTarget)
        self.assertEqual(composite.screen_rect(whole), tuple(whole.layout))
        self.assertIsNone(composite.screen_rect(device))

    def test_the_win32_client_rectangle(self):
        import contextlib
        import sys
        if sys.platform != "win32":
            self.skipTest("Windows only")
        from app.windows.win32 import Win32Backend
        b = Win32Backend()
        b._dpi_aware = contextlib.nullcontext
        b._IsWindow = lambda h: True

        def rect(h, r):
            r._obj.right, r._obj.bottom = 640, 480
            return True

        def origin(h, p):
            p._obj.x, p._obj.y = 100, 50
            return True
        b._GetClientRectBase, b._ClientToScreenBase = rect, origin
        self.assertEqual(b.client_screen_rect(5), (100, 50, 640, 480))
        b._IsWindow = lambda h: False
        self.assertIsNone(b.client_screen_rect(5))


if __name__ == "__main__":
    unittest.main()
