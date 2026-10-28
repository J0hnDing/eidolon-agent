package main

import (
	"testing"
	"time"

	"go.mau.fi/whatsmeow/proto/waCommon"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"google.golang.org/protobuf/proto"
)

func TestMuseIncomingPrivateReaderUsesExactLiveAccountAndValidatedCursor(t *testing.T) {
	b, socket := museBridge(t)
	for _, extra := range []map[string]any{{"after_id": -1}, {"after_id": 1.5}, {"after_id": "1"}, {"since": 42}, {"since": "invalid-date"}, {"grant_id": 1}, {"recipient": types.MuseJID.String()}} {
		input := map[string]any{"account_id": b.account}
		for key, value := range extra {
			input[key] = value
		}
		if _, err := b.request("muse_incoming", input); errorCode(err) != "invalid_input" {
			t.Fatalf("bad reader input accepted: %#v %v", extra, err)
		}
	}
	if _, err := b.request("muse_incoming", map[string]any{"account_id": "other"}); errorCode(err) != "connection_changed" {
		t.Fatalf("wrong account accepted: %v", err)
	}
	input := map[string]any{"account_id": b.account, "after_id": 0, "since": time.Now().Add(-time.Minute).UTC().Format(time.RFC3339Nano)}
	socket.online = false
	if _, err := b.request("muse_incoming", input); errorCode(err) != "connection_unavailable" {
		t.Fatalf("offline reader accepted: %v", err)
	}
	socket.online = true
	event := museEvent("incoming-live", "", "a new idea")
	event.Info.MsgBotInfo.EditType = types.EditTypeLast
	if err := b.ingestMessage(event, false); err != nil {
		t.Fatal(err)
	}
	historical := museEvent("old-history", "", "old text")
	if err := b.ingestMessage(historical, true); err != nil {
		t.Fatal(err)
	}
	if err := b.ingestMessage(historical, false); err != nil {
		t.Fatal(err)
	}
	edit := museEvent("edit-envelope", "", "changed text")
	edit.Info.MsgBotInfo.EditTargetID = "incoming-live"
	edit.Info.MsgBotInfo.EditType = types.EditTypeLast
	if err := b.ingestMessage(edit, false); err != nil {
		t.Fatal(err)
	}
	// A protocol edit of historical text must not create an initiation.
	protocol := museEvent("protocol-edit", "", "")
	protocol.Message = &waE2E.Message{ProtocolMessage: &waE2E.ProtocolMessage{Type: waE2E.ProtocolMessage_MESSAGE_EDIT.Enum(), Key: &waCommon.MessageKey{ID: proto.String("old-history")}, EditedMessage: &waE2E.Message{Conversation: proto.String("updated history")}}}
	if err := b.ingestMessage(protocol, false); err != nil {
		t.Fatal(err)
	}
	result, err := b.request("muse_incoming", input)
	if err != nil {
		t.Fatal(err)
	}
	items := result["items"].([]map[string]any)
	if len(items) != 1 || items[0]["message_id"] != "incoming-live" || socket.sends != 0 {
		t.Fatalf("wrong activation or reader sent a message: %#v %d", result, socket.sends)
	}
}
