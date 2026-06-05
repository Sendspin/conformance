using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.Json.Serialization;
using Microsoft.Extensions.Logging;
using Sendspin.SDK.Audio;
using Sendspin.SDK.Client;
using Sendspin.SDK.Connection;
using Sendspin.SDK.Discovery;
using Sendspin.SDK.Models;
using Sendspin.SDK.Protocol;
using Sendspin.SDK.Protocol.Messages;
using Sendspin.SDK.Synchronization;

var options = CliOptions.Parse(args);
var jsonOptions = new JsonSerializerOptions
{
    WriteIndented = true,
    PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
};

// Matches the SDK's wire-format serializer options (snake_case + null-field elision)
// so captured protocol payloads round-trip to the same JSON shape the server sent.
var wireJsonOptions = new JsonSerializerOptions
{
    PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
    DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
};
using var loggerFactory = LoggerFactory.Create(builder =>
{
    builder.AddSimpleConsole(console =>
    {
        console.SingleLine = true;
        console.TimestampFormat = "HH:mm:ss ";
    });
    builder.SetMinimumLevel(ParseLogLevel(options.LogLevel));
});

var pipeline = new HashingAudioPipeline(loggerFactory);
var disconnectTcs = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
ConnectionSnapshot? connectedServer = null;
string? failureReason = null;
JsonElement? peerHello = null;
JsonElement? playerStream = null;
Dictionary<string, object?>? receivedMetadata = null;
int metadataUpdateCount = 0;
Dictionary<string, object?>? receivedControllerState = null;
Dictionary<string, object?>? sentControllerCommand = null;
JsonElement? artworkStream = null;
int artworkCount = 0;
int artworkByteCount = 0;
string? artworkSha256 = null;

if (options.InitiatorRole == "client")
{
    await RunOutboundClientAsync();
}
else
{
    await RunListenerClientAsync();
}

if (connectedServer is null)
{
    failureReason ??= "Connection closed before handshake completed";
}

var summary = new Dictionary<string, object?>
{
    ["status"] = failureReason is null ? "ok" : "error",
    ["reason"] = failureReason,
    ["implementation"] = "sendspin-dotnet",
    ["role"] = "client",
    ["scenario_id"] = options.ScenarioId,
    ["initiator_role"] = options.InitiatorRole,
    ["preferred_codec"] = options.PreferredCodec,
    ["client_name"] = options.ClientName,
    ["client_id"] = options.ClientId,
    ["server"] = connectedServer,
    ["peer_hello"] = peerHello,
};

if (options.ScenarioId is "client-initiated-pcm" or "server-initiated-pcm" or "server-initiated-flac" or "server-initiated-pcm-24bit")
{
    summary["stream"] = playerStream;
    summary["audio"] = pipeline.Snapshot();
}
else if (options.ScenarioId is "client-initiated-metadata" or "server-initiated-metadata")
{
    summary["metadata"] = new Dictionary<string, object?>
    {
        ["update_count"] = metadataUpdateCount,
        ["received"] = receivedMetadata,
    };
}
else if (options.ScenarioId is "client-initiated-controller" or "server-initiated-controller")
{
    summary["controller"] = new Dictionary<string, object?>
    {
        ["received_state"] = receivedControllerState,
        ["sent_command"] = sentControllerCommand,
    };
}
else if (options.ScenarioId is "client-initiated-artwork" or "server-initiated-artwork")
{
    summary["stream"] = artworkStream;
    summary["artwork"] = new Dictionary<string, object?>
    {
        ["channel"] = 0,
        ["received_count"] = artworkCount,
        ["received_sha256"] = artworkSha256,
        ["byte_count"] = artworkByteCount,
    };
}

WriteJson(options.Summary, summary);
Console.WriteLine(JsonSerializer.Serialize(summary, jsonOptions));
return failureReason is null ? 0 : 1;

async Task RunListenerClientAsync()
{
    var capabilities = BuildCapabilities(options);
    await using var host = new SendspinHostService(
        loggerFactory,
        capabilities,
        new ListenerOptions
        {
            Port = options.Port,
            Path = options.Path,
        },
        new AdvertiserOptions
        {
            ClientId = capabilities.ClientId,
            PlayerName = capabilities.ClientName,
            Port = options.Port,
            Path = options.Path,
        },
        pipeline,
        new KalmanClockSynchronizer(loggerFactory.CreateLogger<KalmanClockSynchronizer>()));

    host.ServerConnected += (_, server) =>
    {
        connectedServer = new ConnectionSnapshot(server.ServerId, server.ServerName, "playback");
    };
    host.ServerDisconnected += (_, _) => disconnectTcs.TrySetResult(true);
    host.GroupStateChanged += (_, group) =>
    {
        HandleGroupState(group, () => host.SendCommandAsync(options.ControllerCommand));
    };
    host.ArtworkReceived += (_, e) => RecordArtwork(e.ImageData);

    await host.StartAsync();
    try
    {
        WriteRegistry(options.Registry, options.ClientName, $"ws://127.0.0.1:{options.Port}{options.Path}");
        WriteJson(
            options.Ready,
            new
            {
                status = "ready",
                scenario_id = options.ScenarioId,
                initiator_role = options.InitiatorRole,
                url = $"ws://127.0.0.1:{options.Port}{options.Path}",
            });

        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(options.TimeoutSeconds));
        await disconnectTcs.Task.WaitAsync(timeout.Token);
    }
    catch (OperationCanceledException)
    {
        failureReason ??= "Timed out waiting for server disconnect";
    }
    finally
    {
        await host.StopAsync();
    }
}

async Task RunOutboundClientAsync()
{
    WriteJson(
        options.Ready,
        new
        {
            status = "ready",
            scenario_id = options.ScenarioId,
            initiator_role = options.InitiatorRole,
        });

    using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(options.TimeoutSeconds));
    try
    {
        var serverUrl = await WaitForRegistryAsync(options.Registry, options.ServerName, timeout.Token);
        var capabilities = BuildCapabilities(options);
        await using var connection = new SendspinConnection(
            loggerFactory.CreateLogger<SendspinConnection>(),
            new ConnectionOptions
            {
                AutoReconnect = false,
            });

        using var client = new SendspinClientService(
            loggerFactory.CreateLogger<SendspinClientService>(),
            connection,
            new KalmanClockSynchronizer(loggerFactory.CreateLogger<KalmanClockSynchronizer>()),
            capabilities,
            pipeline);
        client.ServerHelloReceived += (_, payload) => peerHello = ToWireElement(payload);
        client.StreamStartReceived += (_, payload) => CaptureStreamStart(payload);
        client.GroupStateChanged += (_, group) =>
        {
            HandleGroupState(group, () => client.SendCommandAsync(options.ControllerCommand));
        };

        client.ConnectionStateChanged += (_, state) =>
        {
            if (state.NewState == ConnectionState.Disconnected)
            {
                disconnectTcs.TrySetResult(true);
            }
        };
        client.ArtworkReceived += (_, e) => RecordArtwork(e.ImageData);

        await client.ConnectAsync(new Uri(serverUrl), timeout.Token);
        connectedServer = new ConnectionSnapshot(
            client.ServerId ?? "unknown",
            client.ServerName ?? "unknown",
            client.ConnectionReason);

        await disconnectTcs.Task.WaitAsync(timeout.Token);
    }
    catch (OperationCanceledException) when (timeout.IsCancellationRequested)
    {
        failureReason ??= "Timed out waiting for server disconnect";
    }
    catch (Exception ex)
    {
        failureReason ??= ex.Message;
    }
}

void CaptureStreamStart(StreamStartPayload payload)
{
    if (options.ScenarioId is "client-initiated-pcm" or "server-initiated-pcm" or "server-initiated-flac" or "server-initiated-pcm-24bit"
        && payload.Format is not null)
    {
        playerStream = ToWireElement(payload.Format);
    }
    else if (options.ScenarioId is "client-initiated-artwork" or "server-initiated-artwork"
        && payload.Artwork is not null)
    {
        artworkStream = ToWireElement(payload.Artwork);
    }
}

JsonElement ToWireElement<T>(T payload) =>
    JsonSerializer.SerializeToElement(payload, wireJsonOptions);

void HandleGroupState(GroupState group, Func<Task>? sendControllerCommand)
{
    if (group.Metadata is not null)
    {
        metadataUpdateCount += 1;
        receivedMetadata = NormalizeMetadata(group.Metadata, group);
    }

    if (options.ScenarioId is not "client-initiated-controller" and not "server-initiated-controller")
    {
        return;
    }

    receivedControllerState = NormalizeController(group, options.ControllerCommand);
    if (sentControllerCommand is not null || sendControllerCommand is null)
    {
        return;
    }

    sentControllerCommand = BuildControllerCommand(options.ControllerCommand);
    _ = Task.Run(async () =>
    {
        try
        {
            await Task.Delay(TimeSpan.FromMilliseconds(500));
            await sendControllerCommand();
        }
        catch (Exception ex)
        {
            failureReason ??= ex.Message;
            disconnectTcs.TrySetResult(true);
        }
    });
}

void RecordArtwork(byte[] data)
{
    artworkCount += 1;
    artworkByteCount = data.Length;
    artworkSha256 = Hex(SHA256.HashData(data));
}

static Dictionary<string, object?> NormalizeMetadata(TrackMetadata metadata, GroupState group)
{
    Dictionary<string, object?>? progress = null;
    if (metadata.Progress is not null)
    {
        progress = new Dictionary<string, object?>
        {
            ["track_progress"] = metadata.Progress.TrackProgress is null ? null : Convert.ToInt32(metadata.Progress.TrackProgress.Value),
            ["track_duration"] = metadata.Progress.TrackDuration is null ? null : Convert.ToInt32(metadata.Progress.TrackDuration.Value),
            ["playback_speed"] = metadata.Progress.PlaybackSpeed is null ? null : Convert.ToInt32(metadata.Progress.PlaybackSpeed.Value),
        };
    }

    return new Dictionary<string, object?>
    {
        ["title"] = metadata.Title,
        ["artist"] = metadata.Artist,
        ["album_artist"] = metadata.AlbumArtist,
        ["album"] = metadata.Album,
        ["artwork_url"] = metadata.ArtworkUrl,
        ["year"] = metadata.Year,
        ["track"] = metadata.Track,
        // repeat/shuffle moved off TrackMetadata onto GroupState (the controller
        // role) in SDK v9; the metadata scenario routes them through the group.
        ["repeat"] = group.Repeat,
        ["shuffle"] = group.Shuffle,
        ["progress"] = progress,
    };
}

static Dictionary<string, object?> NormalizeController(GroupState group, string command)
{
    return new Dictionary<string, object?>
    {
        ["supported_commands"] = new[] { command },
        ["volume"] = group.Volume,
        ["muted"] = group.Muted,
    };
}

static Dictionary<string, object?> BuildControllerCommand(string command)
{
    return new Dictionary<string, object?>
    {
        ["command"] = command,
    };
}

static string Hex(byte[] bytes) =>
    Convert.ToHexString(bytes).ToLowerInvariant();

async Task<string> WaitForRegistryAsync(string path, string serverName, CancellationToken cancellationToken)
{
    while (!cancellationToken.IsCancellationRequested)
    {
        var url = ReadRegistry(path, serverName);
        if (!string.IsNullOrWhiteSpace(url))
        {
            return url;
        }

        await Task.Delay(100, cancellationToken);
    }

    throw new OperationCanceledException(cancellationToken);
}

static string? ReadRegistry(string path, string name)
{
    if (!File.Exists(path))
    {
        return null;
    }

    var payload = JsonSerializer.Deserialize<Dictionary<string, Dictionary<string, string>>>(
        File.ReadAllText(path));
    if (payload is null || !payload.TryGetValue(name, out var entry))
    {
        return null;
    }

    return entry.TryGetValue("url", out var url) ? url : null;
}

static ClientCapabilities BuildCapabilities(CliOptions options)
{
    var audioFormats = options.PreferredCodec.Equals("pcm", StringComparison.OrdinalIgnoreCase)
        ? new List<AudioFormat>
        {
            new() { Codec = "pcm", SampleRate = 8000, Channels = 1, BitDepth = 16 },
        }
        : new List<AudioFormat>
        {
            new() { Codec = "flac", SampleRate = 8000, Channels = 1, BitDepth = 16 },
            new() { Codec = "pcm", SampleRate = 8000, Channels = 1, BitDepth = 16 },
            new() { Codec = "flac", SampleRate = 44100, Channels = 2, BitDepth = 16 },
            new() { Codec = "pcm", SampleRate = 44100, Channels = 2, BitDepth = 16 },
        };

    var roles = options.ScenarioId switch
    {
        "client-initiated-metadata" or "server-initiated-metadata" => new List<string> { "metadata@v1" },
        "client-initiated-controller" or "server-initiated-controller" => new List<string> { "controller@v1" },
        "client-initiated-artwork" or "server-initiated-artwork" => new List<string> { "artwork@v1" },
        _ => new List<string> { "player@v1" },
    };

    return new ClientCapabilities
    {
        ClientId = options.ClientId,
        ClientName = options.ClientName,
        Roles = roles,
        BufferCapacity = 2_000_000,
        AudioFormats = audioFormats,
        ArtworkChannels = new List<ArtworkChannelSpec>
        {
            new ArtworkChannelSpec
            {
                Source = "album",
                Format = options.ArtworkFormat,
                MediaWidth = options.ArtworkWidth,
                MediaHeight = options.ArtworkHeight,
            },
        },
        ProductName = "Conformance Dotnet Client",
        Manufacturer = "Sendspin Conformance",
        SoftwareVersion = "0.1.0",
    };
}

void WriteRegistry(string path, string clientName, string url)
{
    Dictionary<string, Dictionary<string, string>> payload = new();
    if (File.Exists(path))
    {
        payload = JsonSerializer.Deserialize<Dictionary<string, Dictionary<string, string>>>(
            File.ReadAllText(path),
            jsonOptions) ?? new();
    }

    payload[clientName] = new Dictionary<string, string> { ["url"] = url };
    WriteJson(path, payload);
}

void WriteJson(string path, object payload)
{
    var directory = Path.GetDirectoryName(path);
    if (!string.IsNullOrEmpty(directory))
    {
        Directory.CreateDirectory(directory);
    }

    File.WriteAllText(path, JsonSerializer.Serialize(payload, jsonOptions) + Environment.NewLine);
}

static LogLevel ParseLogLevel(string value) =>
    value.ToUpperInvariant() switch
    {
        "TRACE" => LogLevel.Trace,
        "DEBUG" => LogLevel.Debug,
        "WARNING" => LogLevel.Warning,
        "ERROR" => LogLevel.Error,
        _ => LogLevel.Information,
    };

internal sealed record ConnectionSnapshot(string ServerId, string ServerName, string? ConnectionReason);

internal sealed class CliOptions
{
    public required string ClientName { get; init; }
    public required string ClientId { get; init; }
    public required string Summary { get; init; }
    public required string Ready { get; init; }
    public required string Registry { get; init; }
    public required string ScenarioId { get; init; }
    public required string InitiatorRole { get; init; }
    public required string PreferredCodec { get; init; }
    public required string ServerName { get; init; }
    public required string ServerId { get; init; }
    public required double TimeoutSeconds { get; init; }
    public required int Port { get; init; }
    public required string Path { get; init; }
    public required string LogLevel { get; init; }
    public required string MetadataTitle { get; init; }
    public required string MetadataArtist { get; init; }
    public required string MetadataAlbumArtist { get; init; }
    public required string MetadataAlbum { get; init; }
    public required string MetadataArtworkUrl { get; init; }
    public required int MetadataYear { get; init; }
    public required int MetadataTrack { get; init; }
    public required string MetadataRepeat { get; init; }
    public required string MetadataShuffle { get; init; }
    public required int MetadataTrackProgress { get; init; }
    public required int MetadataTrackDuration { get; init; }
    public required int MetadataPlaybackSpeed { get; init; }
    public required string ControllerCommand { get; init; }
    public required string ArtworkFormat { get; init; }
    public required int ArtworkWidth { get; init; }
    public required int ArtworkHeight { get; init; }

    public static CliOptions Parse(string[] args)
    {
        var values = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        for (var index = 0; index < args.Length; index += 2)
        {
            if (!args[index].StartsWith("--", StringComparison.Ordinal))
            {
                throw new ArgumentException($"Unexpected argument: {args[index]}");
            }

            if (index + 1 >= args.Length)
            {
                throw new ArgumentException($"Missing value for {args[index]}");
            }

            values[args[index][2..]] = args[index + 1];
        }

        return new CliOptions
        {
            ClientName = Require(values, "client-name"),
            ClientId = Require(values, "client-id"),
            Summary = Require(values, "summary"),
            Ready = Require(values, "ready"),
            Registry = Require(values, "registry"),
            ScenarioId = values.GetValueOrDefault("scenario-id", "server-initiated-flac"),
            InitiatorRole = values.GetValueOrDefault("initiator-role", "server"),
            PreferredCodec = values.GetValueOrDefault("preferred-codec", "flac"),
            ServerName = values.GetValueOrDefault("server-name", "Sendspin Conformance Server"),
            ServerId = values.GetValueOrDefault("server-id", "conformance-server"),
            TimeoutSeconds = double.Parse(values.GetValueOrDefault("timeout-seconds", "30"), System.Globalization.CultureInfo.InvariantCulture),
            Port = int.Parse(values.GetValueOrDefault("port", "8928"), System.Globalization.CultureInfo.InvariantCulture),
            Path = values.GetValueOrDefault("path", "/sendspin"),
            LogLevel = values.GetValueOrDefault("log-level", "Information"),
            MetadataTitle = values.GetValueOrDefault("metadata-title", "Almost Silent"),
            MetadataArtist = values.GetValueOrDefault("metadata-artist", "Sendspin Conformance"),
            MetadataAlbumArtist = values.GetValueOrDefault("metadata-album-artist", "Sendspin"),
            MetadataAlbum = values.GetValueOrDefault("metadata-album", "Protocol Fixtures"),
            MetadataArtworkUrl = values.GetValueOrDefault("metadata-artwork-url", "https://example.invalid/almost-silent.jpg"),
            MetadataYear = int.Parse(values.GetValueOrDefault("metadata-year", "2026"), System.Globalization.CultureInfo.InvariantCulture),
            MetadataTrack = int.Parse(values.GetValueOrDefault("metadata-track", "1"), System.Globalization.CultureInfo.InvariantCulture),
            MetadataRepeat = values.GetValueOrDefault("metadata-repeat", "all"),
            MetadataShuffle = values.GetValueOrDefault("metadata-shuffle", "false"),
            MetadataTrackProgress = int.Parse(values.GetValueOrDefault("metadata-track-progress", "12000"), System.Globalization.CultureInfo.InvariantCulture),
            MetadataTrackDuration = int.Parse(values.GetValueOrDefault("metadata-track-duration", "180000"), System.Globalization.CultureInfo.InvariantCulture),
            MetadataPlaybackSpeed = int.Parse(values.GetValueOrDefault("metadata-playback-speed", "1000"), System.Globalization.CultureInfo.InvariantCulture),
            ControllerCommand = values.GetValueOrDefault("controller-command", "next"),
            ArtworkFormat = values.GetValueOrDefault("artwork-format", "jpeg"),
            ArtworkWidth = int.Parse(values.GetValueOrDefault("artwork-width", "256"), System.Globalization.CultureInfo.InvariantCulture),
            ArtworkHeight = int.Parse(values.GetValueOrDefault("artwork-height", "256"), System.Globalization.CultureInfo.InvariantCulture),
        };
    }

    private static string Require(Dictionary<string, string> values, string key)
    {
        if (!values.TryGetValue(key, out var value) || string.IsNullOrWhiteSpace(value))
        {
            throw new ArgumentException($"Missing required option --{key}");
        }
        return value;
    }
}

internal sealed class HashingAudioPipeline : IAudioPipeline
{
    private readonly AudioDecoderFactory _decoderFactory;
    private IAudioDecoder? _decoder;
    private float[] _decodeBuffer = Array.Empty<float>();
    private readonly MemoryStream _decodedFloatStream = new();
    private readonly MemoryStream _encodedStream = new();
    private int _audioChunkCount;

    public HashingAudioPipeline(ILoggerFactory loggerFactory)
    {
        _decoderFactory = new AudioDecoderFactory(loggerFactory);
    }

    public AudioPipelineState State { get; private set; } = AudioPipelineState.Idle;
    public bool IsReady => _decoder is not null;
    public AudioBufferStats? BufferStats => null;
    public AudioFormat? CurrentFormat { get; private set; }
    public AudioFormat? OutputFormat => CurrentFormat;
    public int DetectedOutputLatencyMs => 0;

    public event EventHandler<AudioPipelineState>? StateChanged;
    public event EventHandler<AudioPipelineError>? ErrorOccurred;

    public Task StartAsync(AudioFormat format, long? targetTimestamp = null, CancellationToken cancellationToken = default)
    {
        _decoder?.Dispose();
        _decoder = _decoderFactory.Create(format);
        _decodeBuffer = new float[_decoder.MaxSamplesPerFrame];
        CurrentFormat = format;
        SetState(AudioPipelineState.Buffering);
        return Task.CompletedTask;
    }

    public Task StopAsync()
    {
        _decoder?.Dispose();
        _decoder = null;
        SetState(AudioPipelineState.Idle);
        return Task.CompletedTask;
    }

    public void NotifyReconnect() { }

    public void Clear(long? newTargetTimestamp = null)
    {
        _decoder?.Reset();
    }

    public void ProcessAudioChunk(AudioChunk chunk)
    {
        if (_decoder is null)
        {
            return;
        }

        _encodedStream.Write(chunk.EncodedData, 0, chunk.EncodedData.Length);
        _audioChunkCount += 1;
        var decodedCount = _decoder.Decode(chunk.EncodedData, _decodeBuffer);
        if (decodedCount <= 0)
        {
            return;
        }

        var bytes = MemoryMarshal.AsBytes(_decodeBuffer.AsSpan(0, decodedCount));
        _decodedFloatStream.Write(bytes);
        SetState(AudioPipelineState.Playing);
    }

    public void SetVolume(int volume) { }

    public void SetMuted(bool muted) { }

    public Task SwitchDeviceAsync(string? deviceId, CancellationToken cancellationToken = default) =>
        Task.CompletedTask;

    public ValueTask DisposeAsync()
    {
        _decoder?.Dispose();
        _decodedFloatStream.Dispose();
        _encodedStream.Dispose();
        return ValueTask.CompletedTask;
    }

    public object Snapshot() => new
    {
        received_pcm_sha256 = ToHex(SHA256.HashData(_decodedFloatStream.ToArray())),
        received_encoded_sha256 = ToHex(SHA256.HashData(_encodedStream.ToArray())),
        received_sample_count = _decodedFloatStream.Length / sizeof(float),
        audio_chunk_count = _audioChunkCount,
    };

    private void SetState(AudioPipelineState state)
    {
        if (State == state)
        {
            return;
        }

        State = state;
        StateChanged?.Invoke(this, state);
    }

    private static string ToHex(byte[] bytes) =>
        Convert.ToHexString(bytes).ToLowerInvariant();
}
