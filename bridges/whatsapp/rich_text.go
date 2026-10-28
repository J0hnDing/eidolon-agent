package main

import (
	"encoding/base64"
	"encoding/json"
	"strings"
)

// Project only the documented text primitive in GenAI's unified response.
// Rich layout, HTML, media, citations and original-recipient metadata are not
// text messages. Never walk arbitrary JSON looking for strings.
func unifiedText(data []byte) *string {
	if len(data) == 0 || len(data) > 1024*1024 {
		return nil
	}
	if !json.Valid(data) {
		decoded, err := base64.StdEncoding.DecodeString(string(data))
		if err != nil || !json.Valid(decoded) {
			return nil
		}
		data = decoded
	}
	var response struct {
		Sections []struct {
			ViewModel struct {
				Type      string `json:"__typename"`
				Primitive struct {
					Type string  `json:"__typename"`
					Text *string `json:"text"`
				} `json:"primitive"`
			} `json:"view_model"`
		} `json:"sections"`
	}
	if json.Unmarshal(data, &response) != nil || len(response.Sections) > 100 {
		return nil
	}
	parts := []string{}
	for _, section := range response.Sections {
		view := section.ViewModel
		if view.Type == "GenAISingleLayoutViewModel" && view.Primitive.Type == "GenAIMarkdownTextUXPrimitive" && view.Primitive.Text != nil {
			parts = append(parts, *view.Primitive.Text)
		}
	}
	if len(parts) == 0 {
		return nil
	}
	text := strings.Join(parts, "\n\n")
	return &text
}
