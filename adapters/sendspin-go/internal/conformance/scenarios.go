package conformance

import "strings"

func SupportsScenario(scenarioID string) bool {
	return IsPlayerScenario(scenarioID) ||
		IsMetadataScenario(scenarioID) ||
		IsControllerScenario(scenarioID) ||
		IsArtworkScenario(scenarioID)
}

func IsPlayerScenario(scenarioID string) bool {
	return scenarioID == "client-initiated-pcm" ||
		scenarioID == "server-initiated-pcm" ||
		scenarioID == "server-initiated-flac" ||
		scenarioID == "server-initiated-legacy-unencrypted" ||
		IsChunkFramingScenario(scenarioID)
}

func IsChunkFramingScenario(scenarioID string) bool {
	return scenarioID == "server-initiated-audio-chunk-framing"
}

func IsMetadataScenario(scenarioID string) bool {
	return scenarioID == "client-initiated-metadata" ||
		scenarioID == "server-initiated-metadata"
}

func IsControllerScenario(scenarioID string) bool {
	return scenarioID == "client-initiated-controller" ||
		scenarioID == "server-initiated-controller"
}

func IsArtworkScenario(scenarioID string) bool {
	return scenarioID == "client-initiated-artwork" ||
		scenarioID == "server-initiated-artwork"
}

func NormalizeArtworkFormat(raw string) string {
	switch strings.ToLower(strings.TrimSpace(raw)) {
	case "png":
		return "png"
	case "bmp":
		return "bmp"
	default:
		return "jpeg"
	}
}

func BuildReadyPayload(scenarioID string, initiatorRole string, url string) map[string]any {
	payload := map[string]any{
		"status":         "ready",
		"scenario_id":    scenarioID,
		"initiator_role": initiatorRole,
	}
	if url != "" {
		payload["url"] = url
	}
	return payload
}
