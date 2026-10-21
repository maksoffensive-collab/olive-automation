"""Text lines: one text condition, several texts, each "is there" or "is not there" -- on the person's decision of
2026-10-19 (true when at least one line holds; each text is looked for anywhere in the chosen area).

Covers the whole path with fakes only (a fake OCR reader, synthetic frames, no screen): the query and its answer,
the decision rule that a click "on the match" needs every line to be one that must BE there, the file format, and
the editor's form -- including that a text saved before text lines existed stays exactly as it was until edited.
"""
import json
import unittest

from app.decide import Condition, ConditionalMacro, Decision, TapMatch, WaitUntil
from app.decide.condition import evaluate
from app.decide.decision import decide, tap_macro
from app.decide.errors import DecisionError
from app.decide.format import MacroFormatError, conditional_from_json, conditional_to_json
from app.gui import flow_edit as fe
from app.gui.macro_edit import EditError
from app.observe import (MAX_TEXT_LINES, TEXT, TEXT_LINES, InvalidQuery, ObservationResult, OcrUnavailable,
                         TextLine, TextLinesQuery, TextQuery)
from app.playback.macro import ActionOp
from app.vision.roi import Roi
from tests.test_observe import FakeOcr, block, context


def lines(*pairs, **options):
    return TextLinesQuery(tuple(TextLine(text, present) for text, present in pairs), **options)


# ---- the query and its answer ------------------------------------------------------------------------------
class Observing(unittest.TestCase):
    def observe(self, query, *blocks, **ocr_options):
        ocr = FakeOcr(blocks, **ocr_options)
        ctx, _ = context(ocr=ocr)
        return ctx.observe(query), ocr

    def test_any_line_that_holds_is_enough_and_the_text_is_found_anywhere_in_a_piece_of_text(self):
        result, _ = self.observe(lines(("урон", True), ("здоровье", True)),
                                 block("Критический урон 120", 10, 20, 90, 12), block("Опыт", 10, 40, 30, 12))
        self.assertEqual(result.kind, TEXT_LINES)
        self.assertTrue(result.found and result.satisfied)
        self.assertEqual(result.bounds, Roi(10, 20, 90, 12))          # the piece of text that contains it
        self.assertEqual([(d["text"], d["seen"], d["holds"]) for d in result.detail["lines"]],
                         [("урон", True, True), ("здоровье", False, False)])

    def test_case_and_runs_of_spaces_do_not_matter(self):
        result, _ = self.observe(lines(("критический урон", True)), block("Критический    УРОН"))
        self.assertTrue(result.found)

    def test_a_line_that_must_be_absent_holds_when_its_text_is_not_there_with_nothing_to_point_at(self):
        result, _ = self.observe(lines(("здоровье", False)), block("Победа"))
        self.assertTrue(result.found and result.satisfied)
        self.assertEqual(result.matches, ())
        self.assertIsNone(result.center)                              # nothing to click "on"
        result, _ = self.observe(lines(("урон", True), ("здоровье", False)), block("Здоровье 100"))
        self.assertFalse(result.found)                                # neither line holds

    def test_the_match_is_the_first_line_in_order_that_holds_because_it_is_there(self):
        result, _ = self.observe(lines(("Ошибка", False), ("урон", True), ("здоровье", True)),
                                 block("здоровье", 0, 0, 40, 10, conf=0.99), block("урон", 50, 50, 30, 10, conf=0.6))
        self.assertEqual(result.bounds, Roi(50, 50, 30, 10))
        self.assertEqual(result.best.detail["line"], 1)

    def test_the_area_is_read_once_whatever_the_number_of_lines(self):
        _, ocr = self.observe(lines(("a", True), ("b", False), ("c", True)), block("x"))
        self.assertEqual(len(ocr.calls), 1)

    def test_a_measured_confidence_below_the_minimum_is_not_seen(self):
        result, _ = self.observe(lines(("урон", True), min_confidence=0.8), block("урон", conf=0.5))
        self.assertFalse(result.found)

    def test_what_a_query_may_hold(self):
        for bad in (lambda: TextLine("   "), lambda: TextLine(""), lambda: TextLine("a", "yes"),
                    lambda: TextLinesQuery(()), lambda: TextLinesQuery((TextLine("a"),) * (MAX_TEXT_LINES + 1)),
                    lambda: TextLinesQuery(("a",)), lambda: TextLinesQuery([TextLine("a")])):
            with self.assertRaises(InvalidQuery):
                bad()
        self.assertTrue(lines(("a", True), ("b", True)).points_at_match)
        self.assertFalse(lines(("a", True), ("b", False)).points_at_match)

    def test_without_a_text_reader_it_is_refused_before_anything_is_captured(self):
        ctx, provider = context()
        with self.assertRaises(OcrUnavailable):
            ctx.check_can_answer(lines(("a", True)))
        self.assertEqual(provider.captures, 0)

    def test_only_text_lines_may_be_found_with_nothing_to_point_at(self):
        result, _ = self.observe(lines(("x", False)), block("y"))
        with self.assertRaises(ValueError):
            ObservationResult(TEXT, True, result.source, result.captured_at, result.observed_at)


# ---- deciding and clicking -------------------------------------------------------------------------------------
class Deciding(unittest.TestCase):
    def test_a_click_on_the_match_needs_every_line_to_be_one_that_must_be_there(self):
        mixed = Condition.text_lines([("урон", True), ("здоровье", False)])
        with self.assertRaises(DecisionError):
            Decision(mixed, TapMatch())
        with self.assertRaises(DecisionError):
            Decision(mixed.negated(), otherwise=TapMatch())          # the "found" side again
        Decision(Condition.text_lines([("урон", True), ("здоровье", True)]), TapMatch())

    def test_the_click_goes_to_the_middle_of_the_piece_of_text_that_contains_the_found_text(self):
        """A reader returns whole lines, so in a longer line that middle is beside the word itself (P0-3)."""
        ocr = FakeOcr([block("Критический урон", 10, 20, 90, 12)])
        ctx, _ = context(ocr=ocr)
        decision = Decision(Condition.text_lines([("здоровье", True), ("урон", True)]), TapMatch())
        choice = decide(decision, evaluate(decision.condition, ctx))
        action = choice.macro.actions[0]
        self.assertEqual((action.op, action.x, action.y), (ActionOp.TAP, 55, 26))

    def test_nothing_is_clicked_when_only_an_absent_line_held(self):
        ocr = FakeOcr([block("Победа")])
        ctx, _ = context(ocr=ocr)
        result = evaluate(Condition.text_lines([("здоровье", False)]), ctx)
        self.assertTrue(result.satisfied)
        with self.assertRaises(DecisionError):
            tap_macro(result.observation, TapMatch())


# ---- the file ---------------------------------------------------------------------------------------------------
class FileFormat(unittest.TestCase):
    def saved(self, condition):
        text, _ = conditional_to_json(ConditionalMacro("t", (WaitUntil(condition, 5, 0.5),)))
        return text

    def test_text_lines_are_saved_and_read_back_exactly(self):
        condition = Condition.text_lines([("урон", True), ("здоровье", False)], region=Roi(1, 2, 30, 40),
                                         language="ru", min_confidence=0.5, label="Бой")
        text = self.saved(condition)
        stored = json.loads(text)["steps"][0]["condition"]
        self.assertEqual(stored["type"], "text_lines")
        self.assertEqual(stored["lines"], [{"text": "урон", "present": True}, {"text": "здоровье", "present": False}])
        self.assertEqual(conditional_from_json(text).steps[0].condition, condition)

    def test_a_file_that_says_anything_else_is_refused(self):
        good = json.loads(self.saved(Condition.text_lines([("урон", True)])))

        def with_lines(value):
            data = json.loads(json.dumps(good))
            data["steps"][0]["condition"]["lines"] = value
            return json.dumps(data)
        for bad in ([], [{"text": "урон"}], [{"text": "урон", "present": "yes"}], [{"text": "  ", "present": True}],
                    [{"text": "a\nb", "present": True}], [{"text": "a", "present": True, "x": 1}],
                    [{"text": "a", "present": True}] * (MAX_TEXT_LINES + 1), "урон"):
            with self.subTest(bad=bad), self.assertRaises(MacroFormatError):
                conditional_from_json(with_lines(bad))
        data = json.loads(json.dumps(good))
        del data["steps"][0]["condition"]["ignore_case"]
        with self.assertRaises(MacroFormatError):
            conditional_from_json(json.dumps(data))


# ---- the editor's form ------------------------------------------------------------------------------------------
class Form(unittest.TestCase):
    def test_a_new_condition_is_text_with_one_empty_line_and_needs_one_written(self):
        form = fe.condition_form(None)
        self.assertEqual((form.kind, form.lines), (fe.TEXT, [("", True)]))
        with self.assertRaises(EditError) as caught:
            fe.build_condition(form)
        self.assertEqual(caught.exception.key, "err_flow_no_text")
        form.lines = [("a", True)] * (MAX_TEXT_LINES + 1)
        with self.assertRaises(EditError) as caught:
            fe.build_condition(form)
        self.assertEqual(caught.exception.key, "err_flow_too_many_lines")

    def test_a_text_saved_before_text_lines_stays_exactly_itself_until_it_is_edited(self):
        old = Condition.text("Hi", match="exact", ignore_case=False, expected=False, region=Roi(0, 0, 9, 9),
                             language="en", label="old")
        form = fe.condition_form(old)
        # one line, and the condition's own "is not there": so an "IF it is not there ... ELSE click it" stays one
        self.assertEqual((form.lines, form.expected), ([("Hi", True)], False))
        self.assertEqual(fe.build_condition(form), old)                           # untouched: the very same query
        form.lines = [("Hi there", True)]
        edited = fe.build_condition(form)
        self.assertEqual(edited.query, TextLinesQuery((TextLine("Hi there"),), True, Roi(0, 0, 9, 9), "en", 0.0,
                                                      False))
        fe.build_decision(edited, None, TapMatch())                               # its ELSE click still allowed
        form.lines, form.region = [("Hi", True)], None                            # another area: edited too
        self.assertIsInstance(fe.build_condition(form).query, TextLinesQuery)
        form = fe.condition_form(old)
        form.expected = True                                                      # "is there" now: edited too
        self.assertEqual(fe.build_condition(form).query.expected, True)
        self.assertIsInstance(fe.build_condition(form).query, TextLinesQuery)

    def test_saved_text_lines_keep_their_own_case_rule_and_expectation(self):
        """A file may say "case matters" or "NOT (these lines)": opening and applying never turns that around."""
        for condition in (Condition.text_lines([("a", True)], ignore_case=False),
                          Condition.text_lines([("a", True), ("b", False)], expected=False, label="not")):
            self.assertEqual(fe.build_condition(fe.condition_form(condition)), condition)

    def test_text_lines_come_back_into_the_same_lines(self):
        condition = Condition.text_lines([("a", True), ("b", False)], region=Roi(1, 1, 5, 5), language="ru")
        self.assertEqual(fe.build_condition(fe.condition_form(condition)), condition)

    def test_a_click_on_the_match_with_a_line_that_must_be_absent_is_explained(self):
        condition = Condition.text_lines([("a", True), ("b", False)])
        with self.assertRaises(EditError) as caught:
            fe.build_decision(condition, TapMatch(), None)
        self.assertEqual(caught.exception.key, "err_flow_tap_absent_line")

    def test_new_conditions_are_pixel_colour_template_or_text(self):
        self.assertEqual(fe.OFFERED_KINDS, (fe.PIXEL, fe.TEMPLATE, fe.TEXT))
        self.assertEqual(fe.condition_kind(Condition.text_lines([("a", True)])), fe.TEXT)
        self.assertEqual(fe.condition_kind(Condition(TextQuery("a"))), fe.TEXT)


if __name__ == "__main__":
    unittest.main()
