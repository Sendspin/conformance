// Conformance server adapter for sendspin-rs: drives the crate's server role
// (ServerListener / dial_client + Group) through the PCM player scenarios.
//
// Scope: player role, PCM only (16- or 24-bit), both connection directions.
// FLAC/OPUS/metadata/controller/artwork/request-format are not implemented by
// the sendspin-rs server, so implementations.py filters those rows out.

use clap::Parser;
use sendspin::protocol::messages::{ClientHello, StreamPlayerConfig};
use sendspin::server::{dial_client, Group};
use sendspin::{DefaultClock, ServerConnection, ServerListener};
use sha2::{Digest, Sha256};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant};

#[derive(Parser, Debug, Clone)]
struct Args {
    #[arg(long)]
    summary: PathBuf,
    #[arg(long)]
    ready: PathBuf,
    #[arg(long)]
    registry: PathBuf,
    #[arg(long)]
    fixture: PathBuf,
    #[arg(long)]
    client_name: String,
    #[arg(long, default_value = "client-initiated-pcm")]
    scenario_id: String,
    #[arg(long, default_value = "client")]
    initiator_role: String,
    #[arg(long, default_value = "pcm")]
    preferred_codec: String,
    #[arg(long, default_value = "conformance-server")]
    server_id: String,
    #[arg(long, default_value = "Sendspin Conformance Server")]
    server_name: String,
    #[arg(long, default_value_t = 30.0)]
    timeout_seconds: f64,
    #[arg(long, default_value_t = 8927)]
    port: u16,
    #[arg(long, default_value = "/sendspin")]
    path: String,
    // Seconds of the fixture to stream. The client hashes whatever it receives,
    // so a short clip is enough to prove transport; kept small for speed.
    #[arg(long, default_value_t = 2.0)]
    clip_seconds: f64,
}

fn is_supported_scenario(scenario_id: &str) -> bool {
    matches!(
        scenario_id,
        "client-initiated-pcm" | "server-initiated-pcm" | "server-initiated-pcm-24bit"
    )
}

// Canonical float PCM hash — byte-for-byte identical to the client adapter's
// FloatPcmHasher and the harness's pcm.py, so server "sent" and client
// "received" hashes match for a lossless PCM transport.
#[derive(Default)]
struct FloatPcmHasher {
    hasher: Sha256,
}

impl FloatPcmHasher {
    fn update(&mut self, pcm_bytes: &[u8], bit_depth: u8) -> Result<(), String> {
        match bit_depth {
            16 => {
                for chunk in pcm_bytes.chunks_exact(2) {
                    let sample = i16::from_le_bytes([chunk[0], chunk[1]]) as f32 / 32768.0;
                    self.hasher.update(sample.to_le_bytes());
                }
                Ok(())
            }
            24 => {
                for chunk in pcm_bytes.chunks_exact(3) {
                    let mut value =
                        (chunk[0] as i32) | ((chunk[1] as i32) << 8) | ((chunk[2] as i32) << 16);
                    if value & 0x80_0000 != 0 {
                        value |= !0x00FF_FFFF;
                    }
                    let sample = value as f32 / 8_388_608.0;
                    self.hasher.update(sample.to_le_bytes());
                }
                Ok(())
            }
            32 => {
                for chunk in pcm_bytes.chunks_exact(4) {
                    let sample = i32::from_le_bytes([chunk[0], chunk[1], chunk[2], chunk[3]]) as f32
                        / 2_147_483_648.0;
                    self.hasher.update(sample.to_le_bytes());
                }
                Ok(())
            }
            other => Err(format!("unsupported PCM bit depth: {other}")),
        }
    }

    fn hexdigest(&self) -> String {
        hex_lower(&self.hasher.clone().finalize())
    }
}

fn hex_lower(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        out.push_str(&format!("{b:02x}"));
    }
    out
}

fn write_json(path: &Path, value: &serde_json::Value) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    let content = serde_json::to_string_pretty(value).map_err(|e| e.to_string())?;
    fs::write(path, format!("{content}\n")).map_err(|e| e.to_string())
}

fn register_endpoint(registry: &Path, name: &str, url: &str) -> Result<(), String> {
    let mut payload = if registry.exists() {
        serde_json::from_str::<serde_json::Value>(
            &fs::read_to_string(registry).map_err(|e| e.to_string())?,
        )
        .unwrap_or_else(|_| serde_json::json!({}))
    } else {
        serde_json::json!({})
    };
    payload[name] = serde_json::json!({ "url": url });
    write_json(registry, &payload)
}

async fn wait_for_endpoint(registry: &Path, name: &str, timeout_s: f64) -> Result<String, String> {
    let deadline = Instant::now() + Duration::from_secs_f64(timeout_s);
    while Instant::now() < deadline {
        if let Ok(content) = fs::read_to_string(registry) {
            if let Ok(value) = serde_json::from_str::<serde_json::Value>(&content) {
                if let Some(url) = value
                    .get(name)
                    .and_then(|e| e.get("url"))
                    .and_then(|u| u.as_str())
                {
                    return Ok(url.to_string());
                }
            }
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    Err(format!("timed out waiting for endpoint {name:?}"))
}

fn write_ready(args: &Args, url: Option<&str>) -> Result<(), String> {
    write_json(
        &args.ready,
        &serde_json::json!({
            "scenario_id": args.scenario_id,
            "initiator_role": args.initiator_role,
            "url": url,
        }),
    )
}

struct TargetPcm {
    bytes: Vec<u8>,
    sample_rate: u32,
    channels: u8,
    bit_depth: u8,
    frame_count: usize,
    source_pcm_sha256: String,
}

/// The PCM format the client advertised (first PCM entry in its hello), or
/// 8000/1/16 if it advertised none.
fn negotiate_format(hello: &ClientHello) -> (u32, u8, u8) {
    if let Some(support) = &hello.player_v1_support {
        for fmt in &support.supported_formats {
            if fmt.codec.eq_ignore_ascii_case("pcm") {
                return (fmt.sample_rate, fmt.channels, fmt.bit_depth);
            }
        }
    }
    (8000, 1, 16)
}

/// Decode the FLAC fixture and requantize to the negotiated PCM format. The
/// fixture is 8000 Hz mono; we don't resample or remix, so the client must
/// advertise a matching rate/channel count (all current scenarios do).
fn load_target_pcm(
    fixture: &Path,
    rate: u32,
    channels: u8,
    bit_depth: u8,
    clip_seconds: f64,
) -> Result<TargetPcm, String> {
    let mut reader = claxon::FlacReader::open(fixture).map_err(|e| e.to_string())?;
    let info = reader.streaminfo();
    let src_bits = info.bits_per_sample as i32;
    if info.sample_rate != rate || info.channels as u8 != channels {
        return Err(format!(
            "cannot serve {rate}Hz/{channels}ch from a {}Hz/{}ch fixture (no resample/remix)",
            info.sample_rate, info.channels
        ));
    }
    let max_samples = if clip_seconds > 0.0 {
        ((rate as f64 * clip_seconds).round() as usize) * channels as usize
    } else {
        usize::MAX
    };

    let bytes_per_sample = (bit_depth / 8) as usize;
    let mut bytes = Vec::new();
    let mut count = 0usize;
    for sample in reader.samples() {
        if count >= max_samples {
            break;
        }
        let s = sample.map_err(|e| e.to_string())?; // i32 in the source bit-depth range
        let target_bits = bit_depth as i32;
        let v = if target_bits >= src_bits {
            s << (target_bits - src_bits)
        } else {
            s >> (src_bits - target_bits)
        };
        match bit_depth {
            16 => bytes.extend_from_slice(&(v as i16).to_le_bytes()),
            24 => bytes.extend_from_slice(&[(v & 0xff) as u8, ((v >> 8) & 0xff) as u8, ((v >> 16) & 0xff) as u8]),
            32 => bytes.extend_from_slice(&v.to_le_bytes()),
            other => return Err(format!("unsupported target bit depth {other}")),
        }
        count += 1;
    }

    let mut hasher = FloatPcmHasher::default();
    hasher.update(&bytes, bit_depth)?;
    let frame_count = bytes.len() / (bytes_per_sample * channels as usize);
    Ok(TargetPcm {
        bytes,
        sample_rate: rate,
        channels,
        bit_depth,
        frame_count,
        source_pcm_sha256: hasher.hexdigest(),
    })
}

async fn stream_session(
    args: &Args,
    conn: ServerConnection,
    clock: Arc<DefaultClock>,
) -> Result<serde_json::Value, String> {
    let (rate, channels, bit_depth) = negotiate_format(conn.hello());
    let pcm = load_target_pcm(&args.fixture, rate, channels, bit_depth, args.clip_seconds)?;

    let client_id = conn.client_id().to_string();
    let client_name = conn.hello().name.clone();
    let supported_roles = conn.hello().supported_roles.clone();
    let sender = conn.sender();

    let group = Group::new(clock);
    group
        .add_member(client_id.clone(), sender)
        .await
        .map_err(|e| e.to_string())?;
    group
        .start_stream(StreamPlayerConfig {
            codec: "pcm".to_string(),
            sample_rate: pcm.sample_rate,
            channels: pcm.channels,
            bit_depth: pcm.bit_depth,
            codec_header: None,
        })
        .await;

    // Stream in ~50ms blocks, paced at real time so the per-connection audio
    // backlog stays well under the eviction bound (dropping frames would break
    // the hash comparison). Timing doesn't affect the hash — this just keeps
    // the queue shallow while exercising the real Group push path.
    let block_ms: u64 = 50;
    let block_bytes = ((pcm.sample_rate as usize * block_ms as usize / 1000)
        * pcm.channels as usize
        * (pcm.bit_depth / 8) as usize)
        .max((pcm.bit_depth / 8) as usize);
    let mut chunk_count = 0usize;
    for chunk in pcm.bytes.chunks(block_bytes) {
        group.push_audio(chunk);
        chunk_count += 1;
        tokio::time::sleep(Duration::from_millis(block_ms)).await;
    }
    group.end_stream().await;
    tokio::time::sleep(Duration::from_millis(200)).await; // let the tail flush
    let _ = conn.disconnect().await;

    Ok(serde_json::json!({
        "client": {
            "client_id": client_id,
            "name": client_name,
            "supported_roles": supported_roles,
        },
        "stream": {
            "codec": "pcm",
            "sample_rate": pcm.sample_rate,
            "channels": pcm.channels,
            "bit_depth": pcm.bit_depth,
        },
        "audio": {
            "fixture": args.fixture.to_string_lossy(),
            "source_pcm_sha256": pcm.source_pcm_sha256,
            "sent_audio_chunk_count": chunk_count,
            "sent_encoded_byte_count": pcm.bytes.len(),
            "clip_seconds": args.clip_seconds,
            "sample_rate": pcm.sample_rate,
            "channels": pcm.channels,
            "bit_depth": pcm.bit_depth,
            "frame_count": pcm.frame_count,
            "duration_seconds": pcm.frame_count as f64 / pcm.sample_rate.max(1) as f64,
        },
    }))
}

async fn connect(args: &Args, clock: Arc<DefaultClock>) -> Result<ServerConnection, String> {
    if args.initiator_role == "client" {
        // Client-initiated: we bind and advertise; the client connects to us.
        let addr = format!("127.0.0.1:{}", args.port);
        let listener = ServerListener::bind(&addr, &args.server_id, &args.server_name)
            .await
            .map_err(|e| e.to_string())?
            .path(&args.path);
        let local = listener.local_addr().map_err(|e| e.to_string())?;
        let url = format!("ws://127.0.0.1:{}{}", local.port(), args.path);
        register_endpoint(&args.registry, &args.server_name, &url)?;
        write_ready(args, Some(&url))?;
        let (conn, _addr) =
            tokio::time::timeout(Duration::from_secs_f64(args.timeout_seconds), listener.accept())
                .await
                .map_err(|_| "timed out waiting for the client to connect".to_string())?
                .map_err(|e| e.to_string())?;
        Ok(conn)
    } else {
        // Server-initiated: the client advertises a listener; we dial in.
        write_ready(args, None)?;
        let url = wait_for_endpoint(&args.registry, &args.client_name, args.timeout_seconds).await?;
        let conn = tokio::time::timeout(
            Duration::from_secs_f64(args.timeout_seconds),
            dial_client(&url, &args.server_id, &args.server_name, clock),
        )
        .await
        .map_err(|_| "timed out dialing the client".to_string())?
        .map_err(|e| e.to_string())?;
        Ok(conn)
    }
}

fn base_summary(args: &Args, status: &str, reason: Option<&str>) -> serde_json::Value {
    serde_json::json!({
        "status": status,
        "reason": reason,
        "implementation": "sendspin-rs",
        "role": "server",
        "scenario_id": args.scenario_id,
        "initiator_role": args.initiator_role,
        "preferred_codec": args.preferred_codec,
        "server_id": args.server_id,
        "server_name": args.server_name,
    })
}

fn finish(args: &Args, mut summary: serde_json::Value) -> i32 {
    let ok = summary.get("status").and_then(|s| s.as_str()) == Some("ok");
    if let Err(e) = write_json(&args.summary, &summary) {
        eprintln!("failed to write summary: {e}");
        return 1;
    }
    // Echo to stdout for the case log.
    if let Ok(text) = serde_json::to_string_pretty(&summary) {
        println!("{text}");
    }
    let _ = &mut summary;
    if ok {
        0
    } else {
        1
    }
}

#[tokio::main]
async fn main() {
    let args = Args::parse();

    if !is_supported_scenario(&args.scenario_id) {
        let reason = format!("sendspin-rs server does not support {}", args.scenario_id);
        std::process::exit(finish(&args, base_summary(&args, "error", Some(&reason))));
    }

    let clock: Arc<DefaultClock> = Arc::new(DefaultClock::default());
    let result = match connect(&args, clock.clone()).await {
        Ok(conn) => stream_session(&args, conn, clock).await,
        Err(e) => Err(e),
    };

    let summary = match result {
        Ok(session) => {
            let mut base = base_summary(&args, "ok", None);
            if let (Some(obj), Some(extra)) = (base.as_object_mut(), session.as_object()) {
                for (k, v) in extra {
                    obj.insert(k.clone(), v.clone());
                }
            }
            base
        }
        Err(e) => base_summary(&args, "error", Some(&e)),
    };
    std::process::exit(finish(&args, summary));
}
