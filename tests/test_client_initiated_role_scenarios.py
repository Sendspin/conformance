"""Coverage for the client-initiated metadata, artwork and controller scenarios.

Each one repeats a server-initiated scenario with the connection opened from the
other side, so the two must ask the same thing of a pair and be offered to the
same implementations. A sibling that drifted would make the two rows of the
report compare different behaviour under matching names.
"""

from __future__ import annotations

import unittest

from conformance.implementations import IMPLEMENTATIONS, implementations_for_scenario
from conformance.scenarios import require_scenario

ROLE_FAMILIES = ("metadata", "artwork", "controller")


class ClientInitiatedRoleScenarioTests(unittest.TestCase):
    def _pairs(self):
        for family in ROLE_FAMILIES:
            yield (
                require_scenario(f"client-initiated-{family}"),
                require_scenario(f"server-initiated-{family}"),
            )

    def test_differs_from_its_sibling_only_in_who_connects(self) -> None:
        for scenario, sibling in self._pairs():
            with self.subTest(scenario=scenario.id):
                self.assertEqual(scenario.initiator_role, "client")
                self.assertEqual(sibling.initiator_role, "server")
                self.assertEqual(scenario.verification_mode, sibling.verification_mode)
                self.assertEqual(scenario.required_role_families, sibling.required_role_families)
                self.assertEqual(scenario.preferred_codec, sibling.preferred_codec)
                self.assertEqual(scenario.extra_cli_args, sibling.extra_cli_args)

    def test_runs_against_every_server_its_sibling_does(self) -> None:
        for scenario, sibling in self._pairs():
            with self.subTest(scenario=scenario.id):
                self.assertEqual(
                    implementations_for_scenario(role="server", scenario=scenario),
                    implementations_for_scenario(role="server", scenario=sibling),
                )

    def test_no_client_is_judged_unsupported_unless_its_sibling_is(self) -> None:
        for scenario, sibling in self._pairs():
            for name, implementation in IMPLEMENTATIONS.items():
                with self.subTest(scenario=scenario.id, client=name):
                    unsupported = implementation.client.unsupported_reason(
                        implementation=name, role="client", scenario=scenario
                    )
                    sibling_unsupported = implementation.client.unsupported_reason(
                        implementation=name, role="client", scenario=sibling
                    )
                    self.assertEqual(unsupported is None, sibling_unsupported is None)


if __name__ == "__main__":
    unittest.main()
