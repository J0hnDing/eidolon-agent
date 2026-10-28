package main

import (
	"context"
	"testing"
	"time"

	"go.mau.fi/whatsmeow/types"
	"google.golang.org/protobuf/proto"
)

func TestMuseRootOnlyReplyBeforeSendAcceptanceIsReconciled(t *testing.T) {
	b, f := museBridge(t)
	b.device.ChatSettings = &museRootStore{id: "shared-wasa-root"}
	f.onMuseSend = func(context.Context) {
		event := museEvent("early-reply", "shared-wasa-root", "answer before send returns")
		// The fake returns its send timestamp after this callback. Model provider
		// ordering explicitly: the reply follows the original accepted prompt.
		event.Info.Timestamp = time.Now().Add(time.Second)
		event.Info.MsgBotInfo.EditType = types.EditTypeLast
		if err := b.ingestMessage(event, false); err != nil {
			t.Fatal(err)
		}
	}
	input := museInput(b)
	result, err := b.request("muse_converse", input)
	if err != nil || result["status"] != "replied" || f.sends != 1 {
		t.Fatalf("early root-only reply was stranded: %v %v", result, err)
	}
	incoming, err := b.cache.MuseIncoming(b.account, 0, time.Time{})
	if err != nil || len(incoming["items"].([]map[string]any)) != 0 {
		t.Fatalf("early reply also initiated a conversation: %v %v", incoming, err)
	}
}

func TestMuseSharedRootReplyContinuesExistingExchangeWithoutInitiation(t *testing.T) {
	b, f := museBridge(t)
	b.device.ChatSettings = &museRootStore{id: "shared-wasa-root"}
	input := museInput(b)
	if _, err := b.request("muse_converse", input); err != nil {
		t.Fatal(err)
	}
	event := museEvent("reply", "shared-wasa-root", "initial answer")
	event.Info.MsgBotInfo.EditType = types.EditTypeFirst
	if err := b.ingestMessage(event, false); err != nil {
		t.Fatal(err)
	}
	result, err := b.request("muse_converse", museResume(input))
	if err != nil || result["status"] != "pending" {
		t.Fatalf("stream settled before its last edit: %v %v", result, err)
	}
	event.Info.ID = "last-edit"
	event.Info.MsgBotInfo.EditTargetID = "reply"
	event.Info.MsgBotInfo.EditType = types.EditTypeLast
	event.Message.Conversation = proto.String("complete answer")
	if err = b.ingestMessage(event, false); err != nil {
		t.Fatal(err)
	}
	result, err = b.request("muse_converse", museResume(input))
	if err != nil || result["status"] != "replied" || f.sends != 1 {
		t.Fatalf("root-only reply did not complete original exchange: %v %v", result, err)
	}
	replies := result["replies"].([]map[string]any)
	if len(replies) != 1 || replies[0]["text"] != "complete answer" || replies[0]["message_id"] != "reply" {
		t.Fatalf("stream edit changed reply identity: %v", replies)
	}
	incoming, err := b.cache.MuseIncoming(b.account, 0, time.Time{})
	if err != nil || len(incoming["items"].([]map[string]any)) != 0 {
		t.Fatalf("matched reply started a second conversation: %v %v", incoming, err)
	}
}
