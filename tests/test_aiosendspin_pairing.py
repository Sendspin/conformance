"""Regression coverage for admission-time conformance trust."""

import unittest

from conformance.adapters._aiosendspin_pairing import (
    deterministic_identity,
    deterministic_psk,
    make_server_identity_and_store,
)


class ConformanceTrustTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_wire_identities_are_approved_before_activation(self):
        _, store = await make_server_identity_and_store(server_id="server", client_id="label")
        for client_id in ("fresh-device-one", "fresh-device-two"):
            approval = await store.trusted_unpaired(client_id)
            self.assertEqual(approval.client_id, client_id)
            self.assertEqual(await store.trusted_unpaired(client_id), approval)
        self.assertEqual(len(await store.list_trusted_unpaired()), 2)

    async def test_prepaired_client_record_is_preserved(self):
        from aiosendspin.noise.keys import psk_id_for

        _, store = await make_server_identity_and_store(server_id="server", client_id="label")
        psk = deterministic_psk("server", "label")
        record = await store.record_by_client_id(deterministic_identity("client:label").peer_id)
        self.assertEqual(record.psk_id, psk_id_for(psk))
        self.assertEqual(record.psk, psk)
        self.assertEqual(record.client_id, deterministic_identity("client:label").peer_id)
