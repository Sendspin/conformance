use clap::Parser;
use sendspin::audio::{AudioBuffer, OutputError, PlayerEvent, PlayerOutput};
use sendspin::protocol::messages::{
    ArtworkChannel, ArtworkSource, ArtworkV1Support, AudioFormatSpec, ClientCommand,
    ControllerCommand, ControllerCommandType, ControllerState, ImageFormat, Message,
    MetadataState, PlayerState, PlayerV1Support, StreamPlayerConfig,
};
use sendspin::roles::{ArtworkEvent, ArtworkOutput};
use sendspin::{MemoryStore, ProtocolClient, ProtocolClientBuilder};
use sha2::{Digest, Sha256};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::time::sleep;

#[derive(Parser, Debug, Clone)]
struct Args {
    #[arg(long)]
    client_name: String,
    #[arg(long)]
    client_id: String,
    #[arg(long)]
    summary: PathBuf,
    #[arg(long)]
    ready: PathBuf,
    #[arg(long)]
    registry: PathBuf,
    #[arg(long, default_value = "client-initiated-pcm")]
    scenario_id: String,
    #[arg(long, default_value = "client")]
    initiator_role: String,
    #[arg(long, default_value = "pcm")]
    preferred_codec: String,
    #[arg(long, default_value = "Sendspin Conformance Server")]
    server_name: String,
    #[arg(long, default_value = "conformance-server")]
    server_id: String,
    #[arg(long, default_value_t = 30.0)]
    timeout_seconds: f64,
    #[arg(long, default_value_t = 8928)]
    port: u16,
    #[arg(long, default_value = "/sendspin")]
    path: String,
    #[arg(long, default_value = "info")]
    log_level: String,
    #[arg(long, default_value = "Almost Silent")]
    metadata_title: String,
    #[arg(long, default_value = "Sendspin Conformance")]
    metadata_artist: String,
    #[arg(long, default_value = "Sendspin")]
    metadata_album_artist: String,
    #[arg(long, default_value = "Protocol Fixtures")]
    metadata_album: String,
    #[arg(long, default_value = "https://example.invalid/almost-silent.jpg")]
    metadata_artwork_url: String,
    #[arg(long, default_value_t = 2026)]
    metadata_year: i32,
    #[arg(long, default_value_t = 1)]
    metadata_track: i32,
    #[arg(long, default_value_t = 12_000)]
    metadata_track_progress: i64,
    #[arg(long, default_value_t = 180_000)]
    metadata_track_duration: i64,
    #[arg(long, default_value_t = 1_000)]
    metadata_playback_speed: i32,
    #[arg(long, default_value = "next")]
    controller_command: String,
    // Accepted for CLI symmetry with the server; repeat/shuffle are observed
    // from the controller state, not these values.
    #[arg(long, default_value = "all")]
    controller_repeat: String,
    #[arg(long, default_value = "false")]
    controller_shuffle: String,
    #[arg(long, default_value = "jpeg")]
    artwork_format: String,
    #[arg(long, default_value_t = 256)]
    artwork_width: u32,
    #[arg(long, default_value_t = 256)]
    artwork_height: u32,
}

#[derive(Default)]
struct FloatPcmHasher {
    hasher: Sha256,
    sample_count: usize,
}

impl FloatPcmHasher {
    fn update_from_pcm_bytes(&mut self, pcm_bytes: &[u8], bit_depth: u8) -> Result<(), String> {
        match bit_depth {
            16 => {
                for chunk in pcm_bytes.chunks_exact(2) {
                    let sample = i16::from_le_bytes([chunk[0], chunk[1]]) as f32 / 32768.0;
                    self.hasher.update(sample.to_le_bytes());
                    self.sample_count += 1;
                }
                Ok(())
            }
            24 => {
                for chunk in pcm_bytes.chunks_exact(3) {
                    let mut value =
                        (chunk[0] as i32) | ((chunk[1] as i32) << 8) | ((chunk[2] as i32) << 16);
                    if value & 0x800000 != 0 {
                        value |= !0x00FF_FFFF;
                    }
                    let sample = value as f32 / 8_388_608.0;
                    self.hasher.update(sample.to_le_bytes());
                    self.sample_count += 1;
                }
                Ok(())
            }
            32 => {
                for chunk in pcm_bytes.chunks_exact(4) {
                    let sample = i32::from_le_bytes([chunk[0], chunk[1], chunk[2], chunk[3]])
                        as f32
                        / 2_147_483_648.0;
                    self.hasher.update(sample.to_le_bytes());
                    self.sample_count += 1;
                }
                Ok(())
            }
            _ => Err(format!("Unsupported PCM bit depth: {bit_depth}")),
        }
    }

    fn hexdigest(&self) -> String {
        let digest = self.hasher.clone().finalize();
        hex_lower(&digest)
    }
}

fn hex_lower(bytes: &[u8]) -> String {
    let mut output = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        output.push_str(&format!("{byte:02x}"));
    }
    output
}

fn write_json(path: &Path, value: &serde_json::Value) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|err| err.to_string())?;
    }
    let content = serde_json::to_string_pretty(value).map_err(|err| err.to_string())?;
    fs::write(path, format!("{content}\n")).map_err(|err| err.to_string())
}

fn register_endpoint(registry_path: &Path, client_name: &str, url: &str) -> Result<(), String> {
    let mut payload = if registry_path.exists() {
        serde_json::from_str::<serde_json::Value>(
            &fs::read_to_string(registry_path).map_err(|err| err.to_string())?,
        )
        .unwrap_or_else(|_| serde_json::json!({}))
    } else {
        serde_json::json!({})
    };
    payload[client_name] = serde_json::json!({ "url": url });
    write_json(registry_path, &payload)
}

fn is_chunk_framing_scenario(scenario_id: &str) -> bool {
    scenario_id == "server-initiated-audio-chunk-framing"
}

fn is_metadata_scenario(scenario_id: &str) -> bool {
    matches!(
        scenario_id,
        "client-initiated-metadata" | "server-initiated-metadata"
    )
}

fn is_controller_scenario(scenario_id: &str) -> bool {
    matches!(
        scenario_id,
        "client-initiated-controller" | "server-initiated-controller"
    )
}

fn is_artwork_scenario(scenario_id: &str) -> bool {
    matches!(
        scenario_id,
        "client-initiated-artwork" | "server-initiated-artwork"
    )
}

async fn wait_for_server_url(
    registry_path: &Path,
    server_name: &str,
    timeout_s: f64,
) -> Result<String, String> {
    let deadline = Instant::now() + Duration::from_secs_f64(timeout_s);
    while Instant::now() < deadline {
        if let Ok(content) = fs::read_to_string(registry_path) {
            if let Ok(value) = serde_json::from_str::<serde_json::Value>(&content) {
                if let Some(url) = value
                    .get(server_name)
                    .and_then(|entry| entry.get("url"))
                    .and_then(|url| url.as_str())
                {
                    return Ok(url.to_string());
                }
            }
        }
        sleep(Duration::from_millis(100)).await;
    }
    Err(format!("Timed out waiting for server {server_name:?}"))
}

fn normalize_metadata(metadata: &MetadataState) -> serde_json::Value {
    serde_json::json!({
        "title": metadata.title,
        "artist": metadata.artist,
        "album_artist": metadata.album_artist,
        "album": metadata.album,
        "artwork_url": metadata.artwork_url,
        "year": metadata.year,
        "track": metadata.track,
        "progress": metadata.progress.as_ref().map(|progress| serde_json::json!({
            "track_progress": progress.track_progress,
            "track_duration": progress.track_duration,
            "playback_speed": progress.playback_speed,
        })),
    })
}

fn normalize_controller(controller: &ControllerState) -> serde_json::Value {
    serde_json::json!({
        "supported_commands": controller.supported_commands,
        "volume": controller.volume,
        "muted": controller.muted,
        "repeat": controller.repeat,
        "shuffle": controller.shuffle,
    })
}

fn build_ready(args: &Args, url: Option<&str>) -> serde_json::Value {
    let mut value = serde_json::json!({
        "status": "ready",
        "scenario_id": args.scenario_id,
        "initiator_role": args.initiator_role,
    });
    if let Some(url) = url {
        value["url"] = serde_json::Value::String(url.to_string());
    }
    value
}


struct NullOutput;

impl PlayerOutput for NullOutput {
    fn commit(&self, _: AudioBuffer) -> Result<(), OutputError> {
        Ok(())
    }
    fn clear(&self) -> Result<(), OutputError> {
        Ok(())
    }
    fn set_volume(&self, _: u8) -> Result<(), OutputError> {
        Ok(())
    }
    fn set_mute(&self, _: bool) -> Result<(), OutputError> {
        Ok(())
    }
    fn set_static_delay(&self, _: u16) -> Result<(), OutputError> {
        Ok(())
    }
}

impl ArtworkOutput for NullOutput {
    type Prepared = ();
    fn commit(&self, _: u8, _: ()) -> Result<(), OutputError> {
        Ok(())
    }
    fn end(&self) -> Result<(), OutputError> {
        Ok(())
    }
}

fn build_protocol_client_builder(args: &Args) -> ProtocolClientBuilder {
    let builder = ProtocolClientBuilder::builder()
        .persistence(Arc::new(MemoryStore::new()))
        .unpaired_access(true)
        .name(args.client_name.clone())
        .product_name(Some("sendspin-rs Conformance Client".to_string()))
        .manufacturer(Some("Sendspin Conformance".to_string()))
        .software_version(Some("0.1.0".to_string()));
    if is_metadata_scenario(&args.scenario_id) {
        return builder.metadata().build();
    }
    if is_controller_scenario(&args.scenario_id) {
        return builder.controller().build();
    }
    if is_artwork_scenario(&args.scenario_id) {
        return builder
            .artwork_v1_support(ArtworkV1Support {
                channels: vec![ArtworkChannel {
                    source: ArtworkSource::Album,
                    format: match args.artwork_format.as_str() {
                        "png" => ImageFormat::Png,
                        "bmp" => ImageFormat::Bmp,
                        _ => ImageFormat::Jpeg,
                    },
                    media_width: args.artwork_width,
                    media_height: args.artwork_height,
                }],
            })
            .build();
    }
    let pcm = |bit_depth| AudioFormatSpec {
        codec: "pcm".to_string(),
        channels: 1,
        sample_rate: 8000,
        bit_depth,
    };
    let supported_formats = if args.preferred_codec == "pcm" {
        let bit_depth = if args.scenario_id == "server-initiated-pcm-24bit" {
            24
        } else {
            16
        };
        vec![pcm(bit_depth)]
    } else {
        vec![
            AudioFormatSpec {
                codec: "flac".to_string(),
                channels: 1,
                sample_rate: 8000,
                bit_depth: 16,
            },
            pcm(16),
        ]
    };
    builder
        .player_v1_support(PlayerV1Support {
            supported_formats,
            buffer_capacity: 2_000_000,
            supported_commands: vec!["volume".to_string(), "mute".to_string()],
        })
        .initial_player_state(PlayerState {
            volume: Some(100),
            muted: Some(false),
            static_delay_ms: None,
            min_buffer_ms: None,
            required_lead_time_ms: None,
            supported_commands: None,
        })
        .build()
}

#[derive(Default)]
struct PlayerObservation {
    stream: Option<StreamPlayerConfig>,
    chunk_count: usize,
    encoded: Sha256,
    pcm: FloatPcmHasher,
}

#[derive(Default)]
struct ArtworkObservation {
    stream: Option<serde_json::Value>,
    channel: Option<u8>,
    count: usize,
    bytes: Sha256,
    byte_count: usize,
}

#[derive(Default)]
struct MessageObservation {
    server: Option<serde_json::Value>,
    metadata_update_count: usize,
    metadata: Option<serde_json::Value>,
    controller_state: Option<serde_json::Value>,
    sent_command: Option<serde_json::Value>,
}

async fn run_session(args: &Args, client: ProtocolClient) -> serde_json::Value {
    let server = serde_json::json!({
        "server_id": client.server_id(),
        "name": client.server_hello().name,
        "activation": client.activation(),
    });
    let conn = client.split();
    let mut messages = conn.messages;
    let sender = conn.sender;
    let mut player = conn.player.attach(NullOutput);
    let mut artwork = conn.artwork.attach(NullOutput);
    let _guard = conn.guard;

    let player_task = async {
        let mut observed = PlayerObservation::default();
        while let Some(event) = player.recv().await {
            match event {
                PlayerEvent::Start(config) => observed.stream = Some(config),
                PlayerEvent::Audio(work) => {
                    let stream = observed
                        .stream
                        .as_ref()
                        .ok_or_else(|| "Received audio before stream/start".to_string())?;
                    observed.encoded.update(work.data());
                    if stream.codec == "pcm" {
                        observed
                            .pcm
                            .update_from_pcm_bytes(work.data(), stream.bit_depth)?;
                    }
                    observed.chunk_count += 1;
                }
                _ => {}
            }
        }
        Ok::<_, String>(observed)
    };
    let artwork_task = async {
        let mut observed = ArtworkObservation::default();
        while let Some(event) = artwork.recv().await {
            match event {
                ArtworkEvent::Start(config) => {
                    observed.stream = Some(serde_json::to_value(config).unwrap())
                }
                ArtworkEvent::Data(work) => {
                    observed.channel = Some(work.channel());
                    observed.count += 1;
                    observed.byte_count += work.data().len();
                    observed.bytes.update(work.data());
                }
                ArtworkEvent::End => {}
            }
        }
        observed
    };
    let message_task = async {
        let mut observed = MessageObservation {
            server: Some(server),
            ..MessageObservation::default()
        };
        while let Some(message) = messages.recv().await {
            let Message::ServerState(state) = message else {
                continue;
            };
            if let Some(metadata) = state.metadata.as_ref() {
                observed.metadata_update_count += 1;
                observed.metadata = Some(normalize_metadata(metadata));
            }
            let Some(controller) = state.controller.as_ref() else {
                continue;
            };
            observed.controller_state = Some(normalize_controller(controller));
            if is_controller_scenario(&args.scenario_id)
                && observed.sent_command.is_none()
                && controller.supported_commands.contains(&args.controller_command)
            {
                let command: ControllerCommandType = serde_json::from_value(
                    serde_json::Value::String(args.controller_command.clone()),
                )
                .map_err(|err| err.to_string())?;
                sender
                    .send_message(Message::ClientCommand(ClientCommand {
                        controller: Some(ControllerCommand {
                            command,
                            volume: None,
                            mute: None,
                            position_ms: None,
                            offset_ms: None,
                        }),
                    }))
                    .await
                    .map_err(|err| err.to_string())?;
                observed.sent_command = Some(serde_json::json!({
                    "command": args.controller_command,
                }));
            }
        }
        Ok::<_, String>(observed)
    };

    let timeout = Duration::from_secs_f64(args.timeout_seconds);
    let joined = tokio::time::timeout(timeout, async {
        tokio::join!(player_task, artwork_task, message_task)
    })
    .await;
    let (player, artwork, messages) = match joined {
        Err(_) => return build_error_summary(args, "Timed out waiting for server disconnect"),
        Ok((Err(reason), _, _)) | Ok((_, _, Err(reason))) => {
            return build_error_summary(args, &reason)
        }
        Ok((Ok(player), artwork, Ok(messages))) => (player, artwork, messages),
    };
    let mut summary = base_summary(args, "ok", None);
    summary["server"] = messages.server.unwrap_or(serde_json::Value::Null);
    if is_metadata_scenario(&args.scenario_id) {
        summary["metadata"] = serde_json::json!({
            "update_count": messages.metadata_update_count,
            "received": messages.metadata,
        });
    } else if is_controller_scenario(&args.scenario_id) {
        summary["controller"] = serde_json::json!({
            "received_state": messages.controller_state,
            "sent_command": messages.sent_command,
        });
    } else if is_artwork_scenario(&args.scenario_id) {
        summary["stream"] = artwork.stream.unwrap_or(serde_json::Value::Null);
        summary["artwork"] = serde_json::json!({
            "channel": artwork.channel,
            "received_count": artwork.count,
            "received_sha256": (artwork.count > 0).then(|| hex_lower(&artwork.bytes.finalize())),
            "byte_count": artwork.byte_count,
        });
    } else {
        if player.chunk_count == 0 {
            summary = base_summary(
                args,
                "error",
                Some("Connection closed before any audio chunks were received"),
            );
        }
        summary["stream"] = player
            .stream
            .map(|stream| {
                serde_json::json!({
                    "codec": stream.codec,
                    "sample_rate": stream.sample_rate,
                    "channels": stream.channels,
                    "bit_depth": stream.bit_depth,
                    "codec_header": stream.codec_header,
                })
            })
            .unwrap_or(serde_json::Value::Null);
        summary["audio"] = serde_json::json!({
            "audio_chunk_count": player.chunk_count,
            "received_encoded_sha256": (player.chunk_count > 0)
                .then(|| hex_lower(&player.encoded.finalize())),
            "received_pcm_sha256": (player.pcm.sample_count > 0).then(|| player.pcm.hexdigest()),
            "received_sample_count": player.pcm.sample_count,
        });
    }
    summary
}

fn base_summary(args: &Args, status: &str, reason: Option<&str>) -> serde_json::Value {
    serde_json::json!({
        "status": status,
        "reason": reason,
        "implementation": "sendspin-rs",
        "role": "client",
        "scenario_id": args.scenario_id,
        "initiator_role": args.initiator_role,
        "preferred_codec": args.preferred_codec,
        "client_name": args.client_name,
        "client_id": args.client_id,
    })
}

fn build_error_summary(args: &Args, reason: &str) -> serde_json::Value {
    base_summary(args, "error", Some(reason))
}

async fn connect_or_accept(args: &Args) -> Result<ProtocolClient, String> {
    let builder = build_protocol_client_builder(args);
    let timeout = Duration::from_secs_f64(args.timeout_seconds);
    if args.initiator_role == "client" {
        write_json(&args.ready, &build_ready(args, None))?;
        let url = wait_for_server_url(&args.registry, &args.server_name, args.timeout_seconds)
            .await?;
        return builder
            .connect(&url)
            .await
            .map_err(|err| format!("Failed to connect to {url}: {err}"));
    }
    let listener = builder
        .listen(&format!("127.0.0.1:{}", args.port))
        .await
        .map_err(|err| err.to_string())?
        .path(args.path.clone());
    let url = format!("ws://127.0.0.1:{}{}", args.port, args.path);
    register_endpoint(&args.registry, &args.client_name, &url)?;
    write_json(&args.ready, &build_ready(args, Some(&url)))?;
    match tokio::time::timeout(timeout, listener.accept()).await {
        Err(_) => Err("Timed out waiting for server connection".to_string()),
        Ok(Err(err)) => Err(err.to_string()),
        Ok(Ok((client, _))) => Ok(client),
    }
}

async fn run(args: Args) -> Result<(), String> {
    let summary = if is_chunk_framing_scenario(&args.scenario_id) {
        write_json(&args.ready, &serde_json::json!({"status": "error"}))?;
        build_error_summary(
            &args,
            "Harness gap, not a protocol result: the sendspin-rs library does not expose raw audio frames",
        )
    } else {
        match connect_or_accept(&args).await {
            Ok(client) => run_session(&args, client).await,
            Err(reason) => build_error_summary(&args, &reason),
        }
    };

    write_json(&args.summary, &summary)?;
    print!("{summary}");

    if summary.get("status").and_then(|value| value.as_str()) == Some("ok") {
        Ok(())
    } else {
        Err(summary
            .get("reason")
            .and_then(|value| value.as_str())
            .unwrap_or("adapter failed")
            .to_string())
    }
}

#[tokio::main]
async fn main() {
    let args = Args::parse();
    if let Err(reason) = run(args.clone()).await {
        eprintln!("{reason}");
        std::process::exit(1);
    }
}
