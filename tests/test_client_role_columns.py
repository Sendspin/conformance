"""Coverage for which implementations the matrix offers as a client.

An implementation with no client role at all has nothing to show in a client
column, so neither the runner nor the report gives it one. A client that does
implement the role keeps its column, including the scenarios it is judged
unsupported for.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from conformance import runner
from conformance.implementations import IMPLEMENTATIONS, implementations_with_role
from conformance.models import ImplementationSpec, RoleSpec
from conformance.scenarios import ordered_scenarios
from conformance.site import _matrix_axes

SERVER_ONLY = "test-server-only"

SERVER_ONLY_SPEC = ImplementationSpec(
    name=SERVER_ONLY,
    display_name=SERVER_ONLY,
    repo_dirname=SERVER_ONLY,
    remote_url="https://example.invalid/test-server-only.git",
    client=RoleSpec(
        supported=False,
        adapter_kind="placeholder",
        entrypoint="conformance.adapters.placeholder",
        reason="test-server-only is a server-only implementation.",
    ),
    server=RoleSpec(
        supported=True,
        adapter_kind="python",
        entrypoint="conformance.adapters.placeholder",
        supports_server_initiated=True,
        supports_client_initiated=True,
        supports_flac=True,
        supports_opus=True,
        supported_role_families=("player", "metadata", "controller", "artwork"),
    ),
)


def _row(scenario_id: str, server_impl: str, client_impl: str) -> dict[str, Any]:
    return {
        "scenario_id": scenario_id,
        "server_impl": server_impl,
        "client_impl": client_impl,
        "status": "passed",
    }


class ClientRoleColumnsTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(IMPLEMENTATIONS, {SERVER_ONLY: SERVER_ONLY_SPEC})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run_matrix_cases(self) -> list[tuple[str, str, str]]:
        cases: list[tuple[str, str, str]] = []

        async def fake_run_case(**kwargs: Any) -> SimpleNamespace:
            cases.append((kwargs["scenario_id"], kwargs["server_impl"], kwargs["client_impl"]))
            return SimpleNamespace()

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(runner, "run_case", fake_run_case),
        ):
            asyncio.run(
                runner.run_matrix(
                    results_dir=Path(tmp),
                    from_filter=None,
                    to_filter=None,
                    timeout_s=1.0,
                )
            )
        return cases

    def test_run_matrix_creates_no_case_with_it_as_the_client(self) -> None:
        cases = self._run_matrix_cases()

        self.assertNotIn(SERVER_ONLY, {client for _, _, client in cases})
        self.assertIn(SERVER_ONLY, {server for _, server, _ in cases})

    def test_run_matrix_keeps_every_client_that_implements_the_role(self) -> None:
        cases = self._run_matrix_cases()
        expected = {
            (scenario.id, server, client)
            for scenario in ordered_scenarios()
            for server in runner.implementations_for_scenario(role="server", scenario=scenario)
            for client in IMPLEMENTATIONS
            if IMPLEMENTATIONS[client].client.supported
        }

        self.assertEqual(set(cases), expected)

    def test_the_report_draws_no_column_for_it(self) -> None:
        scenario = ordered_scenarios()[0]
        results = [_row(scenario.id, SERVER_ONLY, client) for client in implementations_with_role("client")]

        server_impls, client_impls = _matrix_axes(results)

        self.assertIn(SERVER_ONLY, server_impls)
        self.assertNotIn(SERVER_ONLY, client_impls)
        self.assertEqual(client_impls, implementations_with_role("client"))
        self.assertNotIn(SERVER_ONLY, _matrix_axes([])[1])

    def test_a_client_unsupported_for_one_scenario_keeps_its_column(self) -> None:
        judged_unsupported = [
            (scenario.id, name)
            for scenario in ordered_scenarios()
            for name, spec in IMPLEMENTATIONS.items()
            if spec.client.supported
            and spec.client.unsupported_reason(implementation=name, role="client", scenario=scenario)
            is not None
        ]
        self.assertTrue(judged_unsupported, "expected some client to be unsupported somewhere")
        cases = self._run_matrix_cases()

        for scenario_id, client in judged_unsupported:
            with self.subTest(scenario=scenario_id, client=client):
                _, client_impls = _matrix_axes([_row(scenario_id, SERVER_ONLY, client)])
                self.assertIn(client, client_impls)
                self.assertIn(client, {c for s, _, c in cases if s == scenario_id})


if __name__ == "__main__":
    unittest.main()
