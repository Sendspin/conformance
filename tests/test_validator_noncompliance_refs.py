"""Every reason the bundled aiosendspin can reject a client for has a spec reference."""

import ast
import re
import unittest
from pathlib import Path

from conformance.validator.noncompliance_refs import _REFS, UnmappedReasonError, spec_ref_for

VENDORED = Path(__file__).resolve().parents[1] / "src" / "conformance" / "_vendor" / "aiosendspin"
_REPORTERS = {"flag_noncompliance", "_flag_noncompliance", "on_noncompliance"}
COMPOSED_REASON_SITES = 36


def reasons() -> tuple[dict[str, str], list[str]]:
    """
    Return the bundled source's reasons: plain strings with their locations, and the
    locations of every reason composed at runtime.
    """
    literal, composed = {}, []
    for path in sorted(VENDORED.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            if getattr(node.func, "attr", getattr(node.func, "id", None)) not in _REPORTERS:
                continue
            location = f"{path.relative_to(VENDORED)}:{node.lineno}"
            argument = node.args[0]
            if isinstance(argument, ast.Constant):
                literal[argument.value] = location
            else:
                composed.append(location)
    return literal, composed


class NoncomplianceRefsTests(unittest.TestCase):
    def test_every_literal_reason_matches_exactly_one_pattern(self) -> None:
        literal, _ = reasons()
        self.assertGreater(len(literal), 50)
        for reason, location in literal.items():
            with self.subTest(reason=reason, location=location):
                spec_ref_for(reason)
                matches = [p for p, _ in _REFS if re.fullmatch(p, reason)]
                self.assertEqual(len(matches), 1, matches)

    def test_composed_reason_sites_are_the_ones_mapped(self) -> None:
        # The table maps what these call sites compose. A changed count means the bundled
        # copy gained or lost one: map its reasons in noncompliance_refs, then update this.
        _, composed = reasons()
        self.assertEqual(len(composed), COMPOSED_REASON_SITES, composed)

    def test_composed_reasons_are_mapped(self) -> None:
        self.assertEqual(
            spec_ref_for("initial client/state has an active player role but no player state"),
            "messaging.md:297",
        )
        self.assertEqual(
            spec_ref_for("client/hello sent support objects for unlisted roles: player@v1"),
            "messaging.md:216-218",
        )
        self.assertEqual(
            spec_ref_for(
                "did not send the player client/state object after server/activate in time"
            ),
            "messaging.md:297",
        )

    def test_rules_rc1_does_not_make_have_no_reference(self) -> None:
        self.assertIsNone(spec_ref_for("sent an unsolicited management/result"))

    def test_unknown_reason_raises(self) -> None:
        with self.assertRaises(UnmappedReasonError):
            spec_ref_for("a reason the bundled copy never had")


if __name__ == "__main__":
    unittest.main()
