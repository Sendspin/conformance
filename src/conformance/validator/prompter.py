"""
How the validator talks to the person running it.

Steps reach the user only through `Prompter`, so a different front end replaces the
terminal without changing a step. Every question accepts `s` (skip), `r` (retry the
step) and `?` (not sure) besides an answer.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, TextIO

from .findings import Finding, Verdict

_CONTROLS_HINT = "s=skip, r=retry, ?=not sure"
_SYMBOLS = {
    Verdict.PASS: "✓",
    Verdict.FAIL: "✗",
    Verdict.WARN: "!",
    Verdict.SKIPPED: "–",
    Verdict.NOT_APPLICABLE: "–",
    Verdict.TOOL_ERROR: "!",
}


class Reply(StrEnum):
    """How the user responded to a question."""

    ANSWERED = "answered"
    SKIP = "skip"
    RETRY = "retry"
    """Run the step again; the caller decides what that repeats."""
    NOT_SURE = "not_sure"


@dataclass(frozen=True, slots=True)
class Answer[T]:
    """The user's response to one question; `value` is set only when they answered."""

    reply: Reply
    value: T | None = None


def human_verdict[T](answer: Answer[T], expected: T) -> Verdict:
    """
    Return the verdict a human check reaches from the user's answer.

    Skipping and "not sure" are SKIPPED, never PASS. Raises ValueError for a retry,
    which the caller must handle by repeating the step.
    """
    match answer.reply:
        case Reply.ANSWERED:
            return Verdict.PASS if answer.value == expected else Verdict.FAIL
        case Reply.SKIP | Reply.NOT_SURE:
            return Verdict.SKIPPED
        case Reply.RETRY:
            msg = "a retry has no verdict; repeat the step"
            raise ValueError(msg)


class Prompter(Protocol):
    """
    The validator's only channel to the user.

    `expected` is the answer the validator knows to be right. Only the CI auto-answerer
    reads it; a person is never shown it.
    """

    def instruct(self, text: str) -> None:
        """Tell the user something, such as what to do in their app next."""

    def report_check(self, finding: Finding) -> None:
        """Show a check's verdict as it resolves."""

    async def ask_text(self, question: str, *, expected: str | None = None) -> Answer[str]:
        """Ask for free text."""

    async def ask_choice(
        self, question: str, choices: Sequence[str], *, expected: int | None = None
    ) -> Answer[int]:
        """Ask the user to pick one of `choices`; the answer is its index."""

    async def confirm(self, question: str, *, expected: bool | None = None) -> Answer[bool]:
        """Ask a yes/no question."""


class TerminalPrompter:
    """
    A `Prompter` on a plain terminal, using the standard library only.

    Questions are read on a worker thread, so the event loop keeps serving the client
    while the user thinks. Raises EOFError when input ends.
    """

    def __init__(
        self, *, read_line: Callable[[str], str] = input, output: TextIO = sys.stdout
    ) -> None:
        self._read_line = read_line
        self._output = output

    def instruct(self, text: str) -> None:
        """Print `text`."""
        self._write(text)

    def report_check(self, finding: Finding) -> None:
        """Print one line for `finding`, with its spec reference and frames unless it passed."""
        symbol = _SYMBOLS[finding.verdict]
        if finding.verdict is Verdict.PASS:
            self._write(f"  {symbol} {finding.id}  {finding.message}")
            return
        frames = f"; frames {', '.join(map(str, finding.frames))}" if finding.frames else ""
        self._write(
            f"  {symbol} {finding.id} {finding.verdict}  {finding.message}"
            f" ({finding.spec_ref}{frames})"
        )

    async def ask_text(self, question: str, *, expected: str | None = None) -> Answer[str]:
        """Ask for a non-empty line of text."""
        return await self._ask(f"{question} ({_CONTROLS_HINT}): ", lambda line: line or None)

    async def ask_choice(
        self,
        question: str,
        choices: Sequence[str],
        *,
        expected: int | None = None,
    ) -> Answer[int]:
        """Print `choices` numbered from 1 and ask for one."""
        self._write(question)
        for number, choice in enumerate(choices, start=1):
            self._write(f"  {number}) {choice}")

        def parse(line: str) -> int | None:
            if line.isdecimal() and 1 <= int(line) <= len(choices):
                return int(line) - 1
            return None

        return await self._ask(f"> ({_CONTROLS_HINT}) ", parse)

    async def confirm(self, question: str, *, expected: bool | None = None) -> Answer[bool]:
        """Ask a question answered with y or n."""
        answers = {"y": True, "yes": True, "n": False, "no": False}
        return await self._ask(
            f"{question} [y/n] ({_CONTROLS_HINT}): ", lambda line: answers.get(line.lower())
        )

    async def _ask[T](self, prompt: str, parse: Callable[[str], T | None]) -> Answer[T]:
        controls = {"s": Reply.SKIP, "r": Reply.RETRY, "?": Reply.NOT_SURE}
        while True:
            line = (await asyncio.to_thread(self._read_line, printable(prompt))).strip()
            if (reply := controls.get(line.lower())) is not None:
                return Answer(reply)
            if (value := parse(line)) is not None:
                return Answer(Reply.ANSWERED, value)
            self._write(f"Please answer, or {_CONTROLS_HINT}.")

    def _write(self, text: str) -> None:
        print(printable(text), file=self._output, flush=True)


class AutoAnswerPrompter(TerminalPrompter):
    """
    A `Prompter` for CI that answers every question itself.

    It gives the expected answer when the validator knows one, and otherwise the
    first choice, yes, or empty text. It prints what it answered.
    """

    async def ask_text(self, question: str, *, expected: str | None = None) -> Answer[str]:
        """Answer with `expected`, or empty text."""
        return self._answered(question, expected or "")

    async def ask_choice(
        self, question: str, choices: Sequence[str], *, expected: int | None = None
    ) -> Answer[int]:
        """Answer with `expected`, or the first choice."""
        index = expected if expected is not None else 0
        return self._answered(f"{question} {choices[index]}", index)

    async def confirm(self, question: str, *, expected: bool | None = None) -> Answer[bool]:
        """Answer with `expected`, or yes."""
        return self._answered(question, expected if expected is not None else True)

    def _answered[T](self, question: str, value: T) -> Answer[T]:
        self._write(f"{question} [auto-answered: {value}]")
        return Answer(Reply.ANSWERED, value)


def printable(text: str) -> str:
    """
    Return `text` safe to write to a terminal.

    Client-supplied names reach the output, so control characters, terminal escapes
    included, are shown as escape sequences rather than interpreted.
    """
    return "".join(
        char if char.isprintable() or char == "\n" else char.encode("unicode_escape").decode()
        for char in text
    )
