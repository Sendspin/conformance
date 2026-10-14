"""Every reason the bundled aiosendspin can reject a client for has a spec reference."""

import ast
import unittest
from pathlib import Path

from conformance.validator.noncompliance_refs import UnmappedReasonError, spec_ref_for

VENDORED = Path(__file__).resolve().parents[1] / "src" / "conformance" / "_vendor" / "aiosendspin"
_REPORTERS = {"flag_noncompliance", "_flag_noncompliance", "on_noncompliance"}


def literal_reasons() -> dict[str, str]:
    """Return every reason the bundled source passes as a plain string, with its location."""
    reasons = {}
    for path in sorted(VENDORED.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            name = getattr(node.func, "attr", getattr(node.func, "id", None))
            argument = node.args[0]
            if name in _REPORTERS and isinstance(argument, ast.Constant):
                reasons[argument.value] = f"{path.relative_to(VENDORED)}:{node.lineno}"
    return reasons


class NoncomplianceRefsTests(unittest.TestCase):
    def test_every_literal_reason_is_mapped(self) -> None:
        reasons = literal_reasons()
        self.assertGreater(len(reasons), 50)
        for reason, location in reasons.items():
            with self.subTest(reason=reason, location=location):
                spec_ref_for(reason)

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
