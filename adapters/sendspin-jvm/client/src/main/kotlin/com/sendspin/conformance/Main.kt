package com.sendspin.conformance

import com.sendspin.protocol.ArtworkChannel
import com.sendspin.protocol.AudioChunk
import com.sendspin.protocol.AudioFormat
import com.sendspin.protocol.ClientPreferences
import com.sendspin.protocol.ClientState
import com.sendspin.protocol.ControllerState
import com.sendspin.protocol.JsonOptional
import com.sendspin.protocol.JsonOptionalAdapterFactory
import com.sendspin.protocol.SendSpinClient
import com.sendspin.protocol.SendSpinServerHost
import com.sendspin.protocol.ServerHello
import com.sendspin.protocol.StreamArtworkConfig
import com.sendspin.protocol.StreamFormat
import com.sendspin.protocol.TrackMetadataMsg
import com.squareup.moshi.Moshi
import com.squareup.moshi.kotlin.reflect.KotlinJsonAdapterFactory
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.collect
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import okhttp3.OkHttpClient
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.security.MessageDigest
import java.util.UUID
import java.util.logging.ConsoleHandler
import java.util.logging.Level
import java.util.logging.Logger
import kotlin.system.exitProcess

// ── Config ────────────────────────────────────────────────────────────────────

data class CliConfig(
    val scenarioId: String,
    val initiatorRole: String,   // "server" or "client"
    val preferredCodec: String,
    val summaryPath: String,
    val readyPath: String,
    val registryPath: String,
    val clientName: String,
    val clientId: String,
    val serverName: String,
    val serverId: String,
    val listenerPort: Int?,      // server-initiated: our listener port
    val wsPath: String,          // WebSocket path, default "/sendspin"
    val controllerCommand: String, // command to send for controller scenarios
    val timeoutMs: Long,
)

// ── Session result ────────────────────────────────────────────────────────────

data class SessionResult(
    val serverHello: ServerHello,
    val streamFormat: StreamFormat?,
    val artworkStreamConfig: StreamArtworkConfig?,
    val encodedBytes: ByteArray,
    val chunkCount: Int,
    val lastMetadata: TrackMetadataMsg?,
    val metadataUpdateCount: Int,
    val controllerState: ControllerState?,
    val sentControllerCommand: String?,
    val artworkBytes: ByteArray?,
    val artworkChannel: Int,
    val artworkReceivedCount: Int,
)

// ── Entry point ───────────────────────────────────────────────────────────────

fun main(args: Array<String>) {
    val julHandler = ConsoleHandler().apply {
        level = Level.FINE
        formatter = java.util.logging.SimpleFormatter()
    }
    Logger.getLogger("sendspin-protocol").apply {
        addHandler(julHandler); level = Level.FINE; useParentHandlers = false
    }

    val config = parseArgs(args)
    val moshi = Moshi.Builder()
        .add(JsonOptionalAdapterFactory())
        .addLast(KotlinJsonAdapterFactory())
        .build()

    var artworkReceivedCount = 0
    var metadataUpdateCount = 0
    var lastMetadata: TrackMetadataMsg? = null
    var lastControllerState: ControllerState? = null
    var lastStreamFormat: StreamFormat? = null
    var sentCommand: String? = null

    // Collect all raw audio chunks via the hook — bypasses AudioBuffer drop logic so early
    // chunks (arriving before clock sync converges) are not lost.
    val receivedChunks = mutableListOf<AudioChunk>()

    val client = SendSpinClient(
        okHttpClient = OkHttpClient.Builder().build(),
        moshi = moshi,
        preferences = buildPreferences(config.preferredCodec),
        clientId = config.clientId,
        clientName = config.clientName,
        manufacturer = "conformance",
        productName = "conformance-client",
        softwareVersion = "1.0",
        audioPlayerFactory = { buffer, _ -> CollectingAudioPlayer(buffer) },
        reconnectEnabled = false,
    ).also { it.onAudioChunk = { chunk -> synchronized(receivedChunks) { receivedChunks.add(chunk) } } }

    val sessionResult = runBlocking {
        // Collectors for data that require active observation — must be started before the session
        // so we capture values that cleanupJobs() will null out after disconnect.
        val metaJob: Job = launch {
            client.serverState.collect { state ->
                state.metadata?.let { lastMetadata = it; metadataUpdateCount++ }
            }
        }
        val artworkJob: Job = launch {
            client.albumArtwork.collect { bytes ->
                if (bytes != null) artworkReceivedCount++
            }
        }
        val controllerJob: Job = launch {
            client.controllerState.collect { cs -> if (cs != null) lastControllerState = cs }
        }
        val streamFormatJob: Job = launch {
            client.streamFormat.collect { fmt -> if (fmt != null) lastStreamFormat = fmt }
        }

        val result = withTimeoutOrNull(config.timeoutMs) {
            when (config.initiatorRole) {
                "server" -> runServerInitiated(client, moshi, config) { sentCommand = it }
                "client" -> runClientInitiated(client, config) { sentCommand = it }
                else -> error("Unknown initiator_role: ${config.initiatorRole}")
            }
        }

        metaJob.cancelAndJoin()
        artworkJob.cancelAndJoin()
        controllerJob.cancelAndJoin()
        streamFormatJob.cancelAndJoin()

        result
    }

    if (sessionResult == null) {
        writeSummaryAndExit(
            config, moshi,
            buildErrorSummary(config, "timeout after ${config.timeoutMs / 1000}s"),
            exitCode = 1,
        )
    }

    val chunks = synchronized(receivedChunks) { receivedChunks.toList() }
    val encodedBytes = ByteArrayOutputStream().also { out -> chunks.forEach { out.write(it.data) } }.toByteArray()
    val session = SessionResult(
        serverHello = sessionResult!!,
        streamFormat = lastStreamFormat,
        artworkStreamConfig = client.streamArtwork.value,
        encodedBytes = encodedBytes,
        chunkCount = chunks.size,
        lastMetadata = lastMetadata,
        metadataUpdateCount = metadataUpdateCount,
        controllerState = lastControllerState,
        sentControllerCommand = sentCommand,
        artworkBytes = client.albumArtwork.value,
        artworkChannel = 0,
        artworkReceivedCount = artworkReceivedCount,
    )

    writeSummaryAndExit(config, moshi, buildSummary(config, session, moshi), exitCode = 0)
}

// ── Session runners ───────────────────────────────────────────────────────────

private suspend fun runServerInitiated(
    client: SendSpinClient,
    moshi: Moshi,
    config: CliConfig,
    onCommandSent: (String) -> Unit,
): ServerHello {
    val port = requireNotNull(config.listenerPort) { "--port is required for server-initiated scenarios" }
    val url = "ws://127.0.0.1:$port${config.wsPath}"

    val host = SendSpinServerHost(client = client, moshi = moshi, getLastPlayedServerId = { "" }, port = port)
    host.startServer()
    host.serverReady.await()

    // Register in registry and signal ready
    writeRegistry(config.registryPath, config.clientName, url)
    writeReadyFile(config.readyPath, config.scenarioId, config.initiatorRole, url)

    val serverHello = client.serverHello.first { it != null }!!

    if (config.scenarioId == "server-initiated-controller") {
        // Wait for a controller state that explicitly advertises the expected command — sending
        // before the server includes the command in supported_commands causes it to reject it.
        while (client.controllerState.value?.supportedCommands?.contains(config.controllerCommand) != true) {
            kotlinx.coroutines.delay(10)
        }
        client.sendControllerCommand(config.controllerCommand)
        onCommandSent(config.controllerCommand)

        // Give the server up to 5 s to close the connection; if it doesn't, close from our side.
        val disconnected = withTimeoutOrNull(5_000) {
            client.state.first { it == ClientState.DISCONNECTED || it == ClientState.ERROR }
        }
        if (disconnected == null) client.disconnect("conformance_complete")
        host.stopServer()
        return serverHello
    }

    client.state.first { it == ClientState.DISCONNECTED || it == ClientState.ERROR }
    host.stopServer()
    return serverHello
}

private suspend fun runClientInitiated(
    client: SendSpinClient,
    config: CliConfig,
    onCommandSent: (String) -> Unit,
): ServerHello {
    val serverUrl = pollRegistry(config.registryPath, config.serverName, timeoutMs = config.timeoutMs)
        ?: error("Server did not register in registry within timeout")

    writeReadyFile(config.readyPath, config.scenarioId, config.initiatorRole)
    client.connect(serverUrl)

    val serverHello = client.serverHello.first { it != null }!!

    if (config.scenarioId == "client-initiated-controller") {
        client.controllerState.first { it != null }
        client.sendControllerCommand(config.controllerCommand)
        onCommandSent(config.controllerCommand)
    }

    client.state.first { it == ClientState.DISCONNECTED || it == ClientState.ERROR }
    return serverHello
}

// ── Summary building ──────────────────────────────────────────────────────────

private fun buildSummary(config: CliConfig, session: SessionResult, moshi: Moshi): JSONObject {
    val helloAdapter = moshi.adapter(ServerHello::class.java)

    return JSONObject().apply {
        put("status", "ok")
        put("implementation", "sendspin-jvm")
        put("role", "client")
        put("client_name", config.clientName)
        put("client_id", config.clientId)
        put("scenario_id", config.scenarioId)
        put("initiator_role", config.initiatorRole)
        put("preferred_codec", config.preferredCodec)
        put("peer_hello", JSONObject(helloAdapter.toJson(session.serverHello)))
        put("server", JSONObject.NULL)

        when {
            config.scenarioId.contains("-metadata") -> addMetadataFields(session, moshi)
            config.scenarioId.contains("-controller") -> addControllerFields(session)
            config.scenarioId.contains("-artwork") -> addArtworkFields(session)
            else -> addAudioFields(session, config.preferredCodec)
        }
    }
}

private fun JSONObject.addAudioFields(session: SessionResult, codec: String) {
    val fmt = session.streamFormat
    if (fmt != null) {
        put("stream", JSONObject().apply {
            put("codec", fmt.codec)
            put("sample_rate", fmt.sampleRate)
            put("channels", fmt.channels)
            put("bit_depth", fmt.bitDepth)
            put("codec_header", fmt.codecHeader ?: JSONObject.NULL)
        })
    }
    val encodedHash = sha256Hex(session.encodedBytes)
    // PCM hash uses canonical float32 normalization (matches conformance harness pcm.py)
    val pcmHash = if (codec == "pcm" && fmt != null) {
        sha256Hex(pcmToFloat32Bytes(session.encodedBytes, fmt.bitDepth))
    } else {
        JSONObject.NULL
    }
    val sampleCount = if (fmt != null) session.encodedBytes.size / (fmt.bitDepth / 8) else 0
    put("audio", JSONObject().apply {
        put("audio_chunk_count", session.chunkCount)
        put("received_encoded_sha256", encodedHash)
        put("received_pcm_sha256", pcmHash)
        put("received_sample_count", sampleCount)
    })
}

private fun JSONObject.addMetadataFields(session: SessionResult, moshi: Moshi) {
    val meta = session.lastMetadata
    put("metadata", JSONObject().apply {
        put("update_count", session.metadataUpdateCount)
        if (meta != null) {
            put("received", JSONObject().apply {
                put("title", meta.title.orNull())
                put("artist", meta.artist.orNull())
                put("album_artist", meta.albumArtist.orNull())
                put("album", meta.album.orNull())
                put("artwork_url", meta.artworkUrl.orNull())
                put("year", meta.year.orNull() ?: JSONObject.NULL)
                put("track", meta.track.orNull() ?: JSONObject.NULL)
                put("repeat", meta.repeat.orNull())
                put("shuffle", meta.shuffle.orNull() ?: JSONObject.NULL)
                val prog = meta.progress
                if (prog != null) {
                    put("progress", JSONObject().apply {
                        put("track_progress", prog.trackProgress)
                        put("track_duration", prog.trackDuration)
                        put("playback_speed", prog.playbackSpeed)
                    })
                } else {
                    put("progress", JSONObject.NULL)
                }
            })
        } else {
            put("received", JSONObject.NULL)
        }
    })
}

private fun JSONObject.addControllerFields(session: SessionResult) {
    put("controller", JSONObject().apply {
        val cs = session.controllerState
        if (cs != null) {
            put("received_state", JSONObject().apply {
                put("supported_commands", JSONArray(cs.supportedCommands))
                put("volume", cs.volume)
                put("muted", cs.muted)
            })
        } else {
            put("received_state", JSONObject.NULL)
        }
        val cmd = session.sentControllerCommand
        if (cmd != null) {
            put("sent_command", JSONObject().put("command", cmd))
        } else {
            put("sent_command", JSONObject.NULL)
        }
    })
}

private fun JSONObject.addArtworkFields(session: SessionResult) {
    val cfg = session.artworkStreamConfig
    if (cfg != null) {
        put("stream", JSONObject().apply {
            put("channels", JSONArray().apply {
                cfg.channels.forEach { ch ->
                    put(JSONObject().apply {
                        put("source", ch.source)
                        put("format", ch.format)
                        put("width", ch.width)
                        put("height", ch.height)
                    })
                }
            })
        })
    }
    val bytes = session.artworkBytes
    put("artwork", JSONObject().apply {
        put("channel", session.artworkChannel)
        put("received_count", session.artworkReceivedCount)
        put("received_sha256", if (bytes != null) sha256Hex(bytes) else JSONObject.NULL)
        put("byte_count", bytes?.size ?: 0)
    })
}

private fun buildErrorSummary(config: CliConfig, reason: String) = JSONObject().apply {
    put("status", "error")
    put("implementation", "sendspin-jvm")
    put("role", "client")
    put("client_name", config.clientName)
    put("client_id", config.clientId)
    put("scenario_id", config.scenarioId)
    put("initiator_role", config.initiatorRole)
    put("preferred_codec", config.preferredCodec)
    put("reason", reason)
}

private fun writeSummaryAndExit(config: CliConfig, moshi: Moshi, summary: JSONObject, exitCode: Int): Nothing {
    val text = summary.toString(2) + "\n"
    File(config.summaryPath).apply { parentFile?.mkdirs(); writeText(text) }
    print(text)
    exitProcess(exitCode)
}

// ── Registry / ready helpers ──────────────────────────────────────────────────

private fun writeRegistry(registryPath: String, name: String, url: String) {
    val file = File(registryPath)
    // Tolerate a partially-written file from a concurrent writer — treat it as empty.
    val payload = if (file.exists()) try { JSONObject(file.readText()) } catch (_: Exception) { JSONObject() } else JSONObject()
    payload.put(name, JSONObject().put("url", url))
    file.apply { parentFile?.mkdirs(); writeText(payload.toString(2) + "\n") }
}

private suspend fun pollRegistry(registryPath: String, serverName: String, timeoutMs: Long): String? {
    val deadline = System.currentTimeMillis() + timeoutMs
    while (System.currentTimeMillis() < deadline) {
        val url = withContext(Dispatchers.IO) {
            val file = File(registryPath)
            if (file.exists()) try {
                JSONObject(file.readText()).optJSONObject(serverName)?.optString("url")
            } catch (_: Exception) { null }  // partial write race — retry on next poll
            else null
        }
        if (!url.isNullOrBlank()) return url
        delay(100)
    }
    return null
}

private fun writeReadyFile(readyPath: String, scenarioId: String, initiatorRole: String, url: String? = null) {
    val json = JSONObject().apply {
        put("status", "ready")
        put("scenario_id", scenarioId)
        put("initiator_role", initiatorRole)
        if (url != null) put("url", url)
    }
    File(readyPath).apply { parentFile?.mkdirs(); writeText(json.toString(2) + "\n") }
}

// ── Utilities ─────────────────────────────────────────────────────────────────

private fun buildPreferences(preferredCodec: String) = ClientPreferences(
    // Request source-compatible formats first so the server sends without resampling.
    // The almost_silent.flac fixture is 8 kHz mono; 256×256 is its embedded artwork.
    supportedFormats = when (preferredCodec) {
        "flac" -> listOf(AudioFormat("flac", 1, 8000, 16), AudioFormat("flac", 2, 48000, 16))
        "opus" -> listOf(AudioFormat("opus", 1, 48000, 16), AudioFormat("opus", 2, 48000, 16))
        else   -> listOf(AudioFormat("pcm", 1, 8000, 16), AudioFormat("pcm", 2, 48000, 16))
    },
    artworkChannels = listOf(ArtworkChannel("album", "jpeg", 256, 256)),
)

/**
 * Converts integer PCM bytes to canonical float32 bytes, matching conformance/pcm.py's
 * `pcm_int_bytes_to_float_bytes`. Required for `received_pcm_sha256` to match the harness.
 */
private fun pcmToFloat32Bytes(pcmBytes: ByteArray, bitDepth: Int): ByteArray {
    val sampleCount = pcmBytes.size / (bitDepth / 8)
    val result = ByteBuffer.allocate(sampleCount * 4).order(ByteOrder.LITTLE_ENDIAN)
    val src = ByteBuffer.wrap(pcmBytes).order(ByteOrder.LITTLE_ENDIAN)
    when (bitDepth) {
        16 -> repeat(sampleCount) { result.putFloat(src.short / 32768.0f) }
        32 -> repeat(sampleCount) { result.putFloat(src.int / 2147483648.0f) }
        else -> throw IllegalArgumentException("Unsupported PCM bit depth: $bitDepth")
    }
    return result.array()
}

private fun sha256Hex(bytes: ByteArray): String {
    if (bytes.isEmpty()) return ""
    return MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }
}

private fun <T> JsonOptional<T>.orNull(): T? = (this as? JsonOptional.Present<T>)?.value

private fun parseArgs(args: Array<String>): CliConfig {
    val map = mutableMapOf<String, String>()
    var i = 0
    while (i < args.size) {
        if (!args[i].startsWith("--")) { i++; continue }
        val key = args[i].removePrefix("--")
        val value = args.getOrNull(i + 1)?.takeIf { !it.startsWith("--") } ?: ""
        map[key] = value
        i += if (value.isEmpty()) 1 else 2
    }
    val scenarioId = map["scenario-id"] ?: error("--scenario-id is required")
    val initiatorRole = map["initiator-role"]
        ?: if (scenarioId.startsWith("server-initiated")) "server" else "client"
    return CliConfig(
        scenarioId    = scenarioId,
        initiatorRole = initiatorRole,
        preferredCodec = map["preferred-codec"] ?: "pcm",
        summaryPath   = map["summary"]  ?: error("--summary is required"),
        readyPath     = map["ready"]    ?: error("--ready is required"),
        registryPath  = map["registry"] ?: error("--registry is required"),
        clientName    = map["client-name"] ?: "sendspin-android-tv",
        clientId      = map["client-id"]   ?: UUID.randomUUID().toString(),
        serverName    = map["server-name"] ?: "",
        serverId      = map["server-id"]   ?: "",
        listenerPort       = map["port"]?.toIntOrNull(),
        wsPath             = map["path"]?.takeIf { it.isNotBlank() } ?: "/sendspin",
        controllerCommand  = map["controller-command"]?.takeIf { it.isNotBlank() } ?: "volume",
        timeoutMs          = map["timeout-seconds"]?.toDoubleOrNull()?.let { (it * 1000).toLong() } ?: 30_000,
    )
}
