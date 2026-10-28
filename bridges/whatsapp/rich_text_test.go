package main

import (
	"encoding/base64"
	"strings"
	"testing"
)

func TestUnifiedTextUsesOnlyKnownTextPrimitive(t *testing.T) {
	payload := []byte(`{"sections":[{"view_model":{"__typename":"GenAISingleLayoutViewModel","primitive":{"__typename":"GenAIMarkdownTextUXPrimitive","text":"Hello from Muse"}}},{"view_model":{"__typename":"GenAISingleLayoutViewModel","primitive":{"__typename":"GenAIInlineImageUXPrimitive","text":"Excluded image data"}}}],"original_recipient_metadata":{"text":"Excluded private metadata"}}`)
	for _, data := range [][]byte{payload, []byte(base64.StdEncoding.EncodeToString(payload))} {
		text := unifiedText(data)
		if text == nil || *text != "Hello from Muse" {
			t.Fatal("incorrect text projection")
		}
	}
	for _, data := range [][]byte{[]byte(`{"text":"No recursive extraction"}`), []byte(`{"sections":"malformed"}`), []byte("invalid"), []byte(strings.Repeat("x", 1024*1024+1))} {
		if unifiedText(data) != nil {
			t.Fatal("unsupported payload accepted")
		}
	}
}
