package conformance

import (
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"os"
	"path/filepath"
	"sync"
	"time"

	"github.com/mewkiz/flac"
)

type Envelope struct {
	Type    string          `json:"type"`
	Payload json.RawMessage `json:"payload"`
}

type PCMBlock struct {
	Data       []byte
	DurationUS int64
}

type Fixture struct {
	Path             string
	SourceFlacSHA256 string
	SourcePcmSHA256  string
	SampleRate       int
	Channels         int
	BitDepth         int
	FrameCount       int
	DurationSeconds  float64
	PCMBytes         []byte
}

type FloatPcmHasher struct {
	mu          sync.Mutex
	hasher      hashState
	SampleCount int
}

type hashState struct {
	sum hashWriter
}

type hashWriter interface {
	Write([]byte) (int, error)
	Sum([]byte) []byte
}

var processStart = time.Now()

func NewFloatPcmHasher() *FloatPcmHasher {
	return &FloatPcmHasher{
		hasher: hashState{sum: sha256.New()},
	}
}

func CurrentMicros() int64 {
	return time.Since(processStart).Microseconds()
}

func HexLower(bytes []byte) string {
	return fmt.Sprintf("%x", bytes)
}

func SHA256Hex(data []byte) string {
	sum := sha256.Sum256(data)
	return HexLower(sum[:])
}

// framePrefixBytes spans the RC1 audio chunk header, so the prefix shows every
// header field. A frame with a shorter header yields that many bytes of audio
// past it; the rest of the payload is never recorded.
const framePrefixBytes = 13

// FrameRecord describes one binary frame as it was handed to the transport,
// beside the size of the audio payload the caller built it from.
func FrameRecord(frame []byte, payloadByteCount int) map[string]any {
	prefix := frame
	if len(prefix) > framePrefixBytes {
		prefix = prefix[:framePrefixBytes]
	}
	return map[string]any{
		"byte_count":         len(frame),
		"leading_hex":        HexLower(prefix),
		"payload_byte_count": payloadByteCount,
	}
}

func WriteJSON(path string, value any) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	payload, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(path, append(payload, '\n'), 0o644)
}

func PrintFile(path string) error {
	content, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	_, err = os.Stdout.Write(content)
	return err
}

func RegisterEndpoint(path string, name string, url string) error {
	payload := map[string]map[string]string{}
	if raw, err := os.ReadFile(path); err == nil {
		_ = json.Unmarshal(raw, &payload)
	}
	payload[name] = map[string]string{"url": url}
	return WriteJSON(path, payload)
}

func WaitForEndpoint(path string, name string, timeout time.Duration) (string, error) {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		raw, err := os.ReadFile(path)
		if err == nil {
			payload := map[string]map[string]string{}
			if json.Unmarshal(raw, &payload) == nil {
				if entry, ok := payload[name]; ok {
					if url, ok := entry["url"]; ok && url != "" {
						return url, nil
					}
				}
			}
		}
		time.Sleep(100 * time.Millisecond)
	}
	return "", fmt.Errorf("timed out waiting for %q in registry", name)
}

func (h *FloatPcmHasher) UpdateFromPCMBytes(pcmBytes []byte, bitDepth int) error {
	h.mu.Lock()
	defer h.mu.Unlock()

	switch bitDepth {
	case 16:
		for offset := 0; offset+1 < len(pcmBytes); offset += 2 {
			sample := int16(pcmBytes[offset]) | int16(pcmBytes[offset+1])<<8
			value := float32(sample) / 32768.0
			_, _ = h.hasher.sum.Write(float32LE(value))
			h.SampleCount++
		}
	case 24:
		for offset := 0; offset+2 < len(pcmBytes); offset += 3 {
			value := int32(pcmBytes[offset]) |
				int32(pcmBytes[offset+1])<<8 |
				int32(pcmBytes[offset+2])<<16
			if value&0x800000 != 0 {
				value |= ^0x00ffffff
			}
			sample := float32(value) / 8388608.0
			_, _ = h.hasher.sum.Write(float32LE(sample))
			h.SampleCount++
		}
	case 32:
		for offset := 0; offset+3 < len(pcmBytes); offset += 4 {
			value := int32(pcmBytes[offset]) |
				int32(pcmBytes[offset+1])<<8 |
				int32(pcmBytes[offset+2])<<16 |
				int32(pcmBytes[offset+3])<<24
			sample := float32(value) / 2147483648.0
			_, _ = h.hasher.sum.Write(float32LE(sample))
			h.SampleCount++
		}
	default:
		return fmt.Errorf("unsupported PCM bit depth: %d", bitDepth)
	}
	return nil
}

func (h *FloatPcmHasher) HexDigest() string {
	h.mu.Lock()
	defer h.mu.Unlock()
	return HexLower(h.hasher.sum.Sum(nil))
}

func float32LE(value float32) []byte {
	bits := math.Float32bits(value)
	return []byte{
		byte(bits),
		byte(bits >> 8),
		byte(bits >> 16),
		byte(bits >> 24),
	}
}

func PCMBlocks(pcmBytes []byte, sampleRate int, channels int, bitDepth int, blockMS int) []PCMBlock {
	bytesPerFrame := channels * (bitDepth / 8)
	framesPerBlock := int(math.Round(float64(sampleRate) * (float64(blockMS) / 1000.0)))
	if framesPerBlock < 1 {
		framesPerBlock = 1
	}
	bytesPerBlock := framesPerBlock * bytesPerFrame
	blocks := make([]PCMBlock, 0, (len(pcmBytes)+bytesPerBlock-1)/bytesPerBlock)
	for offset := 0; offset < len(pcmBytes); offset += bytesPerBlock {
		end := offset + bytesPerBlock
		if end > len(pcmBytes) {
			end = len(pcmBytes)
		}
		chunk := pcmBytes[offset:end]
		frameCount := len(chunk) / bytesPerFrame
		durationUS := int64(frameCount) * 1_000_000 / int64(sampleRate)
		blocks = append(blocks, PCMBlock{
			Data:       chunk,
			DurationUS: durationUS,
		})
	}
	return blocks
}

func EncodePCM24(samples []int32) []byte {
	output := make([]byte, len(samples)*3)
	for index, sample := range samples {
		output[index*3] = byte(sample)
		output[index*3+1] = byte(sample >> 8)
		output[index*3+2] = byte(sample >> 16)
	}
	return output
}

func DecodeFixture(path string, clipSeconds float64) (*Fixture, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("failed to read fixture: %w", err)
	}

	file, err := os.Open(path)
	if err != nil {
		return nil, fmt.Errorf("failed to open fixture: %w", err)
	}
	defer file.Close()

	stream, err := flac.New(file)
	if err != nil {
		return nil, fmt.Errorf("failed to decode FLAC fixture: %w", err)
	}

	info := stream.Info
	sampleRate := int(info.SampleRate)
	channels := int(info.NChannels)
	bitDepth := int(info.BitsPerSample)
	maxFrames := int(^uint(0) >> 1)
	if clipSeconds > 0 {
		maxFrames = int(math.Round(float64(sampleRate) * clipSeconds))
	}

	samples := make([]int32, 0, maxFrames*channels)
	framesRead := 0
	for framesRead < maxFrames {
		frame, err := stream.ParseNext()
		if err == io.EOF {
			break
		}
		if err != nil {
			return nil, fmt.Errorf("failed to parse FLAC frame: %w", err)
		}

		blockSize := int(frame.BlockSize)
		for frameIndex := 0; frameIndex < blockSize && framesRead < maxFrames; frameIndex++ {
			for channel := 0; channel < channels; channel++ {
				sample := frame.Subframes[channel].Samples[frameIndex]
				samples = append(samples, convertTo24Bit(sample, bitDepth))
			}
			framesRead++
		}
	}

	pcmBytes := EncodePCM24(samples)
	hasher := NewFloatPcmHasher()
	if err := hasher.UpdateFromPCMBytes(pcmBytes, 24); err != nil {
		return nil, err
	}

	return &Fixture{
		Path:             path,
		SourceFlacSHA256: SHA256Hex(raw),
		SourcePcmSHA256:  hasher.HexDigest(),
		SampleRate:       sampleRate,
		Channels:         channels,
		BitDepth:         24,
		FrameCount:       framesRead,
		DurationSeconds:  float64(framesRead) / float64(sampleRate),
		PCMBytes:         pcmBytes,
	}, nil
}

func convertTo24Bit(sample int32, bitDepth int) int32 {
	switch {
	case bitDepth == 24:
		return sample
	case bitDepth == 16:
		return sample << 8
	case bitDepth > 24:
		return sample >> (bitDepth - 24)
	default:
		return sample << (24 - bitDepth)
	}
}
