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
		scenarioID == "server-initiated-legacy-unencrypted"
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

func ActiveRoles(scenarioID string, supported []string) []string {
	families := map[string]bool{}
	switch {
	case IsPlayerScenario(scenarioID):
		families["player"] = true
		families["metadata"] = true
	case IsMetadataScenario(scenarioID):
		families["metadata"] = true
	case IsControllerScenario(scenarioID):
		families["controller"] = true
	case IsArtworkScenario(scenarioID):
		families["artwork"] = true
	}

	roles := make([]string, 0, len(supported))
	seen := map[string]bool{}
	for _, role := range supported {
		family := role
		if index := strings.Index(role, "@"); index > 0 {
			family = role[:index]
		}
		if families[family] && !seen[family] {
			roles = append(roles, role)
			seen[family] = true
		}
	}
	return roles
}
