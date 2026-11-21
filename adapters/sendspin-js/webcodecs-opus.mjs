// Node has no WebCodecs, and SendspinCore only advertises and decodes Opus
// where the platform provides AudioDecoder. This module supplies the part of
// that browser API the SDK uses, backed by libopus compiled to WebAssembly.
import { OpusDecoder } from "opus-decoder";

class EncodedAudioChunk {
  constructor({ type, timestamp, data }) {
    this.type = type;
    this.timestamp = timestamp;
    this.data = new Uint8Array(data);
    this.byteLength = this.data.byteLength;
  }
}

class AudioData {
  constructor(timestamp, channelData) {
    this.format = "f32-planar";
    this.timestamp = timestamp;
    this.numberOfChannels = channelData.length;
    this.numberOfFrames = channelData[0]?.length ?? 0;
    this.channelData = channelData;
  }

  copyTo(destination, { planeIndex }) {
    destination.set(this.channelData[planeIndex]);
  }

  close() {
    this.channelData = [];
  }
}

/**
 * Install AudioDecoder and EncodedAudioChunk on globalThis.
 *
 * `onEncodedChunk` receives the bytes of every chunk the SDK submits for
 * decoding, which is the only place the encoded payload is observable.
 */
export function installOpusWebCodecs(onEncodedChunk) {
  class AudioDecoder {
    static async isConfigSupported(config) {
      return { supported: config.codec === "opus", config };
    }

    constructor({ output, error }) {
      this.output = output;
      this.error = error;
      this.state = "unconfigured";
      this.decoder = null;
      // WebCodecs decodes in submission order, so chunks submitted while the
      // WebAssembly module loads wait on the same chain.
      this.pending = Promise.resolve();
    }

    configure({ sampleRate, numberOfChannels }) {
      this.decoder?.free();
      const decoder = new OpusDecoder({ sampleRate, channels: numberOfChannels });
      this.decoder = decoder;
      this.state = "configured";
      this.pending = this.pending.then(() => decoder.ready);
    }

    decode(chunk) {
      onEncodedChunk(chunk.data);
      const decoder = this.decoder;
      this.pending = this.pending.then(() => {
        try {
          const { channelData } = decoder.decodeFrame(chunk.data);
          this.output(new AudioData(chunk.timestamp, channelData));
        } catch (err) {
          this.error(err);
        }
      });
    }

    close() {
      this.decoder?.free();
      this.decoder = null;
      this.state = "closed";
    }
  }

  globalThis.AudioDecoder = AudioDecoder;
  globalThis.EncodedAudioChunk = EncodedAudioChunk;
}
