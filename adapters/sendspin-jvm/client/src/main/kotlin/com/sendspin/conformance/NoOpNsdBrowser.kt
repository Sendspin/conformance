package com.sendspin.conformance

import com.sendspin.protocol.NsdBrowser
import com.sendspin.protocol.NsdServiceEvent
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.emptyFlow

/** No-op mDNS browser for the conformance CLI. Discovery is not used — the harness provides URLs. */
class NoOpNsdBrowser : NsdBrowser {
    override fun browse(serviceType: String): Flow<NsdServiceEvent> = emptyFlow()
}
