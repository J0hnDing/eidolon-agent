package main

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"eidolon/whatsapp/internal/cache"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waAICommonDeprecated"
	"go.mau.fi/whatsmeow/proto/waCommon"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/proto"
)

type museSocket struct {
	*fakeSocket
	to         types.JID
	id         string
	onMuseSend func(context.Context)
}

type museRootStore struct {
	store.NoopStore
	id string
}

func (s *museRootStore) GetWASARootSecretID(context.Context, types.JID) (types.MessageID, error) {
	return s.id, nil
}

func (f *museSocket) SendMessage(ctx context.Context, to types.JID, msg *waE2E.Message, extra ...whatsmeow.SendRequestExtra) (whatsmeow.SendResponse, error) {
	f.sends++
	f.to, f.lastMessage, f.id = to, msg, extra[0].ID
	if f.onMuseSend != nil {
		f.onMuseSend(ctx)
	}
	return whatsmeow.SendResponse{ID: f.id, Timestamp: time.Now()}, f.sendError
}

func museBridge(t *testing.T) (*bridge, *museSocket) {
	b, fake := testBridge(t)
	f := &museSocket{fakeSocket: fake}
	b.client = f
	return b, f
}

func museInput(b *bridge) map[string]any {
	return map[string]any{"account_id": b.account, "grant_id": float64(7), "exchange_id": strings.Repeat("a", 64), "text": "Ask Muse this", "wait_seconds": float64(0)}
}

func museResume(input map[string]any) map[string]any {
	return map[string]any{"account_id": input["account_id"], "grant_id": input["grant_id"], "exchange_id": input["exchange_id"], "wait_seconds": float64(0)}
}

func museEvent(id, target, body string) *events.Message {
	return &events.Message{Info: types.MessageInfo{MessageSource: types.MessageSource{Chat: types.MuseJID, Sender: types.MuseJID}, ID: id, Timestamp: time.Now(), MsgMetaInfo: types.MsgMetaInfo{TargetID: target}}, Message: &waE2E.Message{Conversation: proto.String(body)}}
}

func TestMuseFixedRecipientValidationAndStableReceipt(t *testing.T) {
	b, f := museBridge(t)
	for _, bad := range []map[string]any{{"recipient": types.MetaAIJID.String()}, {"chat_id": types.MuseJID.String()}, {"grant_id": 0}, {"grant_id": 2.5}, {"grant_id": "7"}, {"exchange_id": "opaque-but-invalid"}, {"text": nil}, {"text": " "}, {"text": strings.Repeat("x", 4001)}, {"wait_seconds": 21}, {"wait_seconds": -1}, {"reply_to_message_id": false}} {
		input := museInput(b)
		for k, v := range bad {
			input[k] = v
		}
		if _, err := b.request("muse_converse", input); errorCode(err) != "invalid_input" {
			t.Fatalf("invalid fields accepted: %v", err)
		}
	}
	if f.sends != 0 {
		t.Fatal("sent before validation")
	}
	input := museInput(b)
	first, err := b.request("muse_converse", input)
	if err != nil || first["status"] != "pending" || first["sent_message_id"] != f.id || f.to != types.MuseJID || f.id == "" {
		t.Fatalf("fixed send: %v %v", first, err)
	}
	for _, request := range []map[string]any{input, museResume(input)} {
		next, err := b.request("muse_converse", request)
		if err != nil || next["sent_message_id"] != first["sent_message_id"] || f.sends != 1 {
			t.Fatalf("repeated dispatch: %v %v", next, err)
		}
	}
	input["text"] = "changed"
	if _, err = b.request("muse_converse", input); errorCode(err) != "exchange_conflict" {
		t.Fatalf("changed body: %v", err)
	}
	input["text"] = "Ask Muse this"
	input["grant_id"] = 8
	if _, err = b.request("muse_converse", input); errorCode(err) != "exchange_conflict" {
		t.Fatalf("changed grant: %v", err)
	}
	input["account_id"] = "other"
	if _, err = b.request("muse_converse", input); errorCode(err) != "connection_changed" {
		t.Fatalf("changed account: %v", err)
	}
	if f.sends != 1 {
		t.Fatal("identity conflict sent")
	}
}

func TestMuseUncertainDispatchTimeoutAndDisconnectNeverReplay(t *testing.T) {
	for _, disconnect := range []bool{false, true} {
		t.Run(map[bool]string{false: "timeout", true: "disconnect"}[disconnect], func(t *testing.T) {
			b, f := museBridge(t)
			b.sendTimeout = 5 * time.Millisecond
			f.sendError = errors.New("private provider content")
			f.onMuseSend = func(ctx context.Context) {
				if disconnect {
					f.Disconnect()
					b.handleEvent(&events.Disconnected{})
				}
				select {
				case <-ctx.Done():
				case <-time.After(time.Second):
					t.Fatal("send deadline or disconnect did not cancel")
				}
			}
			input := museInput(b)
			result, err := b.request("muse_converse", input)
			if err != nil || result["status"] != "outcome_unknown" || result["sent_message_id"] != nil {
				t.Fatalf("uncertain receipt: %v %v", result, err)
			}
			f.online = true
			b.state = "connected"
			result, err = b.request("muse_converse", input)
			if err != nil || result["status"] != "outcome_unknown" || f.sends != 1 {
				t.Fatalf("uncertain replay: %v %v", result, err)
			}
		})
	}
}

func TestMuseCorrelationRejectsPhoneInterferenceAndSettlesStream(t *testing.T) {
	b, f := museBridge(t)
	input := museInput(b)
	if _, err := b.request("muse_converse", input); err != nil {
		t.Fatal(err)
	}
	phone := museEvent("intervening-phone-prompt", "", "manual phone prompt")
	phone.Info.IsFromMe = true
	phone.Info.Sender, _ = types.ParseJID(b.account)
	if err := b.ingestMessage(phone, false); err != nil {
		t.Fatal(err)
	}
	for index, event := range []*events.Message{museEvent("root-only", "shared-wasa-root", "unrelated"), museEvent("unsolicited", "", "unrelated"), museEvent("phone-response", "phone-prompt", "unrelated"), museEvent("phone-prompt", f.id, "manual phone prompt"), museEvent("other-bot", f.id, "other bot"), museEvent("media", f.id, "")} {
		if index == 3 {
			event.Info.IsFromMe = true
			event.Info.Sender, _ = types.ParseJID(b.account)
		}
		if index == 4 {
			event.Info.Chat = types.MetaAIJID
			event.Info.Sender = types.MetaAIJID
		}
		if index == 5 {
			event.Message = &waE2E.Message{ImageMessage: &waE2E.ImageMessage{Caption: proto.String("private caption")}}
		}
		if err := b.ingestMessage(event, false); err != nil {
			t.Fatal(err)
		}
	}
	result, err := b.request("muse_converse", museResume(input))
	if err != nil || result["status"] != "pending" || len(result["replies"].([]map[string]any)) != 0 {
		t.Fatalf("unrelated message matched: %v %v", result, err)
	}
	event := museEvent("reply", f.id, "first chunk")
	event.Info.MsgBotInfo.EditType = types.EditTypeFirst
	if err = b.ingestMessage(event, false); err != nil {
		t.Fatal(err)
	}
	result, err = b.request("muse_converse", museResume(input))
	if err != nil || result["status"] != "pending" {
		t.Fatalf("first prematurely settled: %v %v", result, err)
	}
	event = museEvent("chunk-2", "shared-wasa-root", "complete answer")
	event.Info.MsgBotInfo = types.MsgBotInfo{EditType: types.EditTypeLast, EditTargetID: "reply"}
	if err = b.ingestMessage(event, false); err != nil {
		t.Fatal(err)
	}
	result, err = b.request("muse_converse", museResume(input))
	replies := result["replies"].([]map[string]any)
	if err != nil || result["status"] != "replied" || len(replies) != 1 || replies[0]["message_id"] != "reply" || replies[0]["text"] != "complete answer" {
		t.Fatalf("stream not consolidated: %v %v", result, err)
	}
	if err = b.cache.Delete(types.MuseJID.String(), "reply"); err != nil {
		t.Fatal(err)
	}
	result, err = b.request("muse_converse", museResume(input))
	if err != nil || len(result["replies"].([]map[string]any)) != 0 {
		t.Fatal("deleted text retained in receipt")
	}
}

func TestMuseNativeParentAndQuietSettlement(t *testing.T) {
	b, f := museBridge(t)
	input := museInput(b)
	if _, err := b.request("muse_converse", input); err != nil {
		t.Fatal(err)
	}
	event := museEvent("reply", "shared-root", "answer")
	event.Message.MessageContextInfo = &waE2E.MessageContextInfo{MessageAssociation: &waE2E.MessageAssociation{ParentMessageKey: &waCommon.MessageKey{ID: proto.String(f.id), RemoteJID: proto.String(types.MuseJID.String())}}}
	if err := b.ingestMessage(event, false); err != nil {
		t.Fatal(err)
	}
	result, err := b.request("muse_converse", museResume(input))
	if err != nil || result["status"] != "pending" {
		t.Fatal("unmarked stream skipped quiet settle")
	}
	now := time.Now().Add(2 * time.Second)
	b.cache.Now = func() time.Time { return now }
	result, err = b.request("muse_converse", museResume(input))
	if err != nil || result["status"] != "replied" {
		t.Fatalf("quiet settle failed: %v %v", result, err)
	}
}

func TestMuseSharedRootIsNeverAncestryAndOuterParentIsRetained(t *testing.T) {
	b, f := museBridge(t)
	input := museInput(b)
	if _, err := b.request("muse_converse", input); err != nil {
		t.Fatal(err)
	}
	// A later phone prompt rules out the conservative sole-prompt fallback;
	// only exact ancestry may associate the subsequent answer with Assistant.
	phone := museEvent("intervening-phone-prompt", "", "manual phone prompt")
	phone.Info.IsFromMe = true
	phone.Info.Sender, _ = types.ParseJID(b.account)
	if err := b.ingestMessage(phone, false); err != nil {
		t.Fatal(err)
	}
	// Deliberately collide the root reference with a tracked ID: roots still
	// convey no authority to claim phone-generated or unsolicited responses.
	b.device.ChatSettings = &museRootStore{id: f.id}
	event := museEvent("root-only", f.id, "unrelated")
	if err := b.ingestMessage(event, false); err != nil {
		t.Fatal(err)
	}
	result, err := b.request("muse_converse", museResume(input))
	if err != nil || len(result["replies"].([]map[string]any)) != 0 || !b.lastMuseCorrelation["target_is_wasa_root"] {
		t.Fatal("root used as per-prompt ancestry")
	}
	event = museEvent("outer-parent", f.id, "")
	event.Info.MsgBotInfo.EditType = types.EditTypeFull
	event.Message = &waE2E.Message{BotForwardedMessage: &waE2E.FutureProofMessage{Message: &waE2E.Message{Conversation: proto.String("answer")}}, MessageContextInfo: &waE2E.MessageContextInfo{MessageAssociation: &waE2E.MessageAssociation{ParentMessageKey: &waCommon.MessageKey{ID: proto.String(f.id), RemoteJID: proto.String(types.MuseJID.String())}}}}
	if err = b.ingestMessage(event, false); err != nil {
		t.Fatal(err)
	}
	result, err = b.request("muse_converse", museResume(input))
	if err != nil || result["status"] != "replied" || len(result["replies"].([]map[string]any)) != 1 {
		t.Fatalf("outer native parent lost: %v %v", result, err)
	}
}

func TestMuseWaitAllowsIncomingCallbackAndNativeQuote(t *testing.T) {
	b, f := museBridge(t)
	body := "retained text"
	if err := b.cache.PutMessage(cache.Message{ID: "quote", ChatID: types.MuseJID.String(), SenderID: types.MuseJID.String(), Text: &body, Type: "text", Timestamp: time.Now()}, false); err != nil {
		t.Fatal(err)
	}
	input := museInput(b)
	input["reply_to_message_id"] = "quote"
	input["wait_seconds"] = 2
	f.onMuseSend = func(context.Context) {
		if ctx := f.lastMessage.GetExtendedTextMessage().GetContextInfo(); ctx.GetStanzaID() != "quote" || ctx.GetQuotedMessage().GetConversation() != body {
			t.Error("missing native quote")
		}
		go func() {
			time.Sleep(20 * time.Millisecond)
			e := museEvent("answer", f.id, "reply")
			e.Info.MsgBotInfo.EditType = types.EditTypeFull
			b.handleEvent(e)
		}()
	}
	result, err := b.request("muse_converse", input)
	if err != nil || result["status"] != "replied" {
		t.Fatalf("callback blocked by wait: %v %v", result, err)
	}
	if err = b.cache.Delete(types.MuseJID.String(), "quote"); err != nil {
		t.Fatal(err)
	}
	input["wait_seconds"] = 0
	if _, err = b.request("muse_converse", input); err != nil || f.sends != 1 {
		t.Fatal("duplicate receipt depends on deleted quote")
	}
}

func TestMuseClearCancelLogoutAndAccountBarriers(t *testing.T) {
	for _, action := range []string{"clear", "cancel", "logout", "account"} {
		t.Run(action, func(t *testing.T) {
			b, f := museBridge(t)
			input := museInput(b)
			if _, err := b.request("muse_converse", input); err != nil {
				t.Fatal(err)
			}
			switch action {
			case "clear":
				if err := b.cache.Clear(); err != nil {
					t.Fatal(err)
				}
			case "cancel":
				if _, err := b.request("muse_cancel", map[string]any{"grant_id": 7}); err != nil {
					t.Fatal(err)
				}
			case "logout":
				b.handleEvent(&events.LoggedOut{})
				b.state = "connected"
				b.manualStop = false
			case "account":
				if err := b.cache.SetMeta("account_id", b.account); err != nil {
					t.Fatal(err)
				}
				if err := b.cache.ResetAccount(); err != nil {
					t.Fatal(err)
				}
			}
			result, err := b.request("muse_converse", input)
			if err != nil || result["status"] != "cancelled" || len(result["replies"].([]map[string]any)) != 0 || f.sends != 1 {
				t.Fatalf("barrier replayed: %v %v", result, err)
			}
			if action != "clear" {
				input["exchange_id"] = strings.Repeat("b", 64)
				if _, err = b.request("muse_converse", input); errorCode(err) != "grant_revoked" {
					t.Fatalf("old grant accepted: %v", err)
				}
			}
		})
	}
}

func TestMuseNativeEditsSettleMatchedStream(t *testing.T) {
	for _, kind := range []string{"protocol_last", "protocol_quiet", "generic_last", "generic_quiet"} {
		t.Run(kind, func(t *testing.T) {
			b, f := museBridge(t)
			now := time.Now()
			b.cache.Now = func() time.Time { return now }
			input := museInput(b)
			if _, err := b.request("muse_converse", input); err != nil {
				t.Fatal(err)
			}
			initial := museEvent("reply", f.id, "initial fragment")
			initial.Info.MsgBotInfo.EditType = types.EditTypeFirst
			if err := b.ingestMessage(initial, false); err != nil {
				t.Fatal(err)
			}
			now = now.Add(5 * time.Second)
			result, err := b.request("muse_converse", museResume(input))
			if err != nil || result["status"] != "pending" {
				t.Fatalf("initial marked fragment settled early: %v %v", result, err)
			}
			edited := &waE2E.Message{RichResponseMessage: &waE2E.AIRichResponseMessage{Submessages: []*waAICommonDeprecated.AIRichResponseSubMessage{{MessageText: proto.String("final rich response")}}}}
			event := museEvent("reply", "shared-wasa-root", "")
			if strings.HasSuffix(kind, "last") {
				event.Info.MsgBotInfo.EditType = types.EditTypeLast
			}
			if strings.HasPrefix(kind, "protocol") {
				event.Info.ID = "edit-envelope"
				event.Message = &waE2E.Message{ProtocolMessage: &waE2E.ProtocolMessage{Type: waE2E.ProtocolMessage_MESSAGE_EDIT.Enum(), Key: &waCommon.MessageKey{ID: proto.String("reply"), RemoteJID: proto.String(types.MuseJID.String())}, EditedMessage: edited}}
			} else {
				event.IsEdit = true
				event.Message = edited
			}
			if err = b.ingestMessage(event, false); err != nil {
				t.Fatal(err)
			}
			if event.Info.MsgBotInfo.EditTargetID != "" {
				t.Fatal("edit association mutated provider event")
			}
			result, err = b.request("muse_converse", museResume(input))
			if err != nil {
				t.Fatal(err)
			}
			if strings.HasSuffix(kind, "quiet") {
				if result["status"] != "pending" {
					t.Fatal("unmarked final edit skipped quiet settlement")
				}
				now = now.Add(2 * time.Second)
				result, err = b.request("muse_converse", museResume(input))
			}
			replies := result["replies"].([]map[string]any)
			if err != nil || result["status"] != "replied" || len(replies) != 1 || replies[0]["message_id"] != "reply" || replies[0]["text"] != "final rich response" {
				t.Fatalf("native edit failed to settle one reply: %v %v", result, err)
			}
		})
	}
}

func TestMuseHistoricalProtocolEditDoesNotSettleLiveStream(t *testing.T) {
	b, f := museBridge(t)
	input := museInput(b)
	if _, err := b.request("muse_converse", input); err != nil {
		t.Fatal(err)
	}
	event := museEvent("reply", f.id, "initial")
	event.Info.MsgBotInfo.EditType = types.EditTypeFirst
	if err := b.ingestMessage(event, false); err != nil {
		t.Fatal(err)
	}
	event = museEvent("historical-edit", f.id, "")
	event.Info.MsgBotInfo.EditType = types.EditTypeLast
	event.Message = &waE2E.Message{ProtocolMessage: &waE2E.ProtocolMessage{Type: waE2E.ProtocolMessage_MESSAGE_EDIT.Enum(), Key: &waCommon.MessageKey{ID: proto.String("reply")}, EditedMessage: &waE2E.Message{Conversation: proto.String("historical")}}}
	if err := b.ingestMessage(event, true); err != nil {
		t.Fatal(err)
	}
	result, err := b.request("muse_converse", museResume(input))
	if err != nil || result["status"] != "pending" {
		t.Fatalf("historical edit advanced live exchange: %v %v", result, err)
	}
}
