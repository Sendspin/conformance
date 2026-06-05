#!/usr/bin/env node
// Node.js conformance client adapter for sendspin-js.
//
// This adapter intentionally stays thin: it owns discovery (registry
// lookup for client-initiated, a WebSocket listener for server-initiated)
// and hands a live WebSocket to the published SendspinCore SDK. All
// protocol work — handshake, time sync, state merging, PCM decoding —
// is done by SendspinCore. The adapter reads back state through the
// SDK's public callbacks and sends controller commands through
// SendspinCore.sendCommand.

import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
import { register } from "node:module";
import { WebSocket as WsWebSocket, WebSocketServer } from "ws";

// SendspinCore uses the platform WebSocket class for outbound connections
// and references the constructor's readyState constants while adopting
// sockets. On Node we provide it via the `ws` package before importing
// the SDK module.
if (typeof globalThis.WebSocket === "undefined") {
  globalThis.WebSocket = WsWebSocket;
}

// The published sendspin-js dist/ output uses TypeScript's bundler-style
// extensionless imports. Register a small resolver hook before loading
// the SDK so Node can consume it without a separate bundling step.
register(new URL("./sdk-loader.mjs", import.meta.url));

const sdkModuleUrl = new URL(
  "../../repos/sendspin-js/dist/index.js",
  import.meta.url,
);
const { SendspinCore } = await import(sdkModuleUrl.href);

const IMPLEMENTATION = "sendspin-js";
const PLAYER_SCENARIOS = new Set([
  "client-initiated-pcm",
  "server-initiated-pcm",
]);
const METADATA_SCENARIOS = new Set(["server-initiated-metadata"]);
const CONTROLLER_SCENARIOS = new Set(["server-initiated-controller"]);

function parseArgs(argv) {
  const values = {};
  for (let index = 0; index < argv.length; index += 2) {
    const key = argv[index];
    const value = argv[index + 1];
    if (!key?.startsWith("--") || value === undefined) {
      throw new Error(`Invalid arguments near ${key ?? "<eof>"}`);
    }
    values[key.slice(2)] = value;
  }
  return values;
}

function writeJson(filePath, payload) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, `${JSON.stringify(payload, null, 2)}\n`, "utf8");
}

function readJson(filePath) {
  if (!fs.existsSync(filePath)) return null;
  try {
    return JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch {
    return null;
  }
}

function registerEndpoint(registryPath, clientName, url) {
  const existing = readJson(registryPath) ?? {};
  existing[clientName] = { url };
  writeJson(registryPath, existing);
}

async function waitForServerUrl(registryPath, serverName, timeoutSeconds) {
  const deadline = Date.now() + timeoutSeconds * 1000;
  while (Date.now() < deadline) {
    const payload = readJson(registryPath);
    const entry = payload?.[serverName];
    const url = entry && typeof entry === "object" ? entry.url : undefined;
    if (typeof url === "string" && url.length > 0) {
      return url;
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`Timed out waiting for server ${JSON.stringify(serverName)}`);
}

function scenarioGroup(scenarioId) {
  if (PLAYER_SCENARIOS.has(scenarioId)) return "player";
  if (METADATA_SCENARIOS.has(scenarioId)) return "metadata";
  if (CONTROLLER_SCENARIOS.has(scenarioId)) return "controller";
  return null;
}

// Strip `timestamp` before comparison: the harness's expected snapshot
// never contains it (see conformance issue #45). repeat/shuffle are
// controller-role state per the spec and must not appear in the metadata
// snapshot, so drop them too even if the SDK still surfaces them here.
function metadataForSummary(metadata) {
  if (!metadata) return null;
  const { timestamp: _timestamp, repeat: _repeat, shuffle: _shuffle, ...rest } = metadata;
  return rest;
}

function interleaveSamples(channelSamples) {
  const channels = channelSamples.length;
  if (channels === 0) return new Float32Array(0);
  if (channels === 1) return channelSamples[0];
  const frames = channelSamples[0].length;
  const interleaved = new Float32Array(frames * channels);
  for (let frame = 0; frame < frames; frame += 1) {
    for (let channel = 0; channel < channels; channel += 1) {
      interleaved[frame * channels + channel] = channelSamples[channel][frame];
    }
  }
  return interleaved;
}

class AdapterState {
  constructor() {
    this.peerHello = null;
    this.readyWritten = false;
    this.streamFormat = null;
    this.audioChunkCount = 0;
    this.pcmHasher = crypto.createHash("sha256");
    this.pcmSampleCount = 0;
    this.metadataUpdateCount = 0;
    this.lastMetadataFingerprint = null;
    this.receivedMetadata = null;
    this.receivedController = null;
    this.sentController = null;
  }
}

function observeServerHello(webSocket, state) {
  webSocket.addEventListener("message", (event) => {
    if (typeof event.data !== "string") return;
    let parsed;
    try {
      parsed = JSON.parse(event.data);
    } catch {
      return;
    }
    if (parsed?.type === "server/hello") {
      state.peerHello = parsed;
    }
  });
}

function buildCore({ args, webSocket, state }) {
  const group = scenarioGroup(args["scenario-id"]);
  const controllerCommand = args["controller-command"];
  // Forward reference: onStateChange fires during construction-adjacent
  // merges, but sendCommand lives on the instance we are about to build.
  const coreRef = { current: null };

  const core = new SendspinCore({
    playerId: args["client-id"],
    clientName: args["client-name"],
    codecs: ["pcm"],
    webSocket,
    onStateChange: ({ serverState }) => {
      if (serverState.metadata) {
        const received = metadataForSummary(serverState.metadata);
        const fingerprint = JSON.stringify(received);
        if (fingerprint !== state.lastMetadataFingerprint) {
          state.lastMetadataFingerprint = fingerprint;
          state.metadataUpdateCount += 1;
        }
        state.receivedMetadata = received;
      }
      if (serverState.controller) {
        state.receivedController = serverState.controller;
        if (
          group === "controller" &&
          state.sentController === null &&
          controllerCommand &&
          serverState.controller.supported_commands?.includes(controllerCommand)
        ) {
          try {
            coreRef.current?.sendCommand(controllerCommand, undefined);
            state.sentController = { command: controllerCommand };
          } catch {
            // Leave sent command unset; verification will flag the failure.
          }
        }
      }
    },
  });
  coreRef.current = core;

  core.onStreamStart = (format, isFormatUpdate) => {
    if (isFormatUpdate && state.streamFormat !== null) return;
    state.streamFormat = format;
  };

  core.onAudioData = (chunk) => {
    state.audioChunkCount += 1;
    const interleaved = interleaveSamples(chunk.samples);
    if (interleaved.length === 0) return;
    state.pcmHasher.update(Buffer.from(interleaved.buffer, interleaved.byteOffset, interleaved.byteLength));
    state.pcmSampleCount += interleaved.length;
  };

  return core;
}

function waitForClose(webSocket) {
  return new Promise((resolve) => {
    if (webSocket.readyState === webSocket.CLOSED) {
      resolve();
      return;
    }
    webSocket.addEventListener("close", () => resolve(), { once: true });
  });
}

function waitForConnection(wss, timeoutSeconds) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      wss.removeAllListeners("connection");
      reject(new Error("Timed out waiting for server connection"));
    }, timeoutSeconds * 1000);
    wss.once("connection", (ws) => {
      clearTimeout(timer);
      resolve(ws);
    });
  });
}

function buildBaseSummary(args) {
  return {
    implementation: IMPLEMENTATION,
    role: "client",
    scenario_id: args["scenario-id"] ?? null,
    initiator_role: args["initiator-role"] ?? null,
    preferred_codec: args["preferred-codec"] ?? null,
    client_id: args["client-id"] ?? null,
    client_name: args["client-name"] ?? null,
  };
}

function buildSuccessSummary(args, state) {
  const summary = {
    status: "ok",
    ...buildBaseSummary(args),
    peer_hello: state.peerHello,
    server: state.peerHello?.payload ?? null,
  };

  const group = scenarioGroup(args["scenario-id"]);
  if (group === "metadata") {
    summary.metadata = {
      update_count: state.metadataUpdateCount,
      received: state.receivedMetadata,
    };
  } else if (group === "controller") {
    summary.controller = {
      received_state: state.receivedController,
      sent_command: state.sentController,
    };
  } else if (group === "player") {
    summary.stream = state.streamFormat;
    summary.audio = {
      audio_chunk_count: state.audioChunkCount,
      received_encoded_sha256: null,
      received_pcm_sha256:
        state.pcmSampleCount > 0 ? state.pcmHasher.digest("hex") : null,
      received_sample_count: state.pcmSampleCount,
    };
  }
  return summary;
}

function buildErrorSummary(args, state, reason) {
  return {
    status: "error",
    reason,
    ...buildBaseSummary(args),
    peer_hello: state.peerHello,
    server: state.peerHello?.payload ?? null,
  };
}

function ensureReadyWritten(args, state, extras = {}) {
  if (state.readyWritten) return;
  writeJson(args.ready, {
    status: "ready",
    implementation: IMPLEMENTATION,
    scenario_id: args["scenario-id"] ?? null,
    initiator_role: args["initiator-role"] ?? null,
    ...extras,
  });
  state.readyWritten = true;
}

async function runClientInitiated(args, state, timeoutSeconds) {
  ensureReadyWritten(args, state);
  const serverUrl = await waitForServerUrl(
    args.registry,
    args["server-name"],
    timeoutSeconds,
  );
  const webSocket = new WsWebSocket(serverUrl);
  observeServerHello(webSocket, state);
  const core = buildCore({ args, webSocket, state });
  await core.connect();
  try {
    await waitForClose(webSocket);
  } finally {
    core.disconnect();
  }
}

async function runServerInitiated(args, state, timeoutSeconds) {
  const port = Number(args.port);
  const wsPath = args.path ?? "/sendspin";
  if (!Number.isFinite(port) || port <= 0) {
    throw new Error(`Invalid --port argument: ${args.port}`);
  }
  const url = `ws://127.0.0.1:${port}${wsPath}`;
  const wss = new WebSocketServer({ host: "127.0.0.1", port, path: wsPath });
  try {
    await new Promise((resolve, reject) => {
      wss.once("listening", resolve);
      wss.once("error", reject);
    });
    registerEndpoint(args.registry, args["client-name"], url);
    ensureReadyWritten(args, state, { url });
    const webSocket = await waitForConnection(wss, timeoutSeconds);
    observeServerHello(webSocket, state);
    const core = buildCore({ args, webSocket, state });
    await core.connect();
    try {
      await waitForClose(webSocket);
    } finally {
      core.disconnect();
    }
  } finally {
    await new Promise((resolve) => wss.close(() => resolve()));
  }
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const state = new AdapterState();
  const group = scenarioGroup(args["scenario-id"]);
  const timeoutSeconds = Number(args["timeout-seconds"] ?? "30");

  try {
    if (group === null) {
      throw new Error(
        `sendspin-js adapter does not support scenario ${args["scenario-id"]} through the public SDK.`,
      );
    }
    if (args["initiator-role"] === "client") {
      await runClientInitiated(args, state, timeoutSeconds);
    } else {
      await runServerInitiated(args, state, timeoutSeconds);
    }
    const summary = buildSuccessSummary(args, state);
    writeJson(args.summary, summary);
    process.stdout.write(`${JSON.stringify(summary, null, 2)}\n`);
    process.exit(0);
  } catch (err) {
    ensureReadyWritten(args, state);
    const summary = buildErrorSummary(
      args,
      state,
      err instanceof Error ? err.message : String(err),
    );
    writeJson(args.summary, summary);
    process.stdout.write(`${JSON.stringify(summary, null, 2)}\n`);
    process.exit(1);
  }
}

await main();
