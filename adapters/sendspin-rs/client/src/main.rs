use clap::Parser;
use futures_util::{SinkExt, StreamExt};
use sendspin::protocol::client::{ArtworkChunk, AudioChunk, BinaryFrame};
use sendspin::protocol::messages::{
    ArtworkChannel, ArtworkSource, ArtworkV1Support, AudioFormatSpec, ClientCommand, ClientHello,
    ClientState, ClientSyncState, ClientTime, ConnectionReason, ControllerCommand,
    ControllerCommandType, ControllerState, DeviceInfo, ImageFormat, Message, MetadataState,
    PlayerState, PlayerV1Support, ServerHello, StreamPlayerConfig,
};
use sendspin::ProtocolClientBuilder;
use sha2::{Digest, Sha256};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};
use tokio::io::{AsyncRead, AsyncWrite};
use tokio::net::TcpListener;
use tokio::time::sleep;
use tokio_tungstenite::tungstenite::Message as WsMessage;
use tokio_tungstenite::{accept_async, WebSocketStream};

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

fn current_micros() -> i64 {
    static PROCESS_START: std::sync::OnceLock<Instant> = std::sync::OnceLock::new();
    let start = PROCESS_START.get_or_init(Instant::now);
    start.elapsed().as_micros() as i64
}

fn is_player_scenario(scenario_id: &str) -> bool {
    matches!(
        scenario_id,
        "client-initiated-pcm"
            | "server-initiated-pcm"
            | "server-initiated-pcm-24bit"
            | "server-initiated-flac"
    )
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

fn build_client_hello(args: &Args) -> ClientHello {
    let (supported_roles, player_v1_support, artwork_v1_support) =
        if is_metadata_scenario(&args.scenario_id) {
            (vec!["metadata@v1".to_string()], None, None)
        } else if is_controller_scenario(&args.scenario_id) {
            (vec!["controller@v1".to_string()], None, None)
        } else if is_artwork_scenario(&args.scenario_id) {
            (
                vec!["artwork@v1".to_string()],
                None,
                Some(ArtworkV1Support {
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
                }),
            )
        } else {
            (
                vec!["player@v1".to_string()],
                Some(PlayerV1Support {
                    supported_formats: if args.preferred_codec == "pcm" {
                        let bit_depth = if args.scenario_id == "server-initiated-pcm-24bit" {
                            24
                        } else {
                            16
                        };
                        vec![AudioFormatSpec {
                            codec: "pcm".to_string(),
                            channels: 1,
                            sample_rate: 8000,
                            bit_depth,
                        }]
                    } else {
                        vec![
                            AudioFormatSpec {
                                codec: "flac".to_string(),
                                channels: 1,
                                sample_rate: 8000,
                                bit_depth: 16,
                            },
                            AudioFormatSpec {
                                codec: "pcm".to_string(),
                                channels: 1,
                                sample_rate: 8000,
                                bit_depth: 16,
                            },
                        ]
                    },
                    buffer_capacity: 2_000_000,
                    supported_commands: vec!["volume".to_string(), "mute".to_string()],
                }),
                None,
            )
        };

    ClientHello {
        client_id: args.client_id.clone(),
        name: args.client_name.clone(),
        version: 1,
        supported_roles,
        device_info: Some(DeviceInfo {
            product_name: Some("sendspin-rs Conformance Client".to_string()),
            manufacturer: Some("Sendspin Conformance".to_string()),
            software_version: Some("0.1.0".to_string()),
            mac_address: None,
        }),
        player_v1_support,
        artwork_v1_support,
        visualizer_v1_support: None,
    }
}

fn build_protocol_client_builder(args: &Args) -> ProtocolClientBuilder {
    ProtocolClientBuilder::builder()
        .client_id(args.client_id.clone())
        .name(args.client_name.clone())
        .product_name(Some("sendspin-rs Conformance Client".to_string()))
        .manufacturer(Some("Sendspin Conformance".to_string()))
        .software_version(Some("0.1.0".to_string()))
        .player_v1_support(PlayerV1Support {
            supported_formats: vec![AudioFormatSpec {
                codec: "pcm".to_string(),
                channels: 1,
                sample_rate: 8000,
                bit_depth: 16,
            }],
            buffer_capacity: 2_000_000,
            supported_commands: vec!["volume".to_string(), "mute".to_string()],
        })
        .build()
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

fn build_summary(
    args: &Args,
    status: &str,
    reason: Option<&str>,
    peer_hello: Option<serde_json::Value>,
    server_hello: Option<&ServerHello>,
    current_stream: Option<&StreamPlayerConfig>,
    audio_chunk_count: usize,
    received_encoded_sha256: Option<String>,
    received_pcm_sha256: Option<String>,
    received_sample_count: usize,
    metadata_update_count: usize,
    received_metadata: Option<serde_json::Value>,
    received_controller_state: Option<serde_json::Value>,
    sent_controller_command: Option<serde_json::Value>,
    artwork_stream: Option<serde_json::Value>,
    artwork_channel: Option<u8>,
    artwork_count: usize,
    artwork_sha256: Option<String>,
    artwork_byte_count: usize,
) -> serde_json::Value {
    let mut summary = serde_json::json!({
        "status": status,
        "reason": reason,
        "implementation": "sendspin-rs",
        "role": "client",
        "scenario_id": args.scenario_id,
        "initiator_role": args.initiator_role,
        "preferred_codec": args.preferred_codec,
        "client_name": args.client_name,
        "client_id": args.client_id,
        "peer_hello": peer_hello,
        "server": server_hello.map(|hello| {
            serde_json::json!({
                "server_id": hello.server_id,
                "name": hello.name,
                "version": hello.version,
                "active_roles": hello.active_roles,
                "connection_reason": hello.connection_reason,
            })
        }).or_else(|| peer_hello.as_ref().and_then(|hello| hello.get("payload")).cloned()),
    });

    if is_metadata_scenario(&args.scenario_id) {
        summary["metadata"] = serde_json::json!({
            "update_count": metadata_update_count,
            "received": received_metadata,
        });
    } else if is_controller_scenario(&args.scenario_id) {
        summary["controller"] = serde_json::json!({
            "received_state": received_controller_state,
            "sent_command": sent_controller_command,
        });
    } else if is_artwork_scenario(&args.scenario_id) {
        summary["stream"] = artwork_stream.unwrap_or(serde_json::Value::Null);
        summary["artwork"] = serde_json::json!({
            "channel": artwork_channel,
            "received_count": artwork_count,
            "received_sha256": artwork_sha256,
            "byte_count": artwork_byte_count,
        });
    } else {
        summary["stream"] = current_stream
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
            "audio_chunk_count": audio_chunk_count,
            "received_encoded_sha256": received_encoded_sha256,
            "received_pcm_sha256": received_pcm_sha256,
            "received_sample_count": received_sample_count,
        });
    }

    summary
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

fn build_error_summary(args: &Args, reason: &str) -> serde_json::Value {
    build_summary(
        args,
        "error",
        Some(reason),
        None,
        None,
        None,
        0,
        None,
        None,
        0,
        0,
        None,
        None,
        None,
        None,
        None,
        0,
        None,
        0,
    )
}

async fn run_connected_session<S>(args: &Args, ws_stream: WebSocketStream<S>) -> serde_json::Value
where
    S: AsyncRead + AsyncWrite + Unpin,
{
    let (mut write, mut read) = ws_stream.split();

    let hello = Message::ClientHello(build_client_hello(args));
    let hello_json = match serde_json::to_string(&hello) {
        Ok(value) => value,
        Err(err) => return build_error_summary(args, &err.to_string()),
    };
    if let Err(err) = write.send(WsMessage::Text(hello_json.into())).await {
        return build_error_summary(args, &err.to_string());
    }

    let mut peer_hello: Option<serde_json::Value> = None;
    let mut server_hello_payload: Option<ServerHello> = None;
    let mut current_stream: Option<StreamPlayerConfig> = None;
    let mut received_hasher = FloatPcmHasher::default();
    let mut encoded_hasher = Sha256::new();
    let mut audio_chunk_count = 0usize;
    let mut metadata_update_count = 0usize;
    let mut received_metadata: Option<serde_json::Value> = None;
    let mut received_controller_state: Option<serde_json::Value> = None;
    let mut sent_controller_command: Option<serde_json::Value> = None;
    let mut artwork_stream: Option<serde_json::Value> = None;
    let mut artwork_channel: Option<u8> = None;
    let mut artwork_count = 0usize;
    let mut artwork_hasher = Sha256::new();
    let mut artwork_byte_count = 0usize;
    let timeout = Duration::from_secs_f64(args.timeout_seconds);

    let read_result = tokio::time::timeout(timeout, async {
        loop {
            let Some(frame) = read.next().await else {
                break;
            };

            match frame.map_err(|err| err.to_string())? {
                WsMessage::Text(text) => {
                    let raw_value = serde_json::from_str::<serde_json::Value>(&text)
                        .map_err(|err| err.to_string())?;
                    let message_type = raw_value
                        .get("type")
                        .and_then(|value| value.as_str())
                        .unwrap_or_default();
                    if is_artwork_scenario(&args.scenario_id) && message_type == "stream/start" {
                        artwork_stream = raw_value
                            .get("payload")
                            .and_then(|payload| payload.get("artwork"))
                            .cloned();
                        continue;
                    }
                    if message_type == "stream/end" {
                        continue;
                    }
                    let message =
                        serde_json::from_str::<Message>(&text).map_err(|err| err.to_string())?;
                    match message {
                        Message::ServerHello(server_hello) => {
                            peer_hello = Some(raw_value);
                            server_hello_payload = Some(server_hello);
                            if is_player_scenario(&args.scenario_id) {
                                let state = Message::ClientState(ClientState {
                                    state: Some(ClientSyncState::Synchronized),
                                    player: Some(PlayerState {
                                        volume: Some(100),
                                        muted: Some(false),
                                        static_delay_ms: None,
                                        min_buffer_ms: None,
                                        required_lead_time_ms: None,
                                        supported_commands: None,
                                    }),
                                });
                                let state_json =
                                    serde_json::to_string(&state).map_err(|err| err.to_string())?;
                                write
                                    .send(WsMessage::Text(state_json.into()))
                                    .await
                                    .map_err(|err| err.to_string())?;
                                let time_sync = Message::ClientTime(ClientTime {
                                    client_transmitted: current_micros(),
                                });
                                let time_json = serde_json::to_string(&time_sync)
                                    .map_err(|err| err.to_string())?;
                                write
                                    .send(WsMessage::Text(time_json.into()))
                                    .await
                                    .map_err(|err| err.to_string())?;
                            }
                        }
                        Message::ServerState(server_state) => {
                            if let Some(metadata) = server_state.metadata.as_ref() {
                                metadata_update_count += 1;
                                received_metadata = Some(normalize_metadata(metadata));
                            }
                            if let Some(controller) = server_state.controller.as_ref() {
                                received_controller_state = Some(normalize_controller(controller));
                                if is_controller_scenario(&args.scenario_id)
                                    && sent_controller_command.is_none()
                                    && controller
                                        .supported_commands
                                        .contains(&args.controller_command)
                                {
                                    let command_type: ControllerCommandType =
                                        serde_json::from_value(serde_json::Value::String(
                                            args.controller_command.clone(),
                                        ))
                                        .map_err(|err| err.to_string())?;
                                    let command = Message::ClientCommand(ClientCommand {
                                        controller: Some(ControllerCommand {
                                            command: command_type,
                                            volume: None,
                                            mute: None,
                                        }),
                                    });
                                    let command_json = serde_json::to_string(&command)
                                        .map_err(|err| err.to_string())?;
                                    write
                                        .send(WsMessage::Text(command_json.into()))
                                        .await
                                        .map_err(|err| err.to_string())?;
                                    sent_controller_command = Some(serde_json::json!({
                                        "command": args.controller_command,
                                    }));
                                }
                            }
                        }
                        Message::StreamStart(stream_start) => {
                            current_stream = stream_start.player;
                        }
                        Message::ServerTime(_)
                        | Message::GroupUpdate(_)
                        | Message::ServerCommand(_)
                        | Message::StreamClear(_) => {}
                        other => {
                            return Err(format!("Unexpected server message: {other:?}"));
                        }
                    }
                }
                WsMessage::Binary(data) => {
                    match BinaryFrame::from_bytes(&data).map_err(|err| err.to_string())? {
                        BinaryFrame::Audio(AudioChunk { data, .. }) => {
                            if !is_player_scenario(&args.scenario_id) {
                                continue;
                            }
                            let stream = current_stream
                                .as_ref()
                                .ok_or_else(|| "Received audio before stream/start".to_string())?;
                            if stream.codec != "pcm" && stream.codec != "flac" {
                                return Err(format!(
                                    "Unsupported codec for current scenario: {}",
                                    stream.codec
                                ));
                            }
                            encoded_hasher.update(&*data);
                            if stream.codec == "pcm" {
                                received_hasher
                                    .update_from_pcm_bytes(&data, stream.bit_depth)
                                    .map_err(|err| err.to_string())?;
                            }
                            audio_chunk_count += 1;
                        }
                        BinaryFrame::Artwork(ArtworkChunk { channel, data, .. }) => {
                            if !is_artwork_scenario(&args.scenario_id) {
                                continue;
                            }
                            artwork_channel = Some(channel);
                            artwork_count += 1;
                            artwork_byte_count += data.len();
                            artwork_hasher.update(&*data);
                        }
                        BinaryFrame::Visualizer(_) | BinaryFrame::Unknown { .. } => {}
                    }
                }
                WsMessage::Ping(payload) => {
                    write
                        .send(WsMessage::Pong(payload))
                        .await
                        .map_err(|err| err.to_string())?;
                }
                WsMessage::Close(_) => break,
                _ => {}
            }
        }
        Ok::<(), String>(())
    })
    .await;

    let received_encoded_sha256 = if audio_chunk_count > 0 {
        Some(hex_lower(&encoded_hasher.clone().finalize()))
    } else {
        None
    };
    let received_pcm_sha256 = if received_hasher.sample_count > 0 {
        Some(received_hasher.hexdigest())
    } else {
        None
    };
    let artwork_sha256 = if artwork_count > 0 {
        Some(hex_lower(&artwork_hasher.clone().finalize()))
    } else {
        None
    };

    match read_result {
        Err(_) => build_summary(
            args,
            "error",
            Some("Timed out waiting for server disconnect"),
            peer_hello,
            server_hello_payload.as_ref(),
            current_stream.as_ref(),
            audio_chunk_count,
            received_encoded_sha256,
            received_pcm_sha256,
            received_hasher.sample_count,
            metadata_update_count,
            received_metadata,
            received_controller_state,
            sent_controller_command,
            artwork_stream,
            artwork_channel,
            artwork_count,
            artwork_sha256,
            artwork_byte_count,
        ),
        Ok(Err(reason)) => build_summary(
            args,
            "error",
            Some(&reason),
            peer_hello,
            server_hello_payload.as_ref(),
            current_stream.as_ref(),
            audio_chunk_count,
            received_encoded_sha256,
            received_pcm_sha256,
            received_hasher.sample_count,
            metadata_update_count,
            received_metadata,
            received_controller_state,
            sent_controller_command,
            artwork_stream,
            artwork_channel,
            artwork_count,
            artwork_sha256,
            artwork_byte_count,
        ),
        Ok(Ok(())) if peer_hello.is_none() => build_summary(
            args,
            "error",
            Some("Connection closed before handshake completed"),
            peer_hello,
            server_hello_payload.as_ref(),
            current_stream.as_ref(),
            audio_chunk_count,
            received_encoded_sha256,
            received_pcm_sha256,
            received_hasher.sample_count,
            metadata_update_count,
            received_metadata,
            received_controller_state,
            sent_controller_command,
            artwork_stream,
            artwork_channel,
            artwork_count,
            artwork_sha256,
            artwork_byte_count,
        ),
        Ok(Ok(())) => build_summary(
            args,
            "ok",
            None,
            peer_hello,
            server_hello_payload.as_ref(),
            current_stream.as_ref(),
            audio_chunk_count,
            received_encoded_sha256,
            received_pcm_sha256,
            received_hasher.sample_count,
            metadata_update_count,
            received_metadata,
            received_controller_state,
            sent_controller_command,
            artwork_stream,
            artwork_channel,
            artwork_count,
            artwork_sha256,
            artwork_byte_count,
        ),
    }
}

async fn run_outbound_protocol_client(args: &Args, server_url: &str) -> serde_json::Value {
    let builder = build_protocol_client_builder(args);
    let client = match builder.connect(server_url).await {
        Ok(client) => client,
        Err(err) => {
            return build_error_summary(args, &format!("Failed to connect to {server_url}: {err}"));
        }
    };

    let conn = client.split();
    let mut message_rx = conn.messages;
    let mut audio_rx = conn.audio;
    let _artwork_rx = conn.artwork;
    let _visualizer_rx = conn.visualizer;
    let _clock_sync = conn.clock_sync;
    let ws_tx = conn.sender;
    let _guard = conn.guard;

    if let Err(err) = ws_tx
        .send_message(Message::ClientState(ClientState {
            state: Some(ClientSyncState::Synchronized),
            player: Some(PlayerState {
                volume: Some(100),
                muted: Some(false),
                static_delay_ms: None,
                min_buffer_ms: None,
                required_lead_time_ms: None,
                supported_commands: None,
            }),
        }))
        .await
    {
        return build_error_summary(args, &err.to_string());
    }

    let mut current_stream: Option<StreamPlayerConfig> = None;
    let mut received_hasher = FloatPcmHasher::default();
    let mut encoded_hasher = Sha256::new();
    let mut audio_chunk_count = 0usize;
    // The SDK forwards stream/start (text) and audio (binary) on separate
    // channels, and tokio::select! randomizes branch order, so audio can be
    // polled before an already-buffered stream/start. Buffer early audio and
    // flush it once stream/start has been observed.
    let mut pending_audio: Vec<AudioChunk> = Vec::new();
    let timeout = Duration::from_secs_f64(args.timeout_seconds);

    let read_result = tokio::time::timeout(timeout, async {
        loop {
            tokio::select! {
                maybe_message = message_rx.recv() => {
                    let Some(message) = maybe_message else {
                        break;
                    };
                    match message {
                        Message::StreamStart(stream_start) => {
                            current_stream = stream_start.player;
                            if let Some(stream) = current_stream.as_ref() {
                                if stream.codec != "pcm" {
                                    return Err(format!(
                                        "Unsupported codec for current scenario: {}",
                                        stream.codec
                                    ));
                                }
                                for chunk in pending_audio.drain(..) {
                                    encoded_hasher.update(&chunk.data);
                                    received_hasher
                                        .update_from_pcm_bytes(&chunk.data, stream.bit_depth)
                                        .map_err(|err| err.to_string())?;
                                    audio_chunk_count += 1;
                                }
                            }
                        }
                        Message::StreamEnd(_)
                        | Message::StreamClear(_)
                        | Message::ServerState(_)
                        | Message::GroupUpdate(_)
                        | Message::ServerCommand(_) => {}
                        other => {
                            return Err(format!("Unexpected server message: {other:?}"));
                        }
                    }
                }
                maybe_audio = audio_rx.recv() => {
                    let Some(chunk) = maybe_audio else {
                        break;
                    };
                    let Some(stream) = current_stream.as_ref() else {
                        pending_audio.push(chunk);
                        continue;
                    };
                    if stream.codec != "pcm" {
                        return Err(format!("Unsupported codec for current scenario: {}", stream.codec));
                    }
                    encoded_hasher.update(&*chunk.data);
                    received_hasher
                        .update_from_pcm_bytes(&chunk.data, stream.bit_depth)
                        .map_err(|err| err.to_string())?;
                    audio_chunk_count += 1;
                }
            }
        }
        Ok::<(), String>(())
    })
    .await;

    let peer = ServerHello {
        server_id: args.server_id.clone(),
        name: args.server_name.clone(),
        version: 1,
        active_roles: vec!["player@v1".to_string()],
        connection_reason: ConnectionReason::Playback,
    };

    let received_encoded_sha256 = if audio_chunk_count > 0 {
        Some(hex_lower(&encoded_hasher.clone().finalize()))
    } else {
        None
    };
    let received_pcm_sha256 = if received_hasher.sample_count > 0 {
        Some(received_hasher.hexdigest())
    } else {
        None
    };

    match read_result {
        Err(_) => build_summary(
            args,
            "error",
            Some("Timed out waiting for server disconnect"),
            None,
            Some(&peer),
            current_stream.as_ref(),
            audio_chunk_count,
            received_encoded_sha256,
            received_pcm_sha256,
            received_hasher.sample_count,
            0,
            None,
            None,
            None,
            None,
            None,
            0,
            None,
            0,
        ),
        Ok(Err(reason)) => build_summary(
            args,
            "error",
            Some(&reason),
            None,
            Some(&peer),
            current_stream.as_ref(),
            audio_chunk_count,
            received_encoded_sha256,
            received_pcm_sha256,
            received_hasher.sample_count,
            0,
            None,
            None,
            None,
            None,
            None,
            0,
            None,
            0,
        ),
        Ok(Ok(())) if audio_chunk_count == 0 => build_summary(
            args,
            "error",
            Some("Connection closed before any audio chunks were received"),
            None,
            Some(&peer),
            current_stream.as_ref(),
            audio_chunk_count,
            received_encoded_sha256,
            received_pcm_sha256,
            received_hasher.sample_count,
            0,
            None,
            None,
            None,
            None,
            None,
            0,
            None,
            0,
        ),
        Ok(Ok(())) => build_summary(
            args,
            "ok",
            None,
            None,
            Some(&peer),
            current_stream.as_ref(),
            audio_chunk_count,
            received_encoded_sha256,
            received_pcm_sha256,
            received_hasher.sample_count,
            0,
            None,
            None,
            None,
            None,
            None,
            0,
            None,
            0,
        ),
    }
}

async fn run(args: Args) -> Result<(), String> {
    let summary = if args.initiator_role == "client" {
        write_json(&args.ready, &build_ready(&args, None))?;
        match wait_for_server_url(&args.registry, &args.server_name, args.timeout_seconds).await {
            Ok(server_url) => run_outbound_protocol_client(&args, &server_url).await,
            Err(reason) => build_error_summary(&args, &reason),
        }
    } else {
        let bind_addr = format!("127.0.0.1:{}", args.port);
        let url = format!("ws://127.0.0.1:{}{}", args.port, args.path);
        match TcpListener::bind(&bind_addr).await {
            Ok(listener) => {
                register_endpoint(&args.registry, &args.client_name, &url)?;
                write_json(&args.ready, &build_ready(&args, Some(&url)))?;
                match tokio::time::timeout(
                    Duration::from_secs_f64(args.timeout_seconds),
                    listener.accept(),
                )
                .await
                {
                    Err(_) => build_error_summary(&args, "Timed out waiting for server connection"),
                    Ok(Err(err)) => build_error_summary(&args, &err.to_string()),
                    Ok(Ok((stream, _))) => match accept_async(stream).await {
                        Ok(ws_stream) => run_connected_session(&args, ws_stream).await,
                        Err(err) => build_error_summary(&args, &err.to_string()),
                    },
                }
            }
            Err(err) => build_error_summary(&args, &err.to_string()),
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
