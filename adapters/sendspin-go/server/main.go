package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"flag"
	"fmt"
	"image"
	"image/color"
	"image/draw"
	"image/jpeg"
	"image/png"
	"log"
	"net"
	"net/http"
	"os"
	"strings"
	"time"

	conformance "conformance-sendspin-go/internal/conformance"
	"github.com/Sendspin/sendspin-go/pkg/protocol"
	"github.com/gorilla/websocket"
)

const (
	defaultServerPath = "/sendspin"
)

type args struct {
	ClientName        string
	Summary           string
	Ready             string
	Registry          string
	Fixture           string
	ScenarioID        string
	InitiatorRole     string
	PreferredCodec    string
	TimeoutSeconds    float64
	Port              int
	Host              string
	ServerID          string
	ServerName        string
	ClipSeconds       float64
	MetadataTitle     string
	MetadataArtist    string
	MetadataAlbum     string
	MetadataAlbumArt  string
	MetadataURL       string
	MetadataYear      int
	MetadataTrack     int
	MetadataRepeat    string
	MetadataShuffle   string
	MetadataProgress  int
	MetadataDuration  int
	MetadataSpeed     int
	ControllerCommand string
	ArtworkFormat     string
	ArtworkWidth      int
	ArtworkHeight     int
}

type sessionResult struct {
	summary map[string]any
	err     error
}

type clientCommandPayload struct {
	Controller *controllerCommand `json:"controller,omitempty"`
}

type controllerCommand struct {
	Command string `json:"command"`
	Volume  *int   `json:"volume,omitempty"`
	Mute    *bool  `json:"mute,omitempty"`
}

type flacTransport struct {
	CodecHeader      []byte
	AudioBytes       []byte
	SampleRate       int
	Channels         int
	BitDepth         int
	FrameCount       int
	DurationSeconds  float64
	SourceFlacSHA256 string
	SourcePcmSHA256  string
}

func main() {
	parsed := parseArgs()
	os.Exit(run(parsed))
}

func parseArgs() args {
	var parsed args
	flag.StringVar(&parsed.ClientName, "client-name", "", "")
	flag.StringVar(&parsed.Summary, "summary", "", "")
	flag.StringVar(&parsed.Ready, "ready", "", "")
	flag.StringVar(&parsed.Registry, "registry", "", "")
	flag.StringVar(&parsed.Fixture, "fixture", "", "")
	flag.StringVar(&parsed.ScenarioID, "scenario-id", "client-initiated-pcm", "")
	flag.StringVar(&parsed.InitiatorRole, "initiator-role", "client", "")
	flag.StringVar(&parsed.PreferredCodec, "preferred-codec", "pcm", "")
	flag.Float64Var(&parsed.TimeoutSeconds, "timeout-seconds", 30.0, "")
	flag.IntVar(&parsed.Port, "port", 8927, "")
	flag.StringVar(&parsed.Host, "host", "127.0.0.1", "")
	flag.StringVar(&parsed.ServerID, "server-id", "conformance-server", "")
	flag.StringVar(&parsed.ServerName, "server-name", "Sendspin Conformance Server", "")
	flag.Float64Var(&parsed.ClipSeconds, "clip-seconds", 5.0, "")
	flag.StringVar(&parsed.MetadataTitle, "metadata-title", "Almost Silent", "")
	flag.StringVar(&parsed.MetadataArtist, "metadata-artist", "Sendspin Conformance", "")
	flag.StringVar(&parsed.MetadataAlbumArt, "metadata-album-artist", "Sendspin", "")
	flag.StringVar(&parsed.MetadataAlbum, "metadata-album", "Protocol Fixtures", "")
	flag.StringVar(&parsed.MetadataURL, "metadata-artwork-url", "https://example.invalid/almost-silent.jpg", "")
	flag.IntVar(&parsed.MetadataYear, "metadata-year", 2026, "")
	flag.IntVar(&parsed.MetadataTrack, "metadata-track", 1, "")
	flag.StringVar(&parsed.MetadataRepeat, "metadata-repeat", "all", "")
	flag.StringVar(&parsed.MetadataShuffle, "metadata-shuffle", "false", "")
	flag.IntVar(&parsed.MetadataProgress, "metadata-track-progress", 12000, "")
	flag.IntVar(&parsed.MetadataDuration, "metadata-track-duration", 180000, "")
	flag.IntVar(&parsed.MetadataSpeed, "metadata-playback-speed", 1000, "")
	flag.StringVar(&parsed.ControllerCommand, "controller-command", "next", "")
	flag.StringVar(&parsed.ArtworkFormat, "artwork-format", "jpeg", "")
	flag.IntVar(&parsed.ArtworkWidth, "artwork-width", 256, "")
	flag.IntVar(&parsed.ArtworkHeight, "artwork-height", 256, "")
	flag.Parse()
	return parsed
}

func run(parsed args) int {
	if !conformance.SupportsScenario(parsed.ScenarioID) {
		return exitWithSummary(parsed, errorSummary(parsed, fmt.Sprintf("sendspin-go server does not support %s", parsed.ScenarioID)))
	}

	var fixture *conformance.Fixture
	var flacPayload *flacTransport
	var err error
	if conformance.IsPlayerScenario(parsed.ScenarioID) {
		fixture, err = conformance.DecodeFixture(parsed.Fixture, parsed.ClipSeconds)
		if err != nil {
			return exitWithSummary(parsed, errorSummary(parsed, err.Error()))
		}
		if strings.EqualFold(parsed.PreferredCodec, "flac") {
			flacPayload, err = loadFLACTransport(parsed.Fixture, parsed.ClipSeconds)
			if err != nil {
				return exitWithSummary(parsed, errorSummary(parsed, err.Error()))
			}
		}
	}

	if parsed.InitiatorRole == "client" {
		return runListeningServer(parsed, fixture, flacPayload)
	}

	if err := conformance.WriteJSON(parsed.Ready, conformance.BuildReadyPayload(parsed.ScenarioID, parsed.InitiatorRole, "")); err != nil {
		log.Printf("failed to write ready file: %v", err)
	}

	clientURL, err := conformance.WaitForEndpoint(
		parsed.Registry,
		parsed.ClientName,
		time.Duration(parsed.TimeoutSeconds*float64(time.Second)),
	)
	if err != nil {
		return exitWithSummary(parsed, errorSummary(parsed, err.Error()))
	}

	conn, _, err := websocket.DefaultDialer.Dial(clientURL, nil)
	if err != nil {
		return exitWithSummary(parsed, errorSummary(parsed, fmt.Sprintf("failed to connect to client listener: %v", err)))
	}
	defer conn.Close()

	summary, err := runSession(conn, parsed, fixture, flacPayload, "registry_advertised")
	if err != nil {
		return exitWithSummary(parsed, errorSummary(parsed, err.Error()))
	}
	return exitWithSummary(parsed, summary)
}

func runListeningServer(parsed args, fixture *conformance.Fixture, flacPayload *flacTransport) int {
	listener, err := net.Listen("tcp", fmt.Sprintf("%s:%d", parsed.Host, parsed.Port))
	if err != nil {
		return exitWithSummary(parsed, errorSummary(parsed, fmt.Sprintf("failed to bind listener: %v", err)))
	}
	defer listener.Close()

	upgrader := websocket.Upgrader{
		CheckOrigin: func(_ *http.Request) bool { return true },
	}
	sessionCh := make(chan sessionResult, 1)
	attached := false
	mux := http.NewServeMux()
	mux.HandleFunc(defaultServerPath, func(w http.ResponseWriter, r *http.Request) {
		if attached {
			http.Error(w, "busy", http.StatusConflict)
			return
		}
		attached = true
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			sessionCh <- sessionResult{err: fmt.Errorf("failed to upgrade websocket: %w", err)}
			return
		}
		go func() {
			defer conn.Close()
			summary, sessionErr := runSession(conn, parsed, fixture, flacPayload, "registry_advertised")
			sessionCh <- sessionResult{summary: summary, err: sessionErr}
		}()
	})

	server := &http.Server{Handler: mux}
	go func() {
		if serveErr := server.Serve(listener); serveErr != nil && serveErr != http.ErrServerClosed {
			sessionCh <- sessionResult{err: serveErr}
		}
	}()

	url := fmt.Sprintf("ws://%s:%d%s", parsed.Host, parsed.Port, defaultServerPath)
	if err := conformance.RegisterEndpoint(parsed.Registry, parsed.ServerName, url); err != nil {
		_ = server.Close()
		return exitWithSummary(parsed, errorSummary(parsed, fmt.Sprintf("failed to register endpoint: %v", err)))
	}
	if err := conformance.WriteJSON(parsed.Ready, conformance.BuildReadyPayload(parsed.ScenarioID, parsed.InitiatorRole, url)); err != nil {
		log.Printf("failed to write ready file: %v", err)
	}

	select {
	case result := <-sessionCh:
		_ = server.Close()
		if result.err != nil {
			return exitWithSummary(parsed, errorSummary(parsed, result.err.Error()))
		}
		return exitWithSummary(parsed, result.summary)
	case <-time.After(time.Duration(parsed.TimeoutSeconds * float64(time.Second))):
		_ = server.Close()
		return exitWithSummary(parsed, errorSummary(parsed, fmt.Sprintf("timed out waiting for client %q", parsed.ClientName)))
	}
}

func runSession(
	conn *websocket.Conn,
	parsed args,
	fixture *conformance.Fixture,
	flacPayload *flacTransport,
	discoveryMethod string,
) (map[string]any, error) {
	_, helloBytes, err := conn.ReadMessage()
	if err != nil {
		return nil, fmt.Errorf("failed to read client/hello: %w", err)
	}
	rawPeerHello := decodeRawJSON(helloBytes)

	var envelope conformance.Envelope
	if err := json.Unmarshal(helloBytes, &envelope); err != nil {
		return nil, fmt.Errorf("invalid client/hello: %w", err)
	}
	if envelope.Type != "client/hello" {
		return nil, fmt.Errorf("expected client/hello, got %s", envelope.Type)
	}

	var hello protocol.ClientHello
	if err := json.Unmarshal(envelope.Payload, &hello); err != nil {
		return nil, fmt.Errorf("invalid client/hello payload: %w", err)
	}

	if hello.Name != parsed.ClientName {
		log.Printf("connected client name %q did not match expected %q", hello.Name, parsed.ClientName)
	}

	serverHello := protocol.ServerHello{
		ServerID:         parsed.ServerID,
		Name:             parsed.ServerName,
		Version:          1,
		ActiveRoles:      conformance.ActiveRoles(parsed.ScenarioID, hello.SupportedRoles),
		ConnectionReason: "playback",
	}
	sc := protocol.NewServerConn(conn, hello.ClientID, hello.Name)
	defer sc.Close()

	if err := sc.Send("server/hello", serverHello); err != nil {
		return nil, fmt.Errorf("failed to write server/hello: %w", err)
	}

	readDone := make(chan struct{})
	readErrCh := make(chan error, 1)
	controllerCh := make(chan map[string]any, 1)
	go readClientMessages(conn, sc, controllerCh, readDone, readErrCh)

	baseSummary := map[string]any{
		"status":           "ok",
		"implementation":   "sendspin-go",
		"role":             "server",
		"server_id":        parsed.ServerID,
		"server_name":      parsed.ServerName,
		"scenario_id":      parsed.ScenarioID,
		"initiator_role":   parsed.InitiatorRole,
		"preferred_codec":  parsed.PreferredCodec,
		"discovery_method": discoveryMethod,
		"peer_hello":       rawPeerHello,
		"client": map[string]any{
			"client_id":       hello.ClientID,
			"name":            hello.Name,
			"supported_roles": hello.SupportedRoles,
		},
	}

	switch {
	case conformance.IsPlayerScenario(parsed.ScenarioID):
		summary, err := runPlayerScenario(sc, conn, readDone, readErrCh, parsed, hello, fixture, flacPayload)
		if err != nil {
			return nil, err
		}
		return mergeMaps(baseSummary, summary), nil
	case conformance.IsMetadataScenario(parsed.ScenarioID):
		if err := sc.Send("server/state", metadataStateMessage(parsed)); err != nil {
			return nil, err
		}
		time.Sleep(200 * time.Millisecond)
		_ = sendClose(conn)
		<-readDone
		if err := drainReadError(readErrCh); err != nil {
			return nil, err
		}
		return mergeMaps(baseSummary, map[string]any{
			"metadata": map[string]any{
				"expected": metadataSnapshot(parsed),
			},
		}), nil
	case conformance.IsControllerScenario(parsed.ScenarioID):
		if err := sc.Send("server/state", controllerStateMessage(parsed)); err != nil {
			return nil, err
		}
		var received map[string]any
		select {
		case received = <-controllerCh:
		case <-time.After(time.Duration(parsed.TimeoutSeconds * float64(time.Second))):
			_ = sendClose(conn)
			<-readDone
			return nil, fmt.Errorf("timed out waiting for controller command %q", parsed.ControllerCommand)
		}
		time.Sleep(100 * time.Millisecond)
		_ = sendClose(conn)
		<-readDone
		if err := drainReadError(readErrCh); err != nil {
			return nil, err
		}
		return mergeMaps(baseSummary, map[string]any{
			"controller": map[string]any{
				"expected_command": map[string]any{"command": parsed.ControllerCommand},
				"received_command": received,
				"supported_commands": []string{
					parsed.ControllerCommand,
				},
				"volume": 100,
				"muted":  false,
			},
		}), nil
	case conformance.IsArtworkScenario(parsed.ScenarioID):
		imageBytes, err := encodeReferenceArtwork(parsed.ArtworkFormat, parsed.ArtworkWidth, parsed.ArtworkHeight)
		if err != nil {
			return nil, err
		}
		if err := sc.Send("stream/start", protocol.StreamStart{
			Artwork: &protocol.StreamStartArtwork{
				Channels: []protocol.ArtworkStreamChannel{
					{
						Source: "album",
						Format: conformance.NormalizeArtworkFormat(parsed.ArtworkFormat),
						Width:  parsed.ArtworkWidth,
						Height: parsed.ArtworkHeight,
					},
				},
			},
		}); err != nil {
			return nil, err
		}
		if err := sc.SendBinary(protocol.CreateArtworkChunk(0, conformance.CurrentMicros()+250_000, imageBytes)); err != nil {
			return nil, err
		}
		time.Sleep(200 * time.Millisecond)
		_ = sendClose(conn)
		<-readDone
		if err := drainReadError(readErrCh); err != nil {
			return nil, err
		}
		return mergeMaps(baseSummary, map[string]any{
			"artwork": map[string]any{
				"channel":        0,
				"source":         "album",
				"format":         conformance.NormalizeArtworkFormat(parsed.ArtworkFormat),
				"width":          parsed.ArtworkWidth,
				"height":         parsed.ArtworkHeight,
				"encoded_sha256": conformance.SHA256Hex(imageBytes),
				"byte_count":     len(imageBytes),
			},
		}), nil
	}

	return nil, fmt.Errorf("unsupported scenario %s", parsed.ScenarioID)
}

func runPlayerScenario(
	sc *protocol.ServerConn,
	conn *websocket.Conn,
	readDone <-chan struct{},
	readErrCh <-chan error,
	parsed args,
	hello protocol.ClientHello,
	fixture *conformance.Fixture,
	flacPayload *flacTransport,
) (map[string]any, error) {
	if strings.EqualFold(parsed.PreferredCodec, "flac") {
		if flacPayload == nil {
			return nil, fmt.Errorf("missing FLAC transport payload")
		}
		codecHeader := base64.StdEncoding.EncodeToString(flacPayload.CodecHeader)
		if err := sc.Send("stream/start", protocol.StreamStart{
			Player: &protocol.StreamStartPlayer{
				Codec:       "flac",
				SampleRate:  flacPayload.SampleRate,
				Channels:    flacPayload.Channels,
				BitDepth:    flacPayload.BitDepth,
				CodecHeader: codecHeader,
			},
		}); err != nil {
			return nil, err
		}
		if err := sc.Send("server/state", metadataStateMessage(parsed)); err != nil {
			return nil, err
		}
		if err := sc.Send("group/update", protocol.GroupUpdate{
			GroupID:       ptr(parsed.ServerID),
			PlaybackState: ptr("playing"),
		}); err != nil {
			return nil, err
		}
		time.Sleep(25 * time.Millisecond)
		if err := sc.SendBinary(protocol.CreateAudioChunk(conformance.CurrentMicros()+250_000, flacPayload.AudioBytes)); err != nil {
			return nil, err
		}
		time.Sleep(100 * time.Millisecond)
		_ = sc.Send("stream/end", protocol.StreamEnd{Roles: []string{"player"}})
		_ = sendClose(conn)
		<-readDone
		if err := drainReadError(readErrCh); err != nil {
			return nil, err
		}
		return map[string]any{
			"stream": map[string]any{
				"codec":        "flac",
				"sample_rate":  flacPayload.SampleRate,
				"channels":     flacPayload.Channels,
				"bit_depth":    flacPayload.BitDepth,
				"codec_header": codecHeader,
			},
			"audio": map[string]any{
				"fixture":                  parsed.Fixture,
				"source_flac_sha256":       flacPayload.SourceFlacSHA256,
				"source_pcm_sha256":        flacPayload.SourcePcmSHA256,
				"sent_codec_header_sha256": conformance.SHA256Hex(flacPayload.CodecHeader),
				"sent_encoded_sha256":      conformance.SHA256Hex(flacPayload.AudioBytes),
				"sent_audio_chunk_count":   1,
				"sent_encoded_byte_count":  len(flacPayload.AudioBytes),
				"clip_seconds":             parsed.ClipSeconds,
				"sample_rate":              flacPayload.SampleRate,
				"channels":                 flacPayload.Channels,
				"bit_depth":                flacPayload.BitDepth,
				"frame_count":              flacPayload.FrameCount,
				"duration_seconds":         flacPayload.DurationSeconds,
			},
		}, nil
	}

	if fixture == nil {
		return nil, fmt.Errorf("missing PCM fixture")
	}
	streamFixture, err := selectPCMFixture(fixture, hello)
	if err != nil {
		return nil, err
	}
	if err := sc.Send("stream/start", protocol.StreamStart{
		Player: &protocol.StreamStartPlayer{
			Codec:      "pcm",
			SampleRate: streamFixture.SampleRate,
			Channels:   streamFixture.Channels,
			BitDepth:   streamFixture.BitDepth,
		},
	}); err != nil {
		return nil, err
	}
	if err := sc.Send("server/state", metadataStateMessage(parsed)); err != nil {
		return nil, err
	}
	if err := sc.Send("group/update", protocol.GroupUpdate{
		GroupID:       ptr(parsed.ServerID),
		PlaybackState: ptr("playing"),
	}); err != nil {
		return nil, err
	}
	time.Sleep(25 * time.Millisecond)

	sentHasher := sha256.New()
	chunkCount := 0
	byteCount := 0
	nextTimestamp := conformance.CurrentMicros() + 250_000
	for _, block := range conformance.PCMBlocks(streamFixture.PCMBytes, streamFixture.SampleRate, streamFixture.Channels, streamFixture.BitDepth, 50) {
		if err := sc.SendBinary(protocol.CreateAudioChunk(nextTimestamp, block.Data)); err != nil {
			return nil, err
		}
		_, _ = sentHasher.Write(block.Data)
		chunkCount++
		byteCount += len(block.Data)
		nextTimestamp += block.DurationUS
		time.Sleep(5 * time.Millisecond)
	}

	time.Sleep(100 * time.Millisecond)
	_ = sc.Send("stream/end", protocol.StreamEnd{Roles: []string{"player"}})
	_ = sendClose(conn)
	<-readDone
	if err := drainReadError(readErrCh); err != nil {
		return nil, err
	}

	return map[string]any{
		"stream": map[string]any{
			"codec":       "pcm",
			"sample_rate": streamFixture.SampleRate,
			"channels":    streamFixture.Channels,
			"bit_depth":   streamFixture.BitDepth,
		},
		"audio": map[string]any{
			"fixture":                 parsed.Fixture,
			"source_flac_sha256":      streamFixture.SourceFlacSHA256,
			"source_pcm_sha256":       streamFixture.SourcePcmSHA256,
			"sent_encoded_sha256":     conformance.HexLower(sentHasher.Sum(nil)),
			"sent_audio_chunk_count":  chunkCount,
			"sent_encoded_byte_count": byteCount,
			"clip_seconds":            parsed.ClipSeconds,
			"sample_rate":             streamFixture.SampleRate,
			"channels":                streamFixture.Channels,
			"bit_depth":               streamFixture.BitDepth,
			"frame_count":             streamFixture.FrameCount,
			"duration_seconds":        streamFixture.DurationSeconds,
		},
	}, nil
}

func readClientMessages(
	conn *websocket.Conn,
	sc *protocol.ServerConn,
	controllerCh chan<- map[string]any,
	done chan<- struct{},
	errCh chan<- error,
) {
	defer close(done)
	for {
		messageType, payload, err := conn.ReadMessage()
		if err != nil {
			if websocket.IsCloseError(err, websocket.CloseNormalClosure, websocket.CloseGoingAway) {
				return
			}
			if !strings.Contains(strings.ToLower(err.Error()), "close") {
				select {
				case errCh <- err:
				default:
				}
			}
			return
		}
		if messageType != websocket.TextMessage {
			continue
		}
		var envelope conformance.Envelope
		if err := json.Unmarshal(payload, &envelope); err != nil {
			continue
		}
		switch envelope.Type {
		case "client/time":
			var clientTime protocol.ClientTime
			if err := json.Unmarshal(envelope.Payload, &clientTime); err != nil {
				continue
			}
			_ = sc.Send("server/time", protocol.ServerTime{
				ClientTransmitted: clientTime.ClientTransmitted,
				ServerReceived:    conformance.CurrentMicros(),
				ServerTransmitted: conformance.CurrentMicros(),
			})
		case "client/command":
			var command clientCommandPayload
			if err := json.Unmarshal(envelope.Payload, &command); err != nil {
				continue
			}
			if command.Controller == nil {
				continue
			}
			normalized := map[string]any{
				"command": command.Controller.Command,
			}
			if command.Controller.Volume != nil {
				normalized["volume"] = *command.Controller.Volume
			}
			if command.Controller.Mute != nil {
				normalized["mute"] = *command.Controller.Mute
			}
			select {
			case controllerCh <- normalized:
			default:
			}
		}
	}
}

func drainReadError(errCh <-chan error) error {
	select {
	case err := <-errCh:
		return err
	default:
		return nil
	}
}

func metadataStateMessage(parsed args) protocol.ServerStateMessage {
	return protocol.ServerStateMessage{
		Metadata: &protocol.MetadataState{
			Timestamp:   conformance.CurrentMicros(),
			Title:       ptr(parsed.MetadataTitle),
			Artist:      ptr(parsed.MetadataArtist),
			AlbumArtist: ptr(parsed.MetadataAlbumArt),
			Album:       ptr(parsed.MetadataAlbum),
			ArtworkURL:  ptr(parsed.MetadataURL),
			Year:        intPtr(parsed.MetadataYear),
			Track:       intPtr(parsed.MetadataTrack),
			Repeat:      ptr(parsed.MetadataRepeat),
			Shuffle:     boolPtr(parsed.MetadataShuffle == "true"),
			Progress: &protocol.ProgressState{
				TrackProgress: parsed.MetadataProgress,
				TrackDuration: parsed.MetadataDuration,
				PlaybackSpeed: parsed.MetadataSpeed,
			},
		},
	}
}

func metadataSnapshot(parsed args) map[string]any {
	return map[string]any{
		"title":        parsed.MetadataTitle,
		"artist":       parsed.MetadataArtist,
		"album_artist": parsed.MetadataAlbumArt,
		"album":        parsed.MetadataAlbum,
		"artwork_url":  parsed.MetadataURL,
		"year":         parsed.MetadataYear,
		"track":        parsed.MetadataTrack,
		"repeat":       parsed.MetadataRepeat,
		"shuffle":      parsed.MetadataShuffle == "true",
		"progress": map[string]any{
			"track_progress": parsed.MetadataProgress,
			"track_duration": parsed.MetadataDuration,
			"playback_speed": parsed.MetadataSpeed,
		},
	}
}

func controllerStateMessage(parsed args) protocol.ServerStateMessage {
	return protocol.ServerStateMessage{
		Controller: &protocol.ControllerState{
			SupportedCommands: []string{parsed.ControllerCommand},
			Volume:            100,
			Muted:             false,
			Repeat:  "off",
			Shuffle: false,
		},
	}
}

func supportedPlayerFormats(hello protocol.ClientHello) []protocol.AudioFormat {
	if hello.PlayerV1Support != nil && len(hello.PlayerV1Support.SupportedFormats) > 0 {
		return hello.PlayerV1Support.SupportedFormats
	}
	if hello.PlayerSupport != nil && len(hello.PlayerSupport.SupportedFormats) > 0 {
		return hello.PlayerSupport.SupportedFormats
	}
	return nil
}

func selectPCMFixture(fixture *conformance.Fixture, hello protocol.ClientHello) (*conformance.Fixture, error) {
	formats := supportedPlayerFormats(hello)
	if len(formats) == 0 || fixture == nil {
		return fixture, nil
	}
	hasExact := false
	allows16 := false
	for _, format := range formats {
		if !strings.EqualFold(format.Codec, "pcm") {
			continue
		}
		if format.SampleRate != fixture.SampleRate || format.Channels != fixture.Channels {
			continue
		}
		if format.BitDepth == fixture.BitDepth {
			hasExact = true
		}
		if format.BitDepth == 16 {
			allows16 = true
		}
	}
	if hasExact {
		return fixture, nil
	}
	if fixture.BitDepth == 24 && allows16 {
		return convertFixtureBitDepth(fixture, 16)
	}
	return fixture, nil
}

func convertFixtureBitDepth(fixture *conformance.Fixture, targetBitDepth int) (*conformance.Fixture, error) {
	if fixture == nil || targetBitDepth == fixture.BitDepth {
		return fixture, nil
	}
	if fixture.BitDepth != 24 || targetBitDepth != 16 {
		return nil, fmt.Errorf("unsupported PCM conversion %d -> %d", fixture.BitDepth, targetBitDepth)
	}

	converted := make([]byte, 0, fixture.FrameCount*fixture.Channels*2)
	for offset := 0; offset+2 < len(fixture.PCMBytes); offset += 3 {
		value := int32(fixture.PCMBytes[offset]) |
			int32(fixture.PCMBytes[offset+1])<<8 |
			int32(fixture.PCMBytes[offset+2])<<16
		if value&0x800000 != 0 {
			value |= ^0x00ffffff
		}
		sample := int16(value >> 8)
		converted = append(converted, byte(sample), byte(sample>>8))
	}

	hasher := conformance.NewFloatPcmHasher()
	if err := hasher.UpdateFromPCMBytes(converted, 16); err != nil {
		return nil, err
	}

	return &conformance.Fixture{
		Path:             fixture.Path,
		SourceFlacSHA256: fixture.SourceFlacSHA256,
		SourcePcmSHA256:  hasher.HexDigest(),
		SampleRate:       fixture.SampleRate,
		Channels:         fixture.Channels,
		BitDepth:         16,
		FrameCount:       fixture.FrameCount,
		DurationSeconds:  fixture.DurationSeconds,
		PCMBytes:         converted,
	}, nil
}

func loadFLACTransport(path string, clipSeconds float64) (*flacTransport, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("failed to read FLAC fixture: %w", err)
	}
	if len(raw) < 8 || string(raw[:4]) != "fLaC" {
		return nil, fmt.Errorf("fixture is not a FLAC stream")
	}

	fixture, err := conformance.DecodeFixture(path, clipSeconds)
	if err != nil {
		return nil, err
	}
	if clipSeconds > 0 && fixture.DurationSeconds > clipSeconds+0.01 {
		return nil, fmt.Errorf("partial FLAC fixture clipping is not implemented for sendspin-go")
	}

	offset := 4
	var streamInfo []byte
	for {
		if offset+4 > len(raw) {
			return nil, fmt.Errorf("invalid FLAC metadata block")
		}
		header := raw[offset : offset+4]
		lastBlock := header[0]&0x80 != 0
		blockType := header[0] & 0x7f
		blockLength := int(header[1])<<16 | int(header[2])<<8 | int(header[3])
		if offset+4+blockLength > len(raw) {
			return nil, fmt.Errorf("invalid FLAC metadata length")
		}
		body := raw[offset+4 : offset+4+blockLength]
		if blockType == 0 && streamInfo == nil {
			streamInfo = append([]byte(nil), body...)
		}
		offset += 4 + blockLength
		if lastBlock {
			break
		}
	}
	if len(streamInfo) == 0 {
		return nil, fmt.Errorf("FLAC fixture did not contain STREAMINFO metadata")
	}

	codecHeader := []byte{
		'f', 'L', 'a', 'C',
		0x80,
		byte(len(streamInfo) >> 16),
		byte(len(streamInfo) >> 8),
		byte(len(streamInfo)),
	}
	codecHeader = append(codecHeader, streamInfo...)

	return &flacTransport{
		CodecHeader:      codecHeader,
		AudioBytes:       raw[offset:],
		SampleRate:       fixture.SampleRate,
		Channels:         fixture.Channels,
		BitDepth:         fixture.BitDepth,
		FrameCount:       fixture.FrameCount,
		DurationSeconds:  fixture.DurationSeconds,
		SourceFlacSHA256: fixture.SourceFlacSHA256,
		SourcePcmSHA256:  fixture.SourcePcmSHA256,
	}, nil
}

func encodeReferenceArtwork(format string, width int, height int) ([]byte, error) {
	if width <= 0 || height <= 0 {
		return nil, fmt.Errorf("artwork dimensions must be positive")
	}

	canvas := image.NewRGBA(image.Rect(0, 0, width, height))
	draw.Draw(canvas, canvas.Bounds(), &image.Uniform{C: color.RGBA{0xE8, 0xD4, 0xB8, 0xFF}}, image.Point{}, draw.Src)
	draw.Draw(canvas, image.Rect(0, 0, width, height/3), &image.Uniform{C: color.RGBA{0xC9, 0x78, 0x3B, 0xFF}}, image.Point{}, draw.Src)
	draw.Draw(canvas, image.Rect(0, height/3, width, 2*height/3), &image.Uniform{C: color.RGBA{0x93, 0x52, 0x28, 0xFF}}, image.Point{}, draw.Src)
	draw.Draw(canvas, image.Rect(0, 2*height/3, width, height), &image.Uniform{C: color.RGBA{0x4B, 0x2F, 0x1B, 0xFF}}, image.Point{}, draw.Src)

	var buf bytes.Buffer
	switch conformance.NormalizeArtworkFormat(format) {
	case "png":
		if err := png.Encode(&buf, canvas); err != nil {
			return nil, err
		}
	case "bmp":
		return nil, fmt.Errorf("BMP artwork encoding is not implemented for sendspin-go conformance")
	default:
		if err := jpeg.Encode(&buf, canvas, &jpeg.Options{Quality: 90}); err != nil {
			return nil, err
		}
	}
	return buf.Bytes(), nil
}

func mergeMaps(base map[string]any, extra map[string]any) map[string]any {
	merged := make(map[string]any, len(base)+len(extra))
	for key, value := range base {
		merged[key] = value
	}
	for key, value := range extra {
		merged[key] = value
	}
	return merged
}

func sendClose(conn *websocket.Conn) error {
	deadline := time.Now().Add(time.Second)
	return conn.WriteControl(websocket.CloseMessage, websocket.FormatCloseMessage(websocket.CloseNormalClosure, "done"), deadline)
}

func decodeRawJSON(raw []byte) any {
	var value any
	if err := json.Unmarshal(raw, &value); err != nil {
		return nil
	}
	return value
}

func errorSummary(parsed args, reason string) map[string]any {
	return map[string]any{
		"status":          "error",
		"reason":          reason,
		"implementation":  "sendspin-go",
		"role":            "server",
		"server_id":       parsed.ServerID,
		"server_name":     parsed.ServerName,
		"scenario_id":     parsed.ScenarioID,
		"initiator_role":  parsed.InitiatorRole,
		"preferred_codec": parsed.PreferredCodec,
		"peer_hello":      nil,
	}
}

func exitWithSummary(parsed args, summary map[string]any) int {
	if err := conformance.WriteJSON(parsed.Summary, summary); err != nil {
		log.Printf("failed to write summary: %v", err)
		return 1
	}
	if err := conformance.PrintFile(parsed.Summary); err != nil {
		log.Printf("failed to print summary: %v", err)
	}
	if status, ok := summary["status"].(string); ok && status == "ok" {
		return 0
	}
	return 1
}

func ptr(value string) *string {
	return &value
}

func intPtr(value int) *int {
	return &value
}

func boolPtr(value bool) *bool {
	return &value
}

func init() {
	log.SetFlags(log.LstdFlags | log.Lmicroseconds)
}
