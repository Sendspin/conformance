package com.sendspin.conformance.android

import com.sendspindroid.sendspin.crypto.ClientIdentity
import com.sendspindroid.sendspin.crypto.PskCandidateSet
import com.sendspindroid.sendspin.protocol.NoiseWireCodec
import com.sendspindroid.sendspin.protocol.SendSpinHandshakeDriver
import kotlinx.coroutines.runBlocking
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import java.io.EOFException

/**
 * A WebSocket taken through the Noise handshake by SendspinDroid's own
 * `SendSpinHandshakeDriver`, then read and written through its `NoiseWireCodec`.
 *
 * [onFrame] receives every frame after decryption. Callbacks run on OkHttp's
 * reader thread, one at a time.
 */
internal class EncryptedSocket(
    client: OkHttpClient,
    url: String,
    identity: ClientIdentity,
    candidates: PskCandidateSet,
    private val onFrame: (NoiseWireCodec.Decoded) -> Unit,
    private val onFail: (String) -> Unit,
    private val onClosed: (code: Int, reason: String) -> Unit,
) {
    @Volatile
    private var codec: NoiseWireCodec? = null

    @Volatile
    private var socket: WebSocket? = null

    private val driver = SendSpinHandshakeDriver(
        identity = identity,
        candidates = candidates,
        onEvent = { event ->
            when (event) {
                is SendSpinHandshakeDriver.Event.SendCleartext -> socket?.send(event.text)
                // Nothing is sent yet: client/hello answers server/hello.
                is SendSpinHandshakeDriver.Event.TransportReady -> codec = NoiseWireCodec(event.transport)
                is SendSpinHandshakeDriver.Event.Fail -> onFail("${event.reason}: ${event.detail}")
            }
        },
    )

    init {
        client.newWebSocket(Request.Builder().url(url).build(), object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                socket = webSocket
                driver.start()
            }

            override fun onMessage(webSocket: WebSocket, text: String) =
                driver.onCleartextFrame(text.toByteArray(Charsets.UTF_8))

            override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
                val c = codec ?: return onFail("binary frame before transport mode")
                onFrame(c.decode(bytes.toByteArray()))
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                webSocket.close(1000, null)
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) =
                onClosed(code, reason)

            // A server that ends the scenario by dropping the connection without
            // a close frame has still closed it.
            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) =
                if (t is EOFException) onClosed(1006, "connection closed without a close frame")
                else onFail("socket failure: ${t.message}")
        })
    }

    /** Encrypt and send a JSON message. @return false before the handshake has completed. */
    @Synchronized
    fun send(text: String): Boolean {
        val c = codec ?: return false
        runBlocking { c.encodeJson(text) }.forEach { socket?.send(ByteString.of(*it)) }
        return true
    }

    fun close() {
        socket?.close(1000, "done")
    }
}
