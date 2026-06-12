// Conformance adapter for SendspinKit — uses the public SDK to drive
// all six conformance scenarios (PCM, FLAC, metadata, artwork, controller)
// and writes the JSON summary the harness expects.

import CryptoKit
import Foundation
import Network
import SendspinKit

// MARK: - CLI argument parsing

struct CliOptions {
    // Required paths
    let summaryPath: String
    let readyPath: String
    let registryPath: String

    // Scenario
    let scenarioID: String
    let initiatorRole: String
    let preferredCodec: String
    let timeoutSeconds: Double

    // Identity
    let clientID: String
    let clientName: String
    let serverID: String
    let serverName: String

    // Networking (server-initiated)
    let port: Int
    let path: String

    // Metadata (passed through for metadata scenario)
    let metadataTitle: String?
    let metadataArtist: String?
    let metadataAlbumArtist: String?
    let metadataAlbum: String?
    let metadataArtworkURL: String?
    let metadataYear: String?
    let metadataTrack: String?
    let metadataRepeat: String?
    let metadataShuffle: String?
    let metadataTrackProgress: String?
    let metadataTrackDuration: String?
    let metadataPlaybackSpeed: String?

    // Controller
    let controllerCommand: String

    // Artwork
    let artworkFormat: String
    let artworkWidth: Int
    let artworkHeight: Int

    static func parse(_ arguments: [String]) throws -> CliOptions {
        let filtered = arguments.filter { $0 != "--" }
        var values: [String: String] = [:]
        var index = 0
        while index < filtered.count {
            let token = filtered[index]
            guard token.hasPrefix("--") else {
                index += 1
                continue
            }
            let key = String(token.dropFirst(2))
            // A following token is this flag's value only if it isn't itself a
            // flag; otherwise treat the flag as valueless and don't consume it.
            let next = index + 1 < filtered.count ? filtered[index + 1] : nil
            if let next, !next.hasPrefix("--") {
                values[key] = next
                index += 2
            } else {
                values[key] = ""
                index += 1
            }
        }

        guard let summaryPath = values["summary"], !summaryPath.isEmpty else {
            throw AdapterError("Missing required option --summary")
        }
        guard let readyPath = values["ready"], !readyPath.isEmpty else {
            throw AdapterError("Missing required option --ready")
        }

        return CliOptions(
            summaryPath: summaryPath,
            readyPath: readyPath,
            registryPath: values["registry"] ?? "",
            scenarioID: values["scenario-id"] ?? "client-initiated-pcm",
            initiatorRole: values["initiator-role"] ?? "client",
            preferredCodec: values["preferred-codec"] ?? "pcm",
            timeoutSeconds: Double(values["timeout-seconds"] ?? "30") ?? 30.0,
            clientID: values["client-id"] ?? "sendspinkit-conformance",
            clientName: values["client-name"] ?? "SendspinKit Conformance",
            serverID: values["server-id"] ?? "conformance-server",
            serverName: values["server-name"] ?? "Sendspin Conformance Server",
            // Default port/path match ClientAdvertiser defaults and the conformance harness
            port: Int(values["port"] ?? "8928") ?? 8928,
            path: values["path"] ?? "/sendspin",
            metadataTitle: values["metadata-title"],
            metadataArtist: values["metadata-artist"],
            metadataAlbumArtist: values["metadata-album-artist"],
            metadataAlbum: values["metadata-album"],
            metadataArtworkURL: values["metadata-artwork-url"],
            metadataYear: values["metadata-year"],
            metadataTrack: values["metadata-track"],
            metadataRepeat: values["metadata-repeat"],
            metadataShuffle: values["metadata-shuffle"],
            metadataTrackProgress: values["metadata-track-progress"],
            metadataTrackDuration: values["metadata-track-duration"],
            metadataPlaybackSpeed: values["metadata-playback-speed"],
            controllerCommand: values["controller-command"] ?? "next",
            artworkFormat: values["artwork-format"] ?? "jpeg",
            artworkWidth: Int(values["artwork-width"] ?? "256") ?? 256,
            artworkHeight: Int(values["artwork-height"] ?? "256") ?? 256
        )
    }

    var isPlayerScenario: Bool {
        scenarioID.contains("pcm") || scenarioID.contains("flac") || scenarioID.contains("opus")
    }

    /// Codecs whose conformance check compares the raw encoded chunk bytes the
    /// client received, rather than canonical decoded PCM.
    var usesEncodedByteVerification: Bool {
        preferredCodec == "flac" || preferredCodec == "opus"
    }

    var isMetadataScenario: Bool {
        scenarioID.contains("metadata")
    }

    var isControllerScenario: Bool {
        scenarioID.contains("controller")
    }

    var isArtworkScenario: Bool {
        scenarioID.contains("artwork")
    }

    var isClientInitiated: Bool {
        initiatorRole == "client"
    }

    var requiredRoles: Set<VersionedRole> {
        if isPlayerScenario { return [.playerV1] }
        if isMetadataScenario { return [.metadataV1] }
        if isControllerScenario { return [.controllerV1] }
        if isArtworkScenario { return [.artworkV1] }
        return [.playerV1]
    }
}

// MARK: - Errors

struct AdapterError: Error, CustomStringConvertible {
    let description: String
    init(_ description: String) { self.description = description }
}

/// Thrown by the timeout task to win the race against event consumption.
private struct TimeoutSignal: Error {}

// MARK: - Canonical float32 PCM hashing (matches conformance pcm.py)

/// Incremental SHA-256 hasher that converts integer PCM samples to canonical
/// little-endian float32 before hashing — the same algorithm used by the Python
/// harness's `FloatPcmHasher` and the Rust adapter's `FloatPcmHasher`.
struct FloatPcmHasher {
    /// PCM normalization divisors: 2^15, 2^23, 2^31
    private static let scale16: Float = Float(1 << 15)
    private static let scale24: Float = Float(1 << 23)
    private static let scale32: Float = Float(1 << 31)

    private var hasher = SHA256()
    private(set) var sampleCount: Int = 0

    /// Hash raw PCM bytes at the given bit depth, converting to canonical float32.
    mutating func update(pcmBytes: Data, bitDepth: Int) {
        switch bitDepth {
        case 16:
            update16Bit(pcmBytes)
        case 24:
            update24Bit(pcmBytes)
        case 32:
            update32Bit(pcmBytes)
        default:
            break
        }
    }

    func hexdigest() -> String {
        // `finalize()` is non-mutating, so it reads the running hash without
        // consuming it — `hexdigest()` stays callable more than once.
        hasher.finalize().map { String(format: "%02x", $0) }.joined()
    }

    private mutating func update16Bit(_ data: Data) {
        data.withUnsafeBytes { raw in
            let count = raw.count / 2
            sampleCount += count
            for i in 0 ..< count {
                let sample = raw.loadUnaligned(fromByteOffset: i * 2, as: Int16.self)
                var floatVal = Float(sample) / Self.scale16
                withUnsafeBytes(of: &floatVal) { hasher.update(data: $0) }
            }
        }
    }

    private mutating func update24Bit(_ data: Data) {
        data.withUnsafeBytes { raw in
            let base = raw.baseAddress!.assumingMemoryBound(to: UInt8.self)
            let count = raw.count / 3
            sampleCount += count
            for i in 0 ..< count {
                let off = i * 3
                var value = Int32(base[off])
                    | (Int32(base[off + 1]) << 8)
                    | (Int32(base[off + 2]) << 16)
                if value & 0x80_0000 != 0 {
                    value |= ~0xFF_FFFF // sign extend
                }
                var floatVal = Float(value) / Self.scale24
                withUnsafeBytes(of: &floatVal) { hasher.update(data: $0) }
            }
        }
    }

    private mutating func update32Bit(_ data: Data) {
        data.withUnsafeBytes { raw in
            let count = raw.count / 4
            sampleCount += count
            for i in 0 ..< count {
                let sample = raw.loadUnaligned(fromByteOffset: i * 4, as: Int32.self)
                var floatVal = Float(sample) / Self.scale32
                withUnsafeBytes(of: &floatVal) { hasher.update(data: $0) }
            }
        }
    }
}

// MARK: - Raw byte hasher (for FLAC frames and artwork)

struct RawHasher {
    private var hasher = SHA256()
    private(set) var byteCount: Int = 0

    mutating func update(_ data: Data) {
        data.withUnsafeBytes { hasher.update(data: $0) }
        byteCount += data.count
    }

    func hexdigest() -> String {
        hasher.finalize().map { String(format: "%02x", $0) }.joined()
    }
}

// MARK: - JSON helpers

func writeJSON(to path: String, payload: [String: Any?]) throws {
    let url = URL(fileURLWithPath: path)
    try FileManager.default.createDirectory(
        at: url.deletingLastPathComponent(),
        withIntermediateDirectories: true
    )
    let sanitized = payload.mapValues { $0 ?? NSNull() as Any }
    let data = try JSONSerialization.data(withJSONObject: sanitized, options: [.prettyPrinted, .sortedKeys])
    try data.write(to: url)
}

func readRegistryURL(registryPath: String, name: String, timeout: Double) async throws -> URL {
    let deadline = Date().addingTimeInterval(timeout)
    while Date() < deadline {
        if let data = try? Data(contentsOf: URL(fileURLWithPath: registryPath)),
           let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let entry = json[name] as? [String: Any],
           let urlString = entry["url"] as? String,
           let url = URL(string: urlString)
        {
            return url
        }
        try await Task.sleep(for: .milliseconds(100))
    }
    throw AdapterError("Timed out waiting for \(name) in registry")
}

func registerEndpoint(registryPath: String, name: String, url: String) throws {
    let fileURL = URL(fileURLWithPath: registryPath)
    var payload: [String: Any] = [:]
    if let existing = try? Data(contentsOf: fileURL),
       let json = try? JSONSerialization.jsonObject(with: existing) as? [String: Any]
    {
        payload = json
    }
    payload[name] = ["url": url]
    let data = try JSONSerialization.data(withJSONObject: payload, options: [.prettyPrinted])
    try FileManager.default.createDirectory(
        at: fileURL.deletingLastPathComponent(),
        withIntermediateDirectories: true
    )
    try data.write(to: fileURL)
}

/// True when a metadata update carries at least one populated field — i.e. a
/// real update, not the server's initial all-null clearing event. Used to
/// decide when the metadata scenario is satisfied without assuming any single
/// field (e.g. title) is always present.
func metadataHasContent(_ m: TrackMetadata) -> Bool {
    m.title != nil || m.artist != nil || m.albumArtist != nil || m.album != nil
        || m.artworkURL != nil || m.year != nil || m.track != nil || m.progress != nil
}

// MARK: - Collected state

/// Accumulates data from events during the conformance run.
actor ConformanceCollector {
    let options: CliOptions

    // Audio (player scenarios)
    var pcmHasher = FloatPcmHasher()
    var encodedHasher = RawHasher()
    var audioChunkCount: Int = 0
    var streamFormat: AudioFormatSpec?
    var codecHeaderBase64: String?
    /// PCM chunks received before the stream format was known, awaiting a bit
    /// depth so they can be hashed in arrival order.
    private var pendingPcmChunks: [Data] = []

    // Metadata
    var metadataUpdateCount: Int = 0
    var receivedMetadata: TrackMetadata?

    // Controller
    var receivedControllerState: ControllerState?
    var sentCommand: [String: Any]?

    // Artwork
    var artworkChannel: Int?
    var artworkReceivedCount: Int = 0
    var artworkHasher = RawHasher()

    // Server hello
    var peerHello: ServerInfo?

    /// Terminal status reported in the summary. Stays "ok" for a clean run;
    /// set to "timeout" when the run is cut short so the harness sees a failure
    /// with a clear reason instead of a missing summary.
    var runStatus: String = "ok"

    init(options: CliOptions) {
        self.options = options
    }

    func setRunStatus(_ status: String) {
        runStatus = status
    }

    func recordAudioChunk(data: Data) {
        audioChunkCount += 1
        // Raw encoded bytes (FLAC/Opus verification) need no format to hash.
        encodedHasher.update(data)
        // Canonical float32 hashing needs the bit depth.
        // "none" is a harness-level concept for non-audio scenarios that still
        // stream raw PCM — there is no AudioCodec.none in the SDK.
        guard options.preferredCodec == "pcm" || options.preferredCodec == "none" else { return }
        if let fmt = streamFormat {
            pcmHasher.update(pcmBytes: data, bitDepth: fmt.bitDepth)
        } else {
            // Defer rather than guess a depth and corrupt the running hash.
            pendingPcmChunks.append(data)
        }
    }

    func recordStreamFormat(_ format: AudioFormatSpec, codecHeader: Data?) {
        streamFormat = format
        if let header = codecHeader {
            codecHeaderBase64 = header.base64EncodedString()
        }
        // Flush chunks that arrived before the format was known, in order.
        for chunk in pendingPcmChunks {
            pcmHasher.update(pcmBytes: chunk, bitDepth: format.bitDepth)
        }
        pendingPcmChunks.removeAll()
    }

    func recordMetadata(_ metadata: TrackMetadata) {
        metadataUpdateCount += 1
        receivedMetadata = metadata
    }

    func recordControllerState(_ state: ControllerState) {
        receivedControllerState = state
    }

    func recordSentCommand(_ command: [String: Any]) {
        sentCommand = command
    }

    func recordArtwork(channel: Int, data: Data) {
        artworkChannel = channel
        artworkReceivedCount += 1
        artworkHasher.update(data)
    }

    func recordPeerHello(_ info: ServerInfo) {
        peerHello = info
    }

    /// Serialize the summary to JSON Data inside the actor, avoiding Sendable issues
    /// with `[String: Any?]` crossing actor boundaries.
    func buildSummaryJSON() throws -> Data {
        let summary = buildSummary()
        let sanitized = summary.mapValues { $0 ?? NSNull() as Any }
        return try JSONSerialization.data(withJSONObject: sanitized, options: [.prettyPrinted, .sortedKeys])
    }

    private func buildSummary() -> [String: Any?] {
        var summary: [String: Any?] = [
            "status": runStatus,
            "implementation": "SendspinKit",
            "role": "client",
            "scenario_id": options.scenarioID,
            "initiator_role": options.initiatorRole,
            "preferred_codec": options.preferredCodec,
            "client_name": options.clientName,
            "client_id": options.clientID,
        ]

        if let hello = peerHello {
            summary["peer_hello"] = [
                "type": "server/hello",
                "payload": [
                    "server_id": hello.serverId,
                    "name": hello.name,
                    "version": hello.version,
                    "connection_reason": hello.connectionReason.rawValue,
                ] as [String: Any],
            ] as [String: Any]
            summary["server"] = [
                "server_id": hello.serverId,
                "name": hello.name,
                "version": hello.version,
                "connection_reason": hello.connectionReason.rawValue,
            ] as [String: Any]
        } else {
            summary["peer_hello"] = nil
        }

        if options.isPlayerScenario {
            var streamDict: [String: Any] = [:]
            if let fmt = streamFormat {
                streamDict["codec"] = fmt.codec.rawValue
                streamDict["sample_rate"] = fmt.sampleRate
                streamDict["channels"] = fmt.channels
                streamDict["bit_depth"] = fmt.bitDepth
                streamDict["codec_header"] = codecHeaderBase64 as Any? ?? NSNull()
            }
            summary["stream"] = streamDict

            var audioDict: [String: Any] = [
                "audio_chunk_count": audioChunkCount,
                "received_sample_count": pcmHasher.sampleCount,
            ]
            if options.usesEncodedByteVerification {
                audioDict["received_encoded_sha256"] = encodedHasher.hexdigest()
            } else {
                audioDict["received_pcm_sha256"] = pcmHasher.hexdigest()
            }
            summary["audio"] = audioDict
        }

        if options.isMetadataScenario {
            var received: [String: Any] = [:]
            if let m = receivedMetadata {
                if let v = m.title { received["title"] = v }
                if let v = m.artist { received["artist"] = v }
                if let v = m.albumArtist { received["album_artist"] = v }
                if let v = m.album { received["album"] = v }
                if let v = m.artworkURL { received["artwork_url"] = v }
                if let v = m.year { received["year"] = v }
                if let v = m.track { received["track"] = v }
                if let p = m.progress {
                    received["progress"] = [
                        "track_progress": p.trackProgressMs,
                        "track_duration": p.trackDurationMs,
                        "playback_speed": p.playbackSpeedX1000,
                    ] as [String: Any]
                }
            }
            summary["metadata"] = [
                "update_count": metadataUpdateCount,
                "received": received,
            ] as [String: Any]
        }

        if options.isControllerScenario {
            var controllerDict: [String: Any] = [:]
            if let state = receivedControllerState {
                controllerDict["received_state"] = [
                    "supported_commands": Array(state.supportedCommands.map(\.rawValue)),
                    "volume": state.volume,
                    "muted": state.muted,
                    "repeat": state.repeatMode?.rawValue as Any? ?? NSNull(),
                    "shuffle": state.shuffle as Any? ?? NSNull(),
                ] as [String: Any]
            }
            if let cmd = sentCommand {
                controllerDict["sent_command"] = cmd
            }
            summary["controller"] = controllerDict
        }

        if options.isArtworkScenario {
            summary["artwork"] = [
                "channel": artworkChannel ?? 0,
                "received_count": artworkReceivedCount,
                "received_sha256": artworkHasher.hexdigest(),
                "byte_count": artworkHasher.byteCount,
            ] as [String: Any]
        }

        return summary
    }
}

// MARK: - NWConnection-backed WebSocket transport (local, conformance-only)

/// Minimal `SendspinTransport` implementation wrapping an NWConnection with
/// WebSocket framing. Replaces the library-internal `NWWebSocketTransport`
/// that was demoted from the public API.
actor ConformanceWebSocketTransport: SendspinTransport {
    private var connection: NWConnection?
    /// `nonisolated` so the receive callback yields frames directly in arrival
    /// order — a per-frame `Task` hop would let independent tasks reorder frames.
    private nonisolated let frameContinuation: AsyncStream<TransportFrame>.Continuation
    private let encoder = JSONEncoder()

    private let frames: AsyncStream<TransportFrame>

    /// Iterator over the frames stream, used by `nextFrame()`.
    /// Protected by the actor's serial work queue and the `isReading` flag.
    private var frameIterator: AsyncStream<TransportFrame>.AsyncIterator?

    /// Reentrancy guard for `nextFrame()` to enforce single-consumer contract.
    private var isReading = false

    /// Set when a WebSocket close frame is received, so the expected post-close
    /// receive error is suppressed rather than logged as a transport failure.
    private var closeReceived = false

    var isConnected: Bool {
        connection?.state == .ready
    }

    init(connection: NWConnection) {
        self.connection = connection

        let (frameStream, frameCont) = AsyncStream<TransportFrame>.makeStream()
        frames = frameStream
        frameContinuation = frameCont
    }

    /// Begin pumping messages from the NWConnection into the async streams.
    func startReceiving() {
        guard let connection else { return }
        receiveNext(on: connection)
    }

    func nextFrame() async -> TransportFrame? {
        precondition(!isReading, "SendspinTransport.nextFrame() is single-consumer; overlapping calls are a contract violation")
        if frameIterator == nil { frameIterator = frames.makeAsyncIterator() }
        isReading = true
        defer { isReading = false }
        // Copy the iterator to a local before awaiting: you cannot call the
        // `mutating async` next() on an actor-isolated stored property — Swift 6
        // rejects holding exclusive access across the suspension. AsyncStream's
        // iterator shares the underlying buffer by reference, so the copy-out /
        // copy-back is safe and loses no frames.
        //
        // Swift 6 flags passing the actor-isolated iterator to the nonisolated `next()`
        // as #SendingRisksDataRace; the local copy is `nonisolated(unsafe)` because the
        // `isReading` guard + actor serialization guarantee exactly one in-flight consumer,
        // so no concurrent access to the shared buffer occurs.
        nonisolated(unsafe) var iterator = frameIterator
        guard iterator != nil else { return nil }
        let frame = await iterator!.next()
        frameIterator = iterator
        return frame
    }

    func send(_ message: some Codable & Sendable) async throws {
        guard let connection, connection.state == .ready else {
            throw AdapterError("Transport not connected")
        }

        let data = try encoder.encode(message)
        let metadata = NWProtocolWebSocket.Metadata(opcode: .text)
        let context = NWConnection.ContentContext(
            identifier: "wsText",
            metadata: [metadata]
        )

        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            connection.send(
                content: data,
                contentContext: context,
                isComplete: true,
                completion: .contentProcessed { error in
                    if let error {
                        continuation.resume(throwing: error)
                    } else {
                        continuation.resume()
                    }
                }
            )
        }
    }

    func sendBinary(_ data: Data) async throws {
        guard let connection, connection.state == .ready else {
            throw AdapterError("Transport not connected")
        }

        let metadata = NWProtocolWebSocket.Metadata(opcode: .binary)
        let context = NWConnection.ContentContext(
            identifier: "binary",
            metadata: [metadata]
        )

        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            connection.send(
                content: data,
                contentContext: context,
                isComplete: true,
                completion: .contentProcessed { error in
                    if let error {
                        continuation.resume(throwing: error)
                    } else {
                        continuation.resume()
                    }
                }
            )
        }
    }

    func disconnect() async {
        // Finish the buffer first so a pending nextFrame() unblocks promptly, then cancel.
        frameContinuation.finish()
        connection?.cancel()
        connection = nil
    }

    // MARK: - Private

    private nonisolated func receiveNext(on connection: NWConnection) {
        connection.receiveMessage { [weak self] content, context, _, error in
            guard let self else { return }

            if let error {
                Task { await self.handleReceiveError(error) }
                return
            }

            if let metadata = context?.protocolMetadata(definition: NWProtocolWebSocket.definition)
                as? NWProtocolWebSocket.Metadata
            {
                switch metadata.opcode {
                case .text:
                    if let data = content, let text = String(data: data, encoding: .utf8) {
                        frameContinuation.yield(.text(text))
                    }
                case .binary:
                    if let data = content {
                        frameContinuation.yield(.binary(data))
                    }
                case .close:
                    // Note the close but keep receiving — frames may sit buffered behind
                    // it. The stream finishes when the loop ends (the post-close error).
                    Task { await self.handleClose() }
                default:
                    break
                }
            }

            receiveNext(on: connection)
        }
    }

    /// Records a WebSocket close so the expected post-close receive error is suppressed.
    private func handleClose() {
        closeReceived = true
    }

    /// Finishes the frame buffer when the receive loop ends. Logs an unexpected error,
    /// but stays quiet for the expected error that follows a clean close.
    private func handleReceiveError(_ error: NWError) {
        if !closeReceived {
            fputs("[ADAPTER] Transport receive error: \(error)\n", stderr)
        }
        frameContinuation.finish()
    }
}

// MARK: - NWListener-based WebSocket server for server-initiated connections

/// Listens for a single inbound WebSocket connection and produces a transport.
/// NWListener doesn't filter by path — any WebSocket upgrade on this port is accepted.
func acceptInboundConnection(
    port: UInt16,
    path _: String,
    timeout: Double,
    onListening: @escaping @Sendable () throws -> Void
) async throws -> ConformanceWebSocketTransport {
    let parameters = NWParameters.tcp
    let wsOptions = NWProtocolWebSocket.Options()
    parameters.defaultProtocolStack.applicationProtocols.insert(wsOptions, at: 0)

    let listener = try NWListener(using: parameters, on: NWEndpoint.Port(rawValue: port)!)

    // Guards against double-resuming the continuation. Safe without a lock because
    // both listener and connection callbacks dispatch on DispatchQueue.main.
    final class ResumeGuard: @unchecked Sendable {
        private var _resumed = false
        func tryResume() -> Bool {
            if _resumed { return false }
            _resumed = true
            return true
        }
    }
    let guard_ = ResumeGuard()
    let listenGuard = ResumeGuard()

    // Use DispatchQueue.main — NWConnection callbacks rely on the main queue
    // being serviced. Swift's async runtime keeps it alive in async main().
    return try await withCheckedThrowingContinuation { continuation in
        listener.newConnectionHandler = { connection in
            listener.newConnectionHandler = nil
            connection.start(queue: .main)

            connection.stateUpdateHandler = { state in
                switch state {
                case .ready:
                    if guard_.tryResume() {
                        listener.cancel() // one inbound connection is all we need
                        let transport = ConformanceWebSocketTransport(connection: connection)
                        Task { await transport.startReceiving() }
                        continuation.resume(returning: transport)
                    }
                case let .failed(error):
                    if guard_.tryResume() {
                        listener.cancel()
                        continuation.resume(throwing: error)
                    }
                case .cancelled:
                    if guard_.tryResume() {
                        listener.cancel()
                        continuation.resume(throwing: AdapterError("Connection cancelled"))
                    }
                default:
                    break
                }
            }
        }

        listener.stateUpdateHandler = { state in
            switch state {
            case .ready:
                // Socket is bound and accepting; only now is it safe to signal
                // readiness, so an inbound connection can't race ahead of bind.
                if listenGuard.tryResume() {
                    do {
                        try onListening()
                    } catch {
                        if guard_.tryResume() {
                            listener.cancel()
                            continuation.resume(throwing: error)
                        }
                    }
                }
            case let .failed(error):
                if guard_.tryResume() {
                    listener.cancel()
                    continuation.resume(throwing: error)
                }
            default:
                break
            }
        }

        listener.start(queue: .main)

        // Bound the wait: if no server dials in, fail with a clear error rather
        // than hanging until the harness SIGKILLs us with no summary written.
        // The ResumeGuard makes this a no-op if a connection already won the
        // race, so no explicit timer cancellation is needed.
        DispatchQueue.main.asyncAfter(deadline: .now() + timeout) {
            if guard_.tryResume() {
                listener.cancel()
                continuation.resume(throwing: AdapterError(
                    "Timed out after \(timeout)s waiting for inbound connection on port \(port)"
                ))
            }
        }
    }
}

// MARK: - Main entry point

@main
struct ConformanceSendspinKitClient {
    static func main() async {
        do {
            try await run()
        } catch {
            fputs("FATAL: \(error)\n", stderr)
            Foundation.exit(1)
        }
    }

    static func run() async throws {
        let options = try CliOptions.parse(Array(CommandLine.arguments.dropFirst()))
        let collector = ConformanceCollector(options: options)

        // Build the client with the right roles for this scenario
        let roles = options.requiredRoles
        var playerConfig: PlayerConfiguration?
        var artworkConfig: ArtworkConfiguration?

        if options.isPlayerScenario {
            // Declare formats that cover the conformance fixture (8kHz/1ch/16bit)
            // and common production formats. The server picks the closest match,
            // so listing the fixture format first avoids unnecessary resampling.
            let codec: AudioCodec
            switch options.preferredCodec {
            case "flac": codec = .flac
            case "opus": codec = .opus
            default: codec = .pcm
            }
            let formats = try [
                // Fixture native format — must match so hashes align
                AudioFormatSpec(codec: codec, channels: 1, sampleRate: 8000, bitDepth: 16),
                AudioFormatSpec(codec: codec, channels: 2, sampleRate: 8000, bitDepth: 16),
                AudioFormatSpec(codec: codec, channels: 1, sampleRate: 44100, bitDepth: 16),
                AudioFormatSpec(codec: codec, channels: 2, sampleRate: 44100, bitDepth: 16),
                AudioFormatSpec(codec: codec, channels: 1, sampleRate: 48000, bitDepth: 16),
                AudioFormatSpec(codec: codec, channels: 2, sampleRate: 48000, bitDepth: 16),
            ]
            playerConfig = try PlayerConfiguration(
                bufferCapacity: 48000 * 2 * 2 * 5, // ~5s at 48kHz stereo 16-bit
                supportedFormats: formats,
                volumeMode: .none, // headless — no audio output needed
                emitRawAudioEvents: true
            )
        }

        if options.isArtworkScenario {
            artworkConfig = try ArtworkConfiguration(
                channels: [
                    ArtworkChannel(
                        source: .album,
                        format: ImageFormat(rawValue: options.artworkFormat) ?? .jpeg,
                        mediaWidth: options.artworkWidth,
                        mediaHeight: options.artworkHeight
                    ),
                ]
            )
        }

        let client = try await SendspinClient(
            clientId: options.clientID,
            name: options.clientName,
            roles: roles,
            playerConfig: playerConfig,
            artworkConfig: artworkConfig
        )

        // Connect based on initiator role
        if options.isClientInitiated {
            // Write ready file (no URL — we're the initiator)
            try writeJSON(to: options.readyPath, payload: [
                "status": "ready",
                "scenario_id": options.scenarioID,
                "initiator_role": options.initiatorRole,
            ])

            // Wait for server URL in registry
            let serverURL = try await readRegistryURL(
                registryPath: options.registryPath,
                name: options.serverName,
                timeout: options.timeoutSeconds
            )
            fputs("[ADAPTER] Connecting to server at \(serverURL)\n", stderr)
            try await client.connect(to: serverURL)
        } else {
            // Server-initiated: listen for inbound connection
            let wsURL = "ws://127.0.0.1:\(options.port)\(options.path)"

            fputs("[ADAPTER] Listening for server connection on \(wsURL)\n", stderr)
            // Publish our endpoint (registry + ready file) only once the
            // listener is bound. The server resolves us via the registry and
            // dials it with a single, non-retrying attempt, so advertising
            // before bind lets it connect into a closed port and fail.
            let registryPath = options.registryPath
            let clientName = options.clientName
            let readyPath = options.readyPath
            let scenarioID = options.scenarioID
            let initiatorRole = options.initiatorRole
            let transport = try await acceptInboundConnection(
                port: UInt16(options.port),
                path: options.path,
                timeout: options.timeoutSeconds
            ) {
                if !registryPath.isEmpty {
                    try registerEndpoint(
                        registryPath: registryPath,
                        name: clientName,
                        url: wsURL
                    )
                }
                try writeJSON(to: readyPath, payload: [
                    "status": "ready",
                    "scenario_id": scenarioID,
                    "initiator_role": initiatorRole,
                    "url": wsURL,
                ])
            }
            try await client.acceptConnection(transport)
        }

        // Consume events until the scenario signals completion. The timeout is
        // enforced by racing consumption against a sleep (below), so a
        // connected-but-silent peer can't hang us until the harness SIGKILLs
        // the process with no summary written. The streams are AsyncStreams, so
        // cancelling the consuming tasks unblocks the `for await` loops promptly.
        @Sendable func consumeControlEvents() async throws -> Bool {
            var done = false
            for await event in await client.events() {
                switch event {
                case let .serverConnected(info):
                    fputs("[ADAPTER] Connected to server: \(info.name)\n", stderr)
                    await collector.recordPeerHello(info)

                case let .streamStarted(format):
                    fputs("[ADAPTER] Stream started: \(format.codec.rawValue) \(format.sampleRate)Hz \(format.channels)ch \(format.bitDepth)bit\n", stderr)
                    let codecHeader = await client.currentCodecHeader
                    await collector.recordStreamFormat(format, codecHeader: codecHeader)

                case let .streamFormatChanged(format):
                    fputs("[ADAPTER] Stream format changed: \(format.codec.rawValue) \(format.sampleRate)Hz\n", stderr)
                    let codecHeader = await client.currentCodecHeader
                    await collector.recordStreamFormat(format, codecHeader: codecHeader)

                case .streamEnded:
                    fputs("[ADAPTER] Stream ended\n", stderr)
                    if options.isPlayerScenario { done = true }

                case let .metadataReceived(metadata):
                    fputs("[ADAPTER] Metadata: \(metadata.title ?? "(nil)")\n", stderr)
                    await collector.recordMetadata(metadata)
                    // Metadata scenario: done when we receive metadata with actual content.
                    // The server may send an initial "all null" clearing update first.
                    if options.isMetadataScenario, metadataHasContent(metadata) {
                        done = true
                    }

                case let .controllerStateUpdated(state):
                    let supportedCommands = state.supportedCommands.map { $0.rawValue }
                    fputs("[ADAPTER] Controller state: \(supportedCommands)\n", stderr)
                    await collector.recordControllerState(state)

                    // Send the expected command back via the typed public API, but only
                    // once the server advertises it in supportedCommands. Per the SDK
                    // contract (see SendspinClient.next()) and the protocol, the server
                    // silently drops unsupported commands: aiosendspin sends an initial
                    // state without the app command, then a second update that adds it.
                    if options.isControllerScenario,
                       supportedCommands.contains(options.controllerCommand) {
                        let cmdString = options.controllerCommand
                        switch cmdString {
                        case "play": try await client.play()
                        case "pause": try await client.pause()
                        case "stop": try await client.stopPlayback()
                        case "next": try await client.next()
                        case "previous": try await client.previous()
                        case "repeat_off": try await client.repeatOff()
                        case "repeat_one": try await client.repeatOne()
                        case "repeat_all": try await client.repeatAll()
                        case "shuffle": try await client.shuffle()
                        case "unshuffle": try await client.unshuffle()
                        case "switch": try await client.switchGroup()
                        default:
                            fputs("[ADAPTER] Unknown controller command: \(cmdString)\n", stderr)
                            break
                        }
                        await collector.recordSentCommand(["command": cmdString])
                        fputs("[ADAPTER] Sent controller command: \(cmdString)\n", stderr)
                        done = true
                    }

                case let .disconnected(reason):
                    fputs("[ADAPTER] Disconnected: \(reason)\n", stderr)
                    done = true

                default:
                    break
                }

                if done { return true }
            }
            return false
        }

        @Sendable func consumeAudioChunks() async -> Bool {
            for await chunk in await client.audioChunks {
                // The collector buffers chunks that arrive before stream/start
                // is processed (clock sync can delay it) and hashes them once
                // the format — and thus the bit depth — is known.
                await collector.recordAudioChunk(data: chunk.data)
            }
            return false
        }

        @Sendable func consumeArtwork() async -> Bool {
            for await artwork in await client.artwork {
                fputs("[ADAPTER] Artwork received: channel=\(artwork.channel) bytes=\(artwork.data.count)\n", stderr)
                await collector.recordArtwork(channel: artwork.channel, data: artwork.data)
                if options.isArtworkScenario { return true }
            }
            return false
        }

        let completed: Bool
        do {
            completed = try await withThrowingTaskGroup(of: Bool.self) { group in
                group.addTask { try await consumeControlEvents() }
                group.addTask { await consumeAudioChunks() }
                group.addTask { await consumeArtwork() }
                group.addTask {
                    try await Task.sleep(for: .seconds(options.timeoutSeconds))
                    throw TimeoutSignal()
                }
                defer { group.cancelAll() }
                return try await group.next() ?? false
            }
        } catch is TimeoutSignal {
            fputs("[ADAPTER] Timeout reached after \(options.timeoutSeconds)s\n", stderr)
            await collector.setRunStatus("timeout")
            completed = false
        }

        if !completed {
            fputs("[ADAPTER] Event stream ended without explicit done signal\n", stderr)
        }
        await client.disconnect(reason: .shutdown)

        // Write summary
        let summaryData = try await collector.buildSummaryJSON()
        let summaryURL = URL(fileURLWithPath: options.summaryPath)
        try FileManager.default.createDirectory(
            at: summaryURL.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        try summaryData.write(to: summaryURL)

        // Also print to stdout for debugging
        FileHandle.standardOutput.write(summaryData)
        FileHandle.standardOutput.write(Data([0x0A]))

        fputs("[ADAPTER] Summary written to \(options.summaryPath)\n", stderr)

        // Exit immediately — NWListener/NWConnection teardown during process exit
        // can trigger SIGTRAP if continuations or state handlers fire after the
        // async runtime starts winding down.
        Foundation.exit(0)
    }
}
