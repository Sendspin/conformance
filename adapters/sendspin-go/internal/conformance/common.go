package conformance

import (
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"sync"
	"time"
)

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
