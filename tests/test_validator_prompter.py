"""The validator's prompters read answers, skips and retries the way the spec describes."""

import io
import unittest
from collections.abc import Iterator

from conformance.validator.findings import Finding, Verdict
from conformance.validator.prompter import (
    Answer,
    AutoAnswerPrompter,
    Reply,
    TerminalPrompter,
    human_verdict,
    printable,
)


def terminal(*lines: str) -> tuple[TerminalPrompter, io.StringIO]:
    replies: Iterator[str] = iter(lines)
    output = io.StringIO()
    return TerminalPrompter(read_line=lambda _prompt: next(replies), output=output), output


class TerminalPrompterTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_question_accepts_skip_retry_and_not_sure(self) -> None:
        for line, reply in (("s", Reply.SKIP), ("r", Reply.RETRY), ("?", Reply.NOT_SURE)):
            with self.subTest(line=line):
                prompter, _ = terminal(line, line, line)
                self.assertEqual(await prompter.ask_text("Code?"), Answer(reply))
                self.assertEqual(await prompter.ask_choice("Pick", ["a", "b"]), Answer(reply))
                self.assertEqual(await prompter.confirm("Heard it?"), Answer(reply))

    async def test_text(self) -> None:
        prompter, _ = terminal("", "482-913")
        self.assertEqual(await prompter.ask_text("Code?"), Answer(Reply.ANSWERED, "482-913"))

    async def test_choice_is_numbered_from_one_and_asks_again_when_invalid(self) -> None:
        prompter, output = terminal("0", "3", "x", "2")
        answer = await prompter.ask_choice("Pick", ["a", "b"])
        self.assertEqual(answer, Answer(Reply.ANSWERED, 1))
        self.assertIn("  1) a\n  2) b\n", output.getvalue())
        self.assertEqual(output.getvalue().count("Please answer"), 3)

    async def test_confirm(self) -> None:
        prompter, _ = terminal("maybe", "Y", "no")
        self.assertEqual(await prompter.confirm("Heard it?"), Answer(Reply.ANSWERED, True))
        self.assertEqual(await prompter.confirm("Heard it?"), Answer(Reply.ANSWERED, False))

    async def test_end_of_input_raises(self) -> None:
        def closed(_prompt: str) -> str:
            raise EOFError

        with self.assertRaises(EOFError):
            await TerminalPrompter(read_line=closed, output=io.StringIO()).ask_text("Code?")

    def test_report_check_shows_reference_and_frames_unless_passed(self) -> None:
        prompter, output = terminal()
        prompter.report_check(Finding("CORE-X-001", Verdict.PASS, "m.md:1", "fine", frames=(1,)))
        prompter.report_check(Finding("CORE-X-002", Verdict.FAIL, "m.md:2", "bad", frames=(4, 5)))
        self.assertEqual(
            output.getvalue(),
            "  ✓ CORE-X-001  fine\n  ✗ CORE-X-002 FAIL  bad (m.md:2; frames 4, 5)\n",
        )

    def test_output_escapes_terminal_controls(self) -> None:
        prompter, output = terminal()
        prompter.instruct("Found: \x1b]0;evil\x07 \x9b2J")
        self.assertNotIn("\x1b", output.getvalue())
        self.assertNotIn("\x9b", output.getvalue())
        self.assertEqual(printable("Kitchen\nSpeaker"), "Kitchen\nSpeaker")


class AutoAnswerPrompterTests(unittest.IsolatedAsyncioTestCase):
    async def test_gives_the_expected_answer_or_the_default(self) -> None:
        prompter = AutoAnswerPrompter(output=io.StringIO())
        self.assertEqual(
            await prompter.ask_text("Code?", expected="5"), Answer(Reply.ANSWERED, "5")
        )
        self.assertEqual(await prompter.ask_text("Code?"), Answer(Reply.ANSWERED, ""))
        self.assertEqual(
            await prompter.ask_choice("Pick", ["a", "b"], expected=1), Answer(Reply.ANSWERED, 1)
        )
        self.assertEqual(await prompter.ask_choice("Pick", ["a", "b"]), Answer(Reply.ANSWERED, 0))
        self.assertEqual(
            await prompter.confirm("Heard it?", expected=False), Answer(Reply.ANSWERED, False)
        )
        self.assertEqual(await prompter.confirm("Heard it?"), Answer(Reply.ANSWERED, True))


class HumanVerdictTests(unittest.TestCase):
    def test_skip_and_not_sure_are_skipped_never_pass(self) -> None:
        self.assertIs(human_verdict(Answer(Reply.SKIP), "5"), Verdict.SKIPPED)
        self.assertIs(human_verdict(Answer(Reply.NOT_SURE), "5"), Verdict.SKIPPED)

    def test_answers_pass_only_when_right(self) -> None:
        self.assertIs(human_verdict(Answer(Reply.ANSWERED, "5"), "5"), Verdict.PASS)
        self.assertIs(human_verdict(Answer(Reply.ANSWERED, "4"), "5"), Verdict.FAIL)

    def test_retry_has_no_verdict(self) -> None:
        with self.assertRaises(ValueError):
            human_verdict(Answer(Reply.RETRY), "5")


if __name__ == "__main__":
    unittest.main()
