"""Adapting to the size (the person's decisions of 2026-10-20): a recorded macro remembers the size it was recorded
at, a script the size of the picture its areas were picked on; "Adapt to the size" (scripts by default, recorded
macros only when chosen) scales clicks, points, areas and pictures to the size the target has NOW -- when its
proportions are the same (within PROPORTION_TOLERANCE); with other proportions it refuses rather than guess. Files
written before keep working, unchanged, as they were ("as is").

Fakes only: synthetic pictures, the simulated desktop of tests.test_gui (nothing is sent anywhere).
"""
import json
import unittest
import unittest.mock
from dataclasses import replace

from app.core.cancel import EmergencyStop
from app.decide import Condition, Decision, RunMacro, TapMatch, WaitUntil
from app.decide.format import ConditionalMacro, conditional_from_json, conditional_to_json, needs_version_3
from app.gui import flow_edit as fe
from app.gui.macro_edit import EditError
from app.observe import ObservationContext, PixelQuery, TemplateQuery, TextLine, TextLinesQuery
from app.observe.scale import scaled_query
from app.playback.engine import PlaybackRefused
from app.playback.format import macro_from_json, macro_to_json
from app.playback.macro import Macro
from app.playback.scaling import scaled_macro
from app.providers.recording import PointerButton as B, RecordedEvent as E, RecordedEventKind as K, Recording
from app.providers.recording_format import RecordingFormatError, recording_from_dict, recording_to_dict
from app.providers.size import PROPORTION_TOLERANCE, SizeChanged, check_size, fit
from app.vision.bitmap import Bitmap
from app.vision.roi import Roi
from app.windows.backend import MouseEvent
from app.windows.playback import create_window_playback
from app.windows.target import WindowVerifier
from tests import test_gui as gui_tests
from tests.test_vision_fast import cut, frame as frame_of, noise, _Pictures

RED, WHITE = (220, 20, 20), (255, 255, 255)


def click(x, y, *, size=(60, 40), target="t-1", provider="fake", adapt=False):
    rec = Recording(provider, target, (E(K.BUTTON_DOWN, 0, x, y, B.PRIMARY, 1), E(K.BUTTON_UP, 40, x, y, B.PRIMARY, 1)),
                    size=size)
    return Macro(rec, "Click", adapt=adapt)


# ---- sizes ---------------------------------------------------------------------------------------------------
class Sizes(unittest.TestCase):
    def test_a_size_is_two_positive_whole_numbers_or_nothing(self):
        self.assertEqual(check_size([1280, 720]), (1280, 720))
        self.assertIsNone(check_size(None))
        for bad in ((0, 10), (10, -1), (1.5, 2), (True, 3), (1, 2, 3), "1280x720", (10, 10 ** 6)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                check_size(bad)

    def test_the_same_proportions_give_a_scale_and_other_ones_are_refused(self):
        self.assertIsNone(fit((1280, 720), (1280, 720)))                       # nothing to adapt
        self.assertEqual(fit((1280, 720), (1920, 1080)), (1.5, 1.5))
        self.assertEqual(fit((100, 50), (200, 101)), (2.0, 2.02))             # within the tolerance
        self.assertEqual(PROPORTION_TOLERANCE, 0.02)
        for other in ((2560, 1080), (720, 1280), (200, 110)):
            with self.subTest(other=other), self.assertRaises(SizeChanged) as caught:
                fit((1280, 720), other)
            self.assertIn("1280x720", str(caught.exception))


# ---- files -------------------------------------------------------------------------------------------------------
class Files(unittest.TestCase):
    def test_a_recording_keeps_its_size_and_one_without_is_written_as_before(self):
        rec = click(11, 12).recording
        data = recording_to_dict(rec)
        self.assertEqual(data["size"], [60, 40])
        self.assertEqual(recording_from_dict(data), rec)
        plain = replace(rec, size=None)
        self.assertNotIn("size", recording_to_dict(plain))                    # files written before: unchanged
        self.assertEqual(recording_from_dict(recording_to_dict(plain)), plain)
        for bad in ([0, 40], "60x40", [60]):
            with self.subTest(bad=bad), self.assertRaises(RecordingFormatError):
                recording_from_dict(dict(data, size=bad))

    def test_a_recorded_macro_adapts_only_when_chosen_and_the_choice_is_kept(self):
        self.assertFalse(Macro(click(1, 1).recording).adapt)                  # "as is" by default
        adapting = click(11, 12, adapt=True)
        text = macro_to_json(adapting)
        self.assertTrue(json.loads(text)["adapt"])
        self.assertEqual(macro_from_json(text), adapting)
        self.assertNotIn("adapt", json.loads(macro_to_json(click(11, 12))))   # files written before: unchanged

    def test_a_script_keeps_its_size_and_choice_and_one_without_stays_version_2(self):
        steps = (WaitUntil(Condition.pixel((10, 10), RED), 1, 0.5),)
        plain = ConditionalMacro("Plain", steps)
        self.assertTrue(plain.adapt)                                           # scripts adapt by default
        self.assertFalse(needs_version_3(plain))
        self.assertEqual(json.loads(conditional_to_json(plain)[0])["version"], 2)
        for script in (replace(plain, size=(1280, 720)), replace(plain, size=(1280, 720), adapt=False),
                       replace(plain, adapt=False)):
            with self.subTest(size=script.size, adapt=script.adapt):
                text = conditional_to_json(script)[0]
                self.assertEqual(json.loads(text)["version"], 3)
                self.assertEqual(conditional_from_json(text), script)


# ---- recorded macros: their clicks follow the size ------------------------------------------------------------------
class RecordedMacros(unittest.TestCase):
    def test_clicks_are_scaled_by_the_size_and_kept_inside_it(self):
        scaled = scaled_macro(click(11, 12, size=(60, 40), adapt=True), (120, 80))
        self.assertEqual([(e.x, e.y) for e in scaled.recording.events], [(22, 24), (22, 24)])
        self.assertEqual((scaled.recording.size, scaled.adapt), ((120, 80), True))
        edge = scaled_macro(click(59, 39, size=(60, 40), adapt=True), (30, 20))
        self.assertEqual({(e.x, e.y) for e in edge.recording.events}, {(29, 19)})
        with self.assertRaises(SizeChanged):
            scaled_macro(click(11, 12, size=(60, 40), adapt=True), (120, 40))

    def window(self, macro, client_size):
        desk = gui_tests.World(gui_tests.info(hwnd=100, title="Notes", name="notes.exe", cls="NotesWnd", pid=10))
        desk.size = client_size
        stop = EmergencyStop()
        verifier = WindowVerifier(desk, stop)
        target = verifier.verify(100)
        rec = replace(macro.recording, provider_id=target.provider_id, target_id=target.target_id)
        engine = create_window_playback(replace(macro, recording=rec), target, verifier, desk, stop,
                                        sleep=lambda s: None)
        return engine, desk

    def downs(self, desk):
        return [e[3:] for e in desk.events if e[0] == "mouse" and e[2] is MouseEvent.LEFT_DOWN]

    def test_a_macro_that_adapts_clicks_where_the_same_place_now_is(self):
        engine, desk = self.window(click(11, 12, adapt=True), (120, 80))
        engine.run()
        self.assertEqual(self.downs(desk), [(22, 24)])

    def test_as_is_or_with_no_size_known_a_macro_clicks_where_it_was_recorded(self):
        for macro in (click(11, 12), replace(click(11, 12, adapt=True),
                                             recording=replace(click(11, 12).recording, size=None))):
            with self.subTest(adapt=macro.adapt, size=macro.recording.size):
                engine, desk = self.window(macro, (120, 80))
                engine.run()
                self.assertEqual(self.downs(desk), [(11, 12)])

    def test_other_proportions_are_refused_before_anything_is_sent(self):
        engine, desk = self.window(click(11, 12, adapt=True), (120, 40))
        self.assertIn("size_proportions_changed", engine.preflight().codes())
        with self.assertRaises(PlaybackRefused):
            engine.run()
        self.assertEqual(desk.events, [])


class Recording_(gui_tests.Base):
    def test_a_recording_whose_size_changed_while_recording_keeps_none(self):
        from app.providers.base import TargetIdentity
        from app.providers.recording import RecordingSession
        for sizes, kept in ((((60, 40), (60, 40)), (60, 40)), (((60, 40), (70, 40)), None)):
            with self.subTest(sizes=sizes):
                session = RecordingSession()
                session.start(TargetIdentity("fake", "t-1"))
                for size in sizes:
                    session.note_size(size)
                    session.record(K.POINTER_MOVE, x=1, y=1)
                self.assertEqual(session.stop().size, kept)

    def test_a_script_of_other_proportions_is_refused_before_it_does_anything(self):
        self.pick("Notes")
        macro = Macro(Recording("windows", self.c.target.target_id, click(11, 12).recording.events), "Inner")
        key = self.library.key_of(self.library.save(ConditionalMacro("Thin", (RunMacro(macro),), size=(300, 10))))
        self.c.refresh_macros()
        self.c.select_macro(key)
        self.c.set_run_refs([self.entry("Notes").ref])
        self.assertTrue(self.c.start_run(), self.c.status)
        import time
        deadline = time.time() + 10
        while self.c.run_active and time.time() < deadline:
            self.c.poll()
            time.sleep(0.02)
        result = self.c.run_results[0]
        self.assertEqual(result.state.value, "failed")
        self.assertIn("proportions", result.detail)
        self.assertEqual([e for e in self.world.events if e[0] in ("mouse", "key", "char")], [])   # nothing sent

    def test_a_script_whose_picture_would_be_too_small_is_refused_before_it_does_anything(self):
        import time
        self.pick("Notes")                                               # its picture: 64x48
        macro = Macro(Recording("windows", self.c.target.target_id, click(11, 12).recording.events), "Inner")
        steps = (RunMacro(macro), Decision(Condition.template(blocks(6, 2, 1)), (TapMatch(),)))   # 12x12 -> 3x3
        key = self.library.key_of(self.library.save(ConditionalMacro("Small", steps, size=(256, 192))))
        self.c.refresh_macros()
        self.c.select_macro(key)
        self.c.set_run_refs([self.entry("Notes").ref])
        self.assertTrue(self.c.start_run(), self.c.status)
        deadline = time.time() + 10
        while self.c.run_active and time.time() < deadline:
            self.c.poll()
            time.sleep(0.02)
        result = self.c.run_results[0]
        self.assertEqual(result.state.value, "failed")
        self.assertIn("too small", result.detail)
        self.assertEqual([e for e in self.world.events if e[0] in ("mouse", "key", "char")], [])   # nothing sent

    def test_testing_a_condition_on_other_proportions_says_so(self):
        self.pick("Notes")
        job = self.c.begin_probe(Condition.pixel((1, 1), RED), picked_on=(300, 10))
        try:
            outcome = job()
        except Exception as exc:                                          # what the window's job runner hands on
            outcome = exc
        self.assertIsNone(self.c.finish_probe(outcome, job))
        self.assertEqual(self.c.status.key, "msg_other_proportions")

    def test_the_macro_inside_a_script_that_adapts_clicks_where_the_place_now_is(self):
        import time
        self.pick("Notes")
        inner = Macro(Recording("windows", self.c.target.target_id, click(11, 12).recording.events, size=(30, 20)),
                      "Inner")
        for adapt, point in ((True, (22, 24)), (False, (11, 12))):        # the window is 60x40 now
            with self.subTest(adapt=adapt):
                self.world.events.clear()
                script = ConditionalMacro(f"Inner {adapt}", (RunMacro(inner),), adapt=adapt)
                key = self.library.key_of(self.library.save(script))
                self.c.refresh_macros()
                self.c.select_macro(key)
                self.c.set_run_refs([self.entry("Notes").ref])
                self.assertTrue(self.c.start_run(), self.c.status)
                deadline = time.time() + 10
                while self.c.run_active and time.time() < deadline:
                    self.c.poll()
                    time.sleep(0.02)
                self.assertEqual([e[3:] for e in self.world.events if e[0] == "mouse" and e[2] is MouseEvent.LEFT_DOWN],
                                 [point])

    def test_a_recording_remembers_the_size_of_the_window_it_was_made_in(self):
        from tests.test_recorder import SIZE                  # the client area the fake observer reports
        self.record()
        self.assertEqual(self.c.macro.recording.size, SIZE)


# ---- scripts: their areas follow the size ---------------------------------------------------------------------
class Scripts(unittest.TestCase):
    def test_points_areas_and_pictures_are_scaled_to_the_frame(self):
        template = Bitmap.solid(4, 2, RED)
        pixel = scaled_query(PixelQuery((10, 7), RED), 2.0, 2.0, 200, 100)
        self.assertEqual(pixel.point, (20, 14))
        area = scaled_query(TemplateQuery(template, region=Roi(10, 5, 30, 20)), 2.0, 2.0, 200, 100)
        self.assertEqual((area.region, area.template.width, area.template.height), (Roi(20, 10, 60, 40), 8, 4))
        text = scaled_query(TextLinesQuery((TextLine("OK"),), region=Roi(0, 0, 100, 50)), 0.5, 0.5, 50, 25)
        self.assertEqual(text.region, Roi(0, 0, 50, 25))
        corner = scaled_query(PixelQuery((99, 49), RED), 1.52, 1.52, 150, 75)
        self.assertEqual(corner.point, (149, 74))                              # kept inside the frame

    def context(self, bitmap, picked_on):
        return ObservationContext(_Pictures(bitmap), picked_on=picked_on)

    def test_a_script_made_on_one_size_finds_the_same_things_on_another(self):
        small = noise(60, 40, 3)
        pattern = cut(small, 20, 10, 6, 4)
        big = Bitmap(120, 80, bytes(v for y in range(80) for x in range(120)
                                    for v in small.pixel(x // 2, y // 2)))   # the same picture, twice as large
        context = self.context(big, (60, 40))
        found = context.observe(TemplateQuery(pattern, region=Roi(15, 5, 20, 15)))
        self.assertTrue(found.satisfied)
        self.assertEqual((found.matches[0].bounds.x, found.matches[0].bounds.y), (40, 20))
        self.assertTrue(context.observe(PixelQuery((20, 10), small.pixel(20, 10))).satisfied)

    def test_other_proportions_end_the_look_with_a_reason(self):
        context = self.context(Bitmap.solid(120, 40, WHITE), (60, 40))
        with self.assertRaises(SizeChanged):
            context.observe(PixelQuery((1, 1), WHITE))

    def test_without_a_size_or_as_is_nothing_is_scaled(self):
        context = self.context(Bitmap.solid(120, 80, WHITE).with_rect(10, 10, 1, 1, RED), None)
        self.assertTrue(context.observe(PixelQuery((10, 10), RED)).satisfied)

    def test_the_run_of_a_script_follows_its_choice(self):
        from app.gui import flows
        script = ConditionalMacro("S", (RunMacro(click(1, 1)),), size=(60, 40))

        class Services:
            stop = EmergencyStop()

            def capture(self, target):
                raise AssertionError("not captured here")

        target = type("T", (), {"target_id": "t-1", "provider_id": "fake"})()
        job = flows.FlowJob(script, target, Services(), ocr=None, dry_run=True, allow_different_target=False)
        self.assertEqual(job.context.picked_on, (60, 40))
        job = flows.FlowJob(replace(script, adapt=False), target, Services(), ocr=None, dry_run=True,
                            allow_different_target=False)
        self.assertIsNone(job.context.picked_on)

    def test_the_macros_inside_an_adapting_script_adapt_too(self):
        from app.decide.run import adapting
        steps = (RunMacro(click(1, 1)), Decision(Condition.pixel((1, 1), RED), (TapMatch(), RunMacro(click(2, 2)))))
        self.assertTrue(all(step.macro.adapt for step in (adapting(steps)[0], adapting(steps)[1].then[1])))
        self.assertEqual(adapting(steps)[1].then[0], TapMatch())


# ---- a picture looked for in any size (step D) -----------------------------------------------------------------
def blocks(cells: int, size: int, seed: int) -> Bitmap:
    """A picture of `cells` x `cells` flat squares of `size` pixels: it stays exact when enlarged by a whole number."""
    import random
    rnd = random.Random(seed)
    colours = [[tuple(rnd.randrange(256) for _ in range(3)) for _ in range(cells)] for _ in range(cells)]
    return Bitmap(cells * size, cells * size, bytes(v for y in range(cells * size) for x in range(cells * size)
                                                   for v in colours[y // size][x // size]))


class AnySize(unittest.TestCase):
    def test_a_picture_is_found_at_another_size_when_asked_to(self):
        from app.vision import template as tm
        if tm.fast_matcher() is None:
            self.skipTest("OpenCV is not installed")
        pattern = blocks(4, 2, 5)                                        # 8x8
        scene = noise(80, 60, 9).crop(0, 0, 80, 60)
        big = blocks(4, 4, 5)                                            # the same, twice as large: 16x16
        data = bytearray(scene.rgb)
        for r in range(16):
            i = ((30 + r) * 80 + 40) * 3
            data[i:i + 48] = big.rgb[r * 48:(r + 1) * 48]
        context = ObservationContext(_Pictures(Bitmap(80, 60, bytes(data))))
        self.assertFalse(context.observe(TemplateQuery(pattern)).satisfied)      # at its own size: not there
        found = context.observe(TemplateQuery(pattern, any_size=True))
        self.assertTrue(found.satisfied)
        self.assertEqual((found.matches[0].bounds, found.detail["scale"]), (Roi(40, 30, 16, 16), 2.0))

    def test_without_opencv_it_is_refused_before_anything_is_looked_at(self):
        from app.observe import InvalidQuery
        from app.vision import template as tm
        query = TemplateQuery(blocks(2, 2, 1), any_size=True)
        context = ObservationContext(_Pictures(Bitmap.solid(20, 20, WHITE)))
        with unittest.mock.patch.object(tm, "_FAST", [None]):
            with self.assertRaises(InvalidQuery):
                context.check_can_answer(query)

    def test_it_is_kept_in_the_file_and_the_form(self):
        from app.decide.format import _MemoryAssets, _condition, _condition_out
        query = TemplateQuery(blocks(2, 2, 1), any_size=True)
        assets = {}
        out = _condition_out(Condition(query), assets)
        self.assertTrue(out["any_size"])
        self.assertEqual(_condition(out, "c", _MemoryAssets(assets)).query, query)
        self.assertNotIn("any_size", _condition_out(Condition(replace(query, any_size=False)), {}))
        form = fe.condition_form(Condition(query))
        self.assertEqual(form.fields["any_size"], "1")
        self.assertTrue(fe.build_condition(form).query.any_size)


# ---- the editor -----------------------------------------------------------------------------------------------
class Editor(unittest.TestCase):
    def doc(self, size=None):
        return ConditionalMacro("S", (WaitUntil(Condition.pixel((10, 10), RED), 1, 0.5),), size=size)

    def test_the_first_areas_picked_give_the_script_its_size(self):
        step = WaitUntil(Condition.pixel((30, 20), RED), 1, 0.5)
        fitted, size = fe.fit_to_script(self.doc(), step, {"point": (200, 100)})
        self.assertEqual((fitted, size), (step, (200, 100)))
        d = fe.sized(fe.replace_item)(self.doc(), (0,), fitted, size=size)
        self.assertEqual(d.size, (200, 100))

    def test_areas_picked_on_another_size_are_scaled_into_the_scripts_own(self):
        step = WaitUntil(Condition.pixel((60, 40), RED), 1, 0.5)
        fitted, size = fe.fit_to_script(self.doc((100, 50)), step, {"point": (200, 100)})
        self.assertEqual((fitted.condition.query.point, size), ((30, 20), (100, 50)))
        with self.assertRaises(EditError):
            fe.fit_to_script(self.doc((100, 50)), step, {"point": (200, 200)})   # other proportions

    def test_only_the_parts_picked_on_another_size_are_scaled(self):
        template = blocks(4, 2, 1)                                             # 8x8
        step = WaitUntil(Condition(TemplateQuery(template, region=Roi(10, 6, 40, 20))), 1, 0.5)
        region_only, _ = fe.fit_to_script(self.doc((100, 50)), step, {"region": (200, 100)})
        query = region_only.condition.query
        self.assertEqual((query.region, query.template), (Roi(5, 3, 20, 10), template))
        picture_only, _ = fe.fit_to_script(self.doc((100, 50)), step, {"template": (200, 100)})
        query = picture_only.condition.query
        self.assertEqual((query.region, (query.template.width, query.template.height)), (Roi(10, 6, 40, 20), (4, 4)))

    def test_adapting_is_a_setting_of_the_script_with_undo(self):
        session = fe.FlowSession(self.doc())
        session.apply(fe.set_adapt, False)
        self.assertFalse(session.document.adapt)
        session.undo()
        self.assertTrue(session.document.adapt)
        empty = fe.set_adapt(fe.new_script("New"), False)
        self.assertFalse(fe.insert_item(empty, (0,), RunMacro(click(1, 1))).adapt)


# ---- the independent review of this stage (2026-10-20): each test failed before its fix -------------------------
class ReviewFixes(unittest.TestCase):
    def rig(self, picked_on):
        from tests.test_decide import Rig
        r = Rig()
        r.context = ObservationContext(r.screens, stop=r.stop, picked_on=picked_on)
        return r

    def test_a_click_beside_the_match_moves_with_the_size_too(self):
        from tests.test_observe import RED as SCENE_RED                 # the scene: 200x100, red at (30, 20)
        r = self.rig((100, 50))                                         # picked at half the size
        report = r.run(Decision(Condition.pixel((15, 10), SCENE_RED), then=TapMatch(offset=(3, 2))))
        self.assertEqual(report.state.value, "completed", report.message)
        self.assertEqual([c[:2] for c in r.input.calls], [("tap", (36, 24))])
        r = self.rig(None)                                              # as is: the offset as it was set
        r.run(Decision(Condition.pixel((30, 20), SCENE_RED), then=TapMatch(offset=(3, 2))))
        self.assertEqual([c[:2] for c in r.input.calls], [("tap", (33, 22))])

    def test_other_proportions_met_half_way_end_the_run_with_its_report(self):
        from tests.test_decide import a_macro
        r = self.rig((100, 40))                                         # the scene is 200x100
        report = r.run(RunMacro(a_macro()), WaitUntil(Condition.pixel((1, 1), RED), 1, 0.5))
        self.assertEqual((report.state.value, report.reason), ("failed", "size_proportions_changed"))
        self.assertEqual(len(report.steps), 1)                          # what was done before is still told
        self.assertEqual(len(r.input.calls), 1)

    def test_drag_points_outside_the_window_are_not_moved_to_its_edge(self):
        events = (E(K.BUTTON_DOWN, 0, 10, 10, B.PRIMARY, 1), E(K.POINTER_MOVE, 20, -5, 10),
                  E(K.POINTER_MOVE, 20, 70, 45), E(K.BUTTON_UP, 20, 70, 45, B.PRIMARY, 1))
        macro = Macro(Recording("fake", "t-1", events, size=(60, 40)), "Drag", adapt=True)
        scaled = scaled_macro(macro, (120, 80))
        self.assertEqual([(e.x, e.y) for e in scaled.recording.events], [(20, 20), (-10, 20), (140, 90), (140, 90)])
        engine, desk = RecordedMacros.window(self, macro, (120, 80))
        try:
            engine.run()
        except PlaybackRefused:
            pass
        self.assertFalse([e for e in desk.events if e[0] == "mouse" and (e[3] in (0, 119) or e[4] == 79)])

    def test_a_recorded_macro_set_to_adapt_can_be_put_in_a_script(self):
        from app.decide.format import validate_macro
        macro = click(11, 12, adapt=True)
        for built in (fe.build_run(macro), fe.build_branch(fe.RUN, macro=macro)):
            self.assertFalse(built.macro.adapt)                         # the script's own choice decides
        validate_macro(ConditionalMacro("S", (fe.build_run(macro),)))

    def test_a_window_whose_size_cannot_be_read_refuses_a_macro_that_adapts(self):
        for size in ((0, 0), None):
            with self.subTest(size=size):
                engine, desk = RecordedMacros.window(self, click(11, 12, adapt=True), size)
                self.assertIn("client_area_unreadable", engine.preflight().codes())
                with self.assertRaises(PlaybackRefused):
                    engine.run()
                self.assertEqual(desk.events, [])

    def test_file_forms_the_writer_never_writes_are_refused(self):
        from app.decide.format import MacroFormatError
        steps = (WaitUntil(Condition.pixel((10, 10), RED), 1, 0.5),)
        text = conditional_to_json(ConditionalMacro("S", steps, size=(60, 40)))[0]
        for screen in (None, {"size": None, "adapt": True}):
            with self.subTest(screen=screen), self.assertRaises(MacroFormatError):
                conditional_from_json(json.dumps(dict(json.loads(text), screen=screen)))
        with self.assertRaises(RecordingFormatError):
            recording_from_dict(dict(recording_to_dict(click(1, 1).recording), size=None))

    def test_a_picture_is_never_made_too_small_to_tell_apart(self):
        from app.observe import InvalidQuery
        with self.assertRaises(InvalidQuery):
            scaled_query(TemplateQuery(blocks(4, 2, 1)), 0.25, 0.25, 50, 25)     # 8x8 -> 2x2
        small = scaled_query(TemplateQuery(blocks(4, 2, 1)), 0.5, 0.5, 50, 25)
        self.assertEqual((small.template.width, small.template.height), (4, 4))
        tiny = scaled_query(TemplateQuery(Bitmap.solid(3, 3, RED)), 1.1, 1.1, 50, 25)
        self.assertEqual(tiny.template.width, 3)                                 # as small as it was picked: kept

    def test_any_size_never_looks_for_a_picture_smaller_than_it_can_be_told_apart(self):
        from app.observe import observer
        sizes = []

        def looked(frame, template, **kw):
            sizes.append((template.width, template.height))
            return []
        with unittest.mock.patch.object(observer, "fast_matcher", lambda: object()), \
                unittest.mock.patch.object(observer, "match_template", looked):
            ObservationContext(_Pictures(noise(80, 60, 9))).observe(TemplateQuery(blocks(3, 2, 1), any_size=True))
        self.assertEqual(min(sizes), (4, 4))                                     # 6x6: 4x4 at the smallest

    def test_a_picture_is_resized_once_per_size_and_the_stop_is_heard_between_sizes(self):
        from app.core.errors import EmergencyStopped
        from app.observe import observer
        real = Bitmap.resized
        context = ObservationContext(_Pictures(noise(80, 60, 9)), picked_on=(40, 30))
        query = TemplateQuery(blocks(4, 2, 5))
        with unittest.mock.patch.object(Bitmap, "resized", autospec=True, side_effect=real) as resized:
            context.observe(query)
            context.observe(query)
        self.assertEqual(resized.call_count, 1)
        stop, looks, armed = EmergencyStop(), [], []

        def looked(frame, template, **kw):
            looks.append(template.width)
            if armed:
                stop.trigger("test")
            return []
        context = ObservationContext(_Pictures(noise(80, 60, 9)), stop=stop)
        query = TemplateQuery(blocks(4, 2, 5), any_size=True)
        with unittest.mock.patch.object(observer, "fast_matcher", lambda: object()), \
                unittest.mock.patch.object(observer, "match_template", looked):
            context.observe(query)                       # every size made once: the next look resizes nothing
            looks.clear()
            armed.append(True)
            with self.assertRaises(EmergencyStopped):
                context.observe(query)
        self.assertEqual(len(looks), 1)                                          # not the other sizes


    # ---- the second review round (2026-10-20)
    def test_a_point_outside_stays_outside_when_the_macro_is_made_smaller(self):
        events = (E(K.BUTTON_DOWN, 0, 10, 10, B.PRIMARY, 1), E(K.POINTER_MOVE, 20, -1, 10),
                  E(K.POINTER_MOVE, 20, 60, -1), E(K.BUTTON_UP, 20, 60, -1, B.PRIMARY, 1))
        scaled = scaled_macro(Macro(Recording("fake", "t-1", events, size=(60, 40)), "Drag", adapt=True), (30, 20))
        self.assertEqual([(e.x, e.y) for e in scaled.recording.events], [(5, 5), (-1, 5), (30, -1), (30, -1)])

    def test_the_parts_a_condition_no_longer_has_do_not_give_the_script_its_size(self):
        template = blocks(4, 2, 1)
        condition = Condition(TemplateQuery(template))
        fitted, size = fe.fit_condition(ConditionalMacro("S", (WaitUntil(condition, 1, 0.5),)), condition,
                                        {"point": (800, 600), "template": (1920, 1080)})
        self.assertEqual((fitted, size), (condition, (1920, 1080)))     # the point is of a pixel it no longer is

    def test_a_script_run_as_is_refuses_a_pick_on_another_size(self):
        doc = ConditionalMacro("S", (WaitUntil(Condition.pixel((10, 10), RED), 1, 0.5),), size=(100, 50), adapt=False)
        step = WaitUntil(Condition.pixel((60, 40), RED), 1, 0.5)
        with self.assertRaises(EditError) as caught:
            fe.fit_to_script(doc, step, {"point": (200, 100)})              # it would be asked at (30, 20): elsewhere
        self.assertEqual(caught.exception.key, "err_flow_as_is_other_size")
        self.assertEqual(fe.fit_to_script(doc, step, {"point": (100, 50)}), (step, (100, 50)))

    def test_a_picture_too_small_at_this_size_is_refused_before_anything_is_done(self):
        from app.observe import InvalidQuery
        context = ObservationContext(_Pictures(noise(60, 40, 2)), picked_on=(240, 160))
        context.check_size((PixelQuery((1, 1), RED),))
        with self.assertRaises(InvalidQuery):
            context.check_size((PixelQuery((1, 1), RED), TemplateQuery(blocks(6, 2, 1))))   # 12x12 -> 3x3

    def test_the_stop_is_heard_inside_one_resize_and_resized_pictures_are_bounded_in_bytes(self):
        from app.core.errors import EmergencyStopped
        from app.observe import context as context_module
        rows = []

        def check():
            rows.append(1)
            if len(rows) == 3:
                raise EmergencyStopped("test")
        with self.assertRaises(EmergencyStopped):
            Bitmap.solid(10, 10, RED).resized(20, 20, check=check)
        self.assertEqual(len(rows), 3)                                    # stopped at its third row of twenty
        stop = EmergencyStop()
        context = ObservationContext(_Pictures(noise(80, 60, 9)), stop=stop, picked_on=(40, 30))
        stop.trigger("test")
        with self.assertRaises(EmergencyStopped):
            context._resized(blocks(4, 2, 5), 16, 16)
        context = ObservationContext(_Pictures(noise(80, 60, 9)))
        with unittest.mock.patch.object(context_module, "MAX_PICTURE_BYTES", 16 * 16 * 3):
            for side in (16, 12, 10):
                context._resized(blocks(4, 2, 5), side, side)
                self.assertLessEqual(sum(len(p.rgb) for p in context._pictures.values()), 16 * 16 * 3)


class FastSearchReview(unittest.TestCase):
    def setUp(self):
        from app.vision import template as tm
        if tm.fast_matcher() is None:
            self.skipTest("OpenCV is not installed")

    def test_a_large_bright_picture_is_found_exactly_at_full_confidence(self):
        import random
        from app.vision import fast, template as tm
        rnd = random.Random(1)                          # float32 rounding of 7252 at the exact place
        side, n = 520, 500
        scene = Bitmap(side, side, bytes(rnd.randrange(245, 256) for _ in range(side * side * 3)))
        pattern = cut(scene, 9, 7, n, n)
        self.assertEqual(fast.candidates(scene.rgb, side, side, Roi(0, 0, side, side), pattern.rgb, n, n, 0.0, 100),
                         [(0, 7, 9)])
        found = tm.match_template(frame_of(scene), pattern, min_confidence=1.0)
        self.assertEqual([(m.x, m.y, m.confidence) for m in found], [(9, 7, 1.0)])

    def test_the_exact_measure_is_done_in_pieces_of_bounded_memory(self):
        from app.vision import fast
        self.assertEqual(fast.chunk_for(9, 7), fast.CHUNK)
        for side in (100, 500, 2048):
            with self.subTest(side=side):
                per_position = side * side * 3 * fast.BYTES_PER_VALUE
                self.assertLessEqual(fast.chunk_for(side, side) * per_position, max(fast.MEMORY, per_position))
        scene = noise(60, 40, 4)
        pattern = cut(scene, 20, 10, 9, 7)
        args = (scene.rgb, 60, 40, Roi(0, 0, 60, 40), pattern.rgb, 9, 7, 9 * 7 * 3 * 255 * 0.4, 10 ** 6)
        whole = fast.candidates(*args)
        with unittest.mock.patch.object(fast, "MEMORY", 1):                     # one position at a time
            self.assertEqual(fast.candidates(*args), whole)
        with unittest.mock.patch.object(fast, "chunk_for", wraps=fast.chunk_for) as sized:
            fast.candidates(*args)
        sized.assert_called_once_with(9, 7)


if __name__ == "__main__":
    unittest.main()
