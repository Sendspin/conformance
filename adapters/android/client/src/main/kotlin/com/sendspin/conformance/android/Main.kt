package com.sendspin.conformance.android

import com.sendspindroid.sendspin.crypto.ClientIdentity
import com.sendspindroid.sendspin.crypto.PairingConfig
import com.sendspindroid.sendspin.crypto.Psk
import com.sendspindroid.sendspin.crypto.PskCandidateSet
import com.sendspindroid.sendspin.crypto.PskCandidates
import com.sendspindroid.sendspin.crypto.PskCategory
import com.sendspindroid.sendspin.crypto.secureRandomBytes
import com.sendspindroid.sendspin.protocol.ActivationOutcome
import com.sendspindroid.sendspin.protocol.NoiseWireCodec
import com.sendspindroid.sendspin.protocol.SendSpinProtocol
import com.sendspindroid.sendspin.protocol.ServerActivateRules
import com.sendspindroid.sendspin.protocol.StreamConfig
import com.sendspindroid.sendspin.protocol.message.BinaryMessageParser
import com.sendspindroid.sendspin.protocol.message.MessageBuilder
import com.sendspindroid.sendspin.protocol.message.MessageParser
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import okhttp3.OkHttpClient
import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.security.MessageDigest
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import kotlin.system.exitProcess

/**
 * Conformance client adapter for SendspinDroid.
 *
 * Implements the client-initiated player scenarios of the adapter contract
 * (adapters/README.md) over the encrypted wire: [EncryptedSocket] runs the app's
 * handshake driver and wire codec, and everything above it is the app's shared
 * protocol layer - ServerActivateRules, MessageBuilder, MessageParser and
 * BinaryMessageParser. Other scenarios fail fast with an explanatory summary.
 *
 * The client connects unpaired, on the Sentinel PSK with unpaired access
 * enabled, which the harness servers approve for every connecting client.
 */

private const val IMPLEMENTATION = "android"

private const val SCENARIO_PCM = "client-initiated-pcm"
private const val SCENARIO_STATE_FORMAT_PCM = "client-initiated-state-format-pcm"
private const val SCENARIO_STATE_FORMAT_FLAC = "client-initiated-state-format-flac"

// What the app's :player module reports on a device at API 31 or later:
// AudioDecoderFactory decodes FLAC and Opus through MediaCodec, and AudioTrack
// accepts 16, 24 and 32-bit PCM.
private val SUPPORTED_CODECS = setOf("pcm", "flac", "opus")
private val SUPPORTED_PCM_BIT_DEPTHS = listOf(16, 24, 32)

private class Args(argv: Array<String>) {
    private val map = buildMap {
        var i = 0
        while (i < argv.size - 1) {
            val key = argv[i]
            if (key.startsWith("--")) {
                put(key.removePrefix("--"), argv[i + 1])
                i += 2
            } else {
                i += 1
            }
        }
    }

    operator fun get(key: String): String? = map[key]
    fun required(key: String): String = map[key] ?: error("Missing required arg --$key")
}

/** Canonical float32 PCM hasher matching conformance/pcm.py. */
private class FloatPcmHasher {
    private val digest = MessageDigest.getInstance("SHA-256")
    var sampleCount = 0L
        private set

    fun update(pcmBytes: ByteArray, bitDepth: Int) {
        val floats: FloatArray = when (bitDepth) {
            16 -> {
                val buf = ByteBuffer.wrap(pcmBytes).order(ByteOrder.LITTLE_ENDIAN)
                FloatArray(pcmBytes.size / 2) { buf.short / 32768.0f }
            }
            24 -> FloatArray(pcmBytes.size / 3) { i ->
                val o = i * 3
                var v = (pcmBytes[o].toInt() and 0xFF) or
                    ((pcmBytes[o + 1].toInt() and 0xFF) shl 8) or
                    ((pcmBytes[o + 2].toInt() and 0xFF) shl 16)
                if (v and 0x800000 != 0) v = v or -0x1000000
                v / 8388608.0f
            }
            32 -> {
                val buf = ByteBuffer.wrap(pcmBytes).order(ByteOrder.LITTLE_ENDIAN)
                FloatArray(pcmBytes.size / 4) { buf.int / 2147483648.0f }
            }
            else -> error("Unsupported PCM bit depth: $bitDepth")
        }
        val out = ByteBuffer.allocate(floats.size * 4).order(ByteOrder.LITTLE_ENDIAN)
        floats.forEach { out.putFloat(it) }
        digest.update(out.array())
        sampleCount += floats.size
    }

    fun hexdigest(): String = digest.digest().toHex()
}

private fun ByteArray.toHex(): String = joinToString("") { "%02x".format(it) }

private fun writeJson(path: String, obj: JsonObject) {
    File(path).writeText(obj.toString())
}

private fun MessageBuilder.FormatEntry.toJson() = buildJsonObject {
    put("codec", codec)
    put("sample_rate", sampleRate)
    put("channels", channels)
    put("bit_depth", bitDepth)
}

private fun MessageBuilder.FormatEntry.label() = "$codec ${sampleRate}Hz ${channels}ch ${bitDepth}-bit"

fun main(argv: Array<String>) {
    val args = Args(argv)
    val summaryPath = args.required("summary")
    val readyPath = args.required("ready")
    val registryPath = args.required("registry")
    val scenarioId = args["scenario-id"] ?: SCENARIO_PCM
    val initiatorRole = args["initiator-role"] ?: "client"
    val preferredCodec = args["preferred-codec"] ?: "pcm"
    val clientName = args["client-name"] ?: "android-client"
    val serverName = args["server-name"] ?: "Sendspin Conformance Server"
    val timeoutSeconds = (args["timeout-seconds"] ?: "40").toDouble()

    // Ready first: the harness waits for this file before proceeding.
    writeJson(readyPath, buildJsonObject {
        put("status", "ready")
        put("scenario_id", scenarioId)
        put("initiator_role", initiatorRole)
    })

    fun exitWithError(reason: String): Nothing {
        writeJson(summaryPath, buildJsonObject {
            put("status", "error")
            put("implementation", IMPLEMENTATION)
            put("role", "client")
            put("scenario_id", scenarioId)
            put("reason", reason)
        })
        exitProcess(1)
    }

    if (initiatorRole != "client") {
        exitWithError(
            "android adapter supports only client-initiated scenarios " +
                "(got scenario_id=$scenarioId, initiator_role=$initiatorRole)"
        )
    }

    // The scenario's preferred codec is the app's preferred-codec setting.
    val formats = MessageBuilder.buildSupportedFormats(
        preferredCodec = preferredCodec,
        isCodecSupported = { it in SUPPORTED_CODECS },
        supportedBitDepths = SUPPORTED_PCM_BIT_DEPTHS,
    )

    // For the format-preference scenarios, the advertised entry the client then
    // prefers in client/state.
    val requestedFormat: MessageBuilder.FormatEntry? = when (scenarioId) {
        SCENARIO_PCM -> null
        SCENARIO_STATE_FORMAT_PCM -> formats.firstOrNull { it.codec == "pcm" && it.bitDepth == 16 }
            ?: exitWithError("SendspinDroid's client/hello offers no 16-bit PCM entry to prefer")
        SCENARIO_STATE_FORMAT_FLAC -> formats.firstOrNull { it.codec == "flac" }
            ?: exitWithError("SendspinDroid's client/hello offers no FLAC entry to prefer")
        else -> exitWithError("android adapter does not support scenario $scenarioId")
    }
    // Set when the hello already leads with the preferred entry. The case still
    // runs, so the server records its side of it, and then fails with this.
    val inexpressible: String? = if (requestedFormat != null && requestedFormat == formats.first()) {
        "SendspinDroid cannot advertise the format order $scenarioId starts from: its " +
            "client/hello lists ${requestedFormat.label()} first, so preferring it in client/state " +
            "cannot change the stream. MessageBuilder.buildSupportedFormats puts the preferred " +
            "codec ahead of PCM and offers no other codec."
    } else {
        null
    }

    // Discover the server URL via the harness registry handoff.
    val deadline = System.nanoTime() + (timeoutSeconds * 1e9).toLong()
    var serverUrl: String? = null
    while (System.nanoTime() < deadline) {
        val registry = File(registryPath)
        if (registry.exists()) {
            runCatching {
                val payload = Json.parseToJsonElement(registry.readText()).jsonObject
                serverUrl = payload[serverName]?.jsonObject?.get("url")
                    ?.jsonPrimitive?.contentOrNull
            }
            if (serverUrl != null) break
        }
        Thread.sleep(100)
    }
    val url = serverUrl ?: exitWithError("Timed out waiting for '$serverName' in registry $registryPath")

    // Session state collected by the socket callbacks.
    val done = CountDownLatch(1)
    val pcmHasher = FloatPcmHasher()
    val encodedDigest = MessageDigest.getInstance("SHA-256")
    var chunkCount = 0
    var streamConfig: StreamConfig? = null
    // The player object of the first and latest stream/start, as received.
    var initialStream: JsonObject? = null
    var latestStream: JsonObject? = null
    var streamStartCount = 0
    var serverHelloPayload: JsonObject? = null
    var failureReason: String? = null
    var activeRoles: List<String> = emptyList()
    var activationSeen = false
    var timeRequests = 0
    // Sent in every client/state once set, as the app's protocol handler does.
    var preferredFormat: MessageBuilder.FormatEntry? = null

    fun fail(reason: String) {
        if (failureReason == null) failureReason = reason
        done.countDown()
    }

    val identity = ClientIdentity.generate()
    val pairingConfig = PairingConfig(
        secureRandomBytes(Psk.PSK_SIZE), unpairedAccessEnabled = true, dynamicPairingCodeEnabled = false,
    )
    val client = OkHttpClient.Builder()
        .pingInterval(5, TimeUnit.SECONDS)
        .build()

    lateinit var socket: EncryptedSocket

    // "When `format` changes while a `player` stream is active, the server
    // re-derives the stream format and sends a `stream/start` if it changed."
    fun sendState() {
        socket.send(
            MessageBuilder.buildPlayerState(
                volume = 100, muted = false, available = true,
                playerRoleActive = SendSpinProtocol.Roles.PLAYER in activeRoles,
                format = preferredFormat,
                artworkRoleActive = SendSpinProtocol.Roles.ARTWORK in activeRoles,
            )
        )
    }

    fun sendTime() {
        timeRequests += 1
        socket.send(MessageBuilder.buildClientTime(System.nanoTime() / 1000))
    }

    socket = EncryptedSocket(
        client = client,
        url = url,
        identity = identity,
        candidates = PskCandidateSet(PskCandidates.build(emptyList(), pairingConfig)),
        onFrame = fun(decoded: NoiseWireCodec.Decoded) {
            when (decoded) {
                is NoiseWireCodec.Decoded.Json -> {
                    val json = runCatching { Json.parseToJsonElement(decoded.text).jsonObject }
                        .getOrNull() ?: return
                    val payload = json["payload"] as? JsonObject
                    when (json["type"]?.jsonPrimitive?.contentOrNull) {
                        SendSpinProtocol.MessageType.SERVER_HELLO -> {
                            serverHelloPayload = payload
                            // "Sent by the client once it has received server/hello."
                            socket.send(
                                MessageBuilder.buildClientHello(
                                    deviceName = clientName,
                                    bufferCapacity = MessageBuilder.calculateBufferCapacity(
                                        formats, SendSpinProtocol.Buffer.DURATION_NORMAL_SEC,
                                    ),
                                    manufacturer = "SendSpinDroid",
                                    supportedFormats = formats,
                                    softwareVersion = "conformance",
                                    unpairedAccessEnabled = true,
                                )
                            )
                        }
                        SendSpinProtocol.MessageType.SERVER_ACTIVATE -> {
                            val activate = ServerActivateRules.parse(payload)
                                ?: return fail("malformed server/activate")
                            // Same rules the app applies.
                            val outcome = ServerActivateRules.evaluate(
                                activate = activate,
                                category = PskCategory.SENTINEL,
                                unpairedAccessEnabled = true,
                                previousRoles = activeRoles,
                                isFirstActivation = !activationSeen,
                                offeredPairMethods = setOf(
                                    MessageBuilder.PairMethodDescriptor.PAIRING_PSK.wireName
                                ),
                            )
                            if (outcome !is ActivationOutcome.Accept) {
                                return fail("server/activate not accepted: $outcome")
                            }
                            activationSeen = true
                            activeRoles = outcome.activeRoles
                            // Only now may the client speak, and every role
                            // this activation made active is owed its state.
                            sendState()
                            if (timeRequests == 0) sendTime()
                        }
                        SendSpinProtocol.MessageType.SERVER_TIME -> {
                            if (timeRequests < 5) sendTime()
                        }
                        SendSpinProtocol.MessageType.STREAM_START -> {
                            val config = MessageParser.parseStreamStart(payload) ?: return
                            streamConfig = config
                            latestStream = payload?.get("player") as? JsonObject
                            streamStartCount += 1
                            if (streamStartCount == 1) {
                                initialStream = latestStream
                                if (requestedFormat != null) {
                                    preferredFormat = requestedFormat
                                    sendState()
                                }
                            }
                        }
                        SendSpinProtocol.MessageType.NOISE_HANDSHAKE ->
                            fail("a re-handshake is outside the harness scenarios")
                        else -> { /* metadata/group/stream-end and others: not verified here */ }
                    }
                }
                is NoiseWireCodec.Decoded.Typed -> {
                    val message = BinaryMessageParser.parse(decoded.type, decoded.body)
                    if (message is BinaryMessageParser.BinaryMessage.Audio) {
                        val config = streamConfig
                            ?: return fail("Received audio chunk before stream/start")
                        chunkCount += 1
                        encodedDigest.update(message.payload)
                        if (config.codec == "pcm") pcmHasher.update(message.payload, config.bitDepth)
                    }
                }
                NoiseWireCodec.Decoded.Buffered -> Unit
                is NoiseWireCodec.Decoded.ProtocolError -> fail("decode failed: ${decoded.reason}")
            }
        },
        onFail = { reason -> fail(reason) },
        onClosed = { _, _ -> done.countDown() },
    )

    val finished = done.await((timeoutSeconds * 1000).toLong(), TimeUnit.MILLISECONDS)
    socket.close()
    client.dispatcher.executorService.shutdown()

    if (!finished) exitWithError("Timed out waiting for server disconnect")
    failureReason?.let { exitWithError(it) }
    inexpressible?.let { exitWithError(it) }

    val summary = buildJsonObject {
        put("status", "ok")
        put("implementation", IMPLEMENTATION)
        put("role", "client")
        put("client_name", clientName)
        put("client_id", identity.clientId)
        put("scenario_id", scenarioId)
        put("initiator_role", initiatorRole)
        put("preferred_codec", preferredCodec)
        put("peer_hello", buildJsonObject {
            put("type", "server/hello")
            serverHelloPayload?.let { put("payload", it) }
        })
        latestStream?.let { put("stream", it) }
        if (requestedFormat != null) {
            put("renegotiation", buildJsonObject {
                put("stream_start_count", streamStartCount)
                put("requested", requestedFormat.toJson())
                initialStream?.let { put("initial_format", it) }
                if (streamStartCount > 1) latestStream?.let { put("final_format", it) }
            })
        } else {
            put("audio", buildJsonObject {
                put("audio_chunk_count", chunkCount)
                put("received_encoded_sha256", encodedDigest.digest().toHex())
                // FLAC and Opus are decoded only by Android MediaCodec, so a
                // compressed stream has no PCM evidence to report.
                if (streamConfig?.codec == "pcm") {
                    put("received_pcm_sha256", pcmHasher.hexdigest())
                    put("received_sample_count", pcmHasher.sampleCount)
                }
            })
        }
    }
    writeJson(summaryPath, summary)
    print(File(summaryPath).readText())
    exitProcess(0)
}
