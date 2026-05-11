package com.sendspin.conformance

import com.sendspin.protocol.NsdRegistrar
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.callbackFlow

/**
 * No-op mDNS registrar for the conformance CLI. The harness assigns ports directly.
 *
 * Uses [callbackFlow] so the flow stays open until cancelled, matching the contract of
 * [NsdRegistrar.register] (cancellation = unregister). [flowOf] would complete immediately
 * and terminate callers' collect blocks prematurely.
 */
class NoOpNsdRegistrar : NsdRegistrar {
    override fun register(
        serviceName: String,
        serviceType: String,
        port: Int,
        attributes: Map<String, String>,
    ): Flow<Unit> = callbackFlow {
        trySend(Unit)
        awaitClose { }
    }
}
