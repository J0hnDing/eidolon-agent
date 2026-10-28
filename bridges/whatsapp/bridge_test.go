package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"eidolon/whatsapp/internal/cache"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waAICommon"
	"go.mau.fi/whatsmeow/proto/waAICommonDeprecated"
	"go.mau.fi/whatsmeow/proto/waAdv"
	"go.mau.fi/whatsmeow/proto/waCommon"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/proto/waHistorySync"
	"go.mau.fi/whatsmeow/proto/waWeb"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/proto"
)

type fakeSocket struct {
	mu                       sync.Mutex
	online                   bool
	connects, sends, logouts int
	peers                    int
	sendError, logoutError   error
	lastMessage              *waE2E.Message
	onSend                   func(context.Context) error
}

func (f *fakeSocket) ConnectContext(context.Context) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.connects++
	f.online = true
	return nil
}
func (f *fakeSocket) Disconnect()       { f.mu.Lock(); defer f.mu.Unlock(); f.online = false }
func (f *fakeSocket) IsConnected() bool { f.mu.Lock(); defer f.mu.Unlock(); return f.online }
func (f *fakeSocket) IsLoggedIn() bool  { return f.IsConnected() }
func (f *fakeSocket) GetQRChannel(context.Context) (<-chan whatsmeow.QRChannelItem, error) {
	c := make(chan whatsmeow.QRChannelItem, 1)
	c <- whatsmeow.QRChannelItem{Event: "code", Code: "sensitive-qr-code"}
	close(c)
	return c, nil
}
func (f *fakeSocket) Logout(context.Context) error { f.logouts++; return f.logoutError }
func (f *fakeSocket) SendMessage(ctx context.Context, _ types.JID, m *waE2E.Message, _ ...whatsmeow.SendRequestExtra) (whatsmeow.SendResponse, error) {
	f.sends++
	f.lastMessage = m
	if f.onSend != nil {
		return whatsmeow.SendResponse{}, f.onSend(ctx)
	}
	return whatsmeow.SendResponse{ID: "sent-id", Timestamp: time.Now()}, f.sendError
}
func (f *fakeSocket) GetBotListV2(context.Context) ([]types.BotListInfo, error) {
	return []types.BotListInfo{{BotJID: types.MetaAIJID, PersonaID: "directory-persona"}}, nil
}
func (f *fakeSocket) SendPeerMessage(context.Context, *waE2E.Message) (whatsmeow.SendResponse, error) {
	f.peers++
	return whatsmeow.SendResponse{}, nil
}
func (f *fakeSocket) BuildHistorySyncRequest(*types.MessageInfo, int) *waE2E.Message {
	return &waE2E.Message{}
}
func (f *fakeSocket) BuildUnavailableMessageRequest(types.JID, types.JID, types.MessageID) *waE2E.Message {
	return &waE2E.Message{}
}
func (f *fakeSocket) ParseWebMessage(types.JID, *waWeb.WebMessageInfo) (*events.Message, error) {
	return nil, errors.New("participant missing")
}

func testBridge(t *testing.T) (*bridge, *fakeSocket) {
	t.Helper()
	c, err := cache.Open(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	b := newBridge(nil)
	f := &fakeSocket{online: true}
	b.cache = c
	b.client = f
	b.account = "15551234567@s.whatsapp.net"
	b.state = "connected"
	b.device = &store.Device{}
	t.Cleanup(b.close)
	return b, f
}
func sendInput() map[string]any {
	return map[string]any{"operation": "whatsapp.message.send", "account_id": "15551234567@s.whatsapp.net", "dispatch_id": "approval:123", "input": map[string]any{"recipient": "+15557654321", "text": "complete private text"}}
}

func TestSendRequiresExactLiveAccountAndDurableClaim(t *testing.T) {
	b, f := testBridge(t)
	input := sendInput()
	input["account_id"] = "other-account"
	if _, err := b.request("execute", input); errorCode(err) != "connection_changed" {
		t.Fatalf("wrong account: %v", err)
	}
	if f.sends != 0 {
		t.Fatal("dispatched before account validation")
	}
	input["account_id"] = b.account
	result, err := b.request("execute", input)
	if err != nil || result["message_id"] != "sent-id" {
		t.Fatalf("send: %v %v", result, err)
	}
	again, err := b.request("execute", input)
	if err != nil || again["message_id"] != "sent-id" || f.sends != 1 {
		t.Fatal("accepted dispatch repeated")
	}
	input["input"].(map[string]any)["text"] = "changed"
	if _, err = b.request("execute", input); errorCode(err) != "dispatch_conflict" || f.sends != 1 {
		t.Fatal("claim was reused for different content")
	}
}

func TestUncertainSendNeverRepeats(t *testing.T) {
	b, f := testBridge(t)
	f.sendError = errors.New("provider-secret-content")
	for range 2 {
		_, err := b.request("execute", sendInput())
		if errorCode(err) != "outcome_unknown" {
			t.Fatalf("uncertain: %v", err)
		}
	}
	if f.sends != 1 {
		t.Fatal("uncertain send replayed")
	}
}

func TestDisconnectCancelsUncertainSendBeforeReconnect(t *testing.T) {
	b, f := testBridge(t)
	account, _ := types.ParseJID(b.account)
	b.device.ID = &account
	f.onSend = func(ctx context.Context) error {
		f.Disconnect()
		b.handleEvent(&events.Disconnected{})
		if b.state == "reconnecting" {
			t.Fatal("reconnected during uncertain send")
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(time.Second):
			t.Fatal("send context was not cancelled")
			return nil
		}
	}
	if _, err := b.request("execute", sendInput()); errorCode(err) != "outcome_unknown" {
		t.Fatalf("send outcome: %v", err)
	}
	if f.sends != 1 || f.connects != 0 {
		t.Fatal("uncertain send was retried")
	}
	if b.state != "reconnecting" {
		t.Fatal("connection recovery did not resume after send ended")
	}
}

func TestSendValidationAndSameChatQuote(t *testing.T) {
	b, f := testBridge(t)
	for _, change := range []map[string]any{{"recipient": types.MuseJID.String()}, {"recipient": "status@broadcast"}, {"recipient": "recipient-name"}, {"text": " "}, {"text": strings.Repeat("x", 4001)}, {"reply_to_message_id": "missing"}} {
		input := sendInput()
		for key, value := range change {
			input["input"].(map[string]any)[key] = value
		}
		if _, err := b.request("execute", input); err == nil {
			t.Fatalf("allowed malformed send: %v", change)
		}
	}
	if f.sends != 0 {
		t.Fatal("malformed send reached network")
	}
	text := "quoted text"
	if err := b.cache.PutMessage(cache.Message{ID: "quote", ChatID: "15557654321@s.whatsapp.net", SenderID: "15557654321@s.whatsapp.net", Timestamp: time.Now(), Text: &text, Type: "text"}, false); err != nil {
		t.Fatal(err)
	}
	input := sendInput()
	input["input"].(map[string]any)["reply_to_message_id"] = "quote"
	if _, err := b.request("execute", input); err != nil {
		t.Fatal(err)
	}
	if f.lastMessage.GetExtendedTextMessage().GetContextInfo().GetStanzaID() != "quote" {
		t.Fatal("quote missing")
	}
}

func TestMuseIncomingAndStreamingEditsAreCachedWithoutSend(t *testing.T) {
	b, f := testBridge(t)
	e := &events.Message{Info: types.MessageInfo{MessageSource: types.MessageSource{Chat: types.MuseJID, Sender: types.MuseJID}, ID: "reply", Timestamp: time.Now()}, Message: &waE2E.Message{Conversation: proto.String("Hello")}}
	b.handleEvent(e)
	e.Info.ID = "fragment"
	e.Info.MsgBotInfo.EditTargetID = "reply"
	e.Message.Conversation = proto.String("Hello from Muse")
	b.handleEvent(e)
	result, err := b.cache.Read("whatsapp.message.list", map[string]any{"chat_id": types.MuseJID.String()})
	if err != nil {
		t.Fatal(err)
	}
	data, _ := json.Marshal(result)
	if !bytes.Contains(data, []byte("Hello from Muse")) || bytes.Contains(data, []byte("\"id\":\"fragment\"")) {
		t.Fatalf("Muse cache: %s", data)
	}
	if f.sends != 0 {
		t.Fatal("incoming message started outgoing interaction")
	}
	counts, _ := b.cache.Counts()
	if counts["messages"] != int64(1) {
		t.Fatalf("duplicate stream: %v", counts)
	}
	e.Info.MsgBotInfo.EditTargetID = ""
	e.Info.ID = "reply"
	e.Message.Conversation = proto.String("historical initial fragment")
	if err := b.ingestMessage(e, true); err != nil {
		t.Fatal(err)
	}
	retained, err := b.cache.Quote(types.MuseJID.String(), "reply")
	if err != nil || *retained.Text != "Hello from Muse" {
		t.Fatal("history reverted streamed reply")
	}
	e.Info.MsgBotInfo.EditTargetID = "reply"
	e.Info.ID = "historical-stream"
	e.Message.Conversation = proto.String("stale streamed edit")
	if err = b.ingestMessage(e, true); err != nil {
		t.Fatal(err)
	}
	e.Info.MsgBotInfo.EditTargetID = ""
	e.Message = &waE2E.Message{ProtocolMessage: &waE2E.ProtocolMessage{Type: waE2E.ProtocolMessage_MESSAGE_EDIT.Enum(), Key: &waCommon.MessageKey{ID: proto.String("reply")}, EditedMessage: &waE2E.Message{Conversation: proto.String("stale protocol edit")}}}
	if err = b.ingestMessage(e, true); err != nil {
		t.Fatal(err)
	}
	retained, err = b.cache.Quote(types.MuseJID.String(), "reply")
	if err != nil || *retained.Text != "Hello from Muse" {
		t.Fatal("historical edit replaced latest live edit")
	}
}

func TestRichBotTextCachesOnlyTextParts(t *testing.T) {
	b, _ := testBridge(t)
	b.handleEvent(&events.Message{Info: types.MessageInfo{MessageSource: types.MessageSource{Chat: types.MuseJID, Sender: types.MuseJID}, ID: "rich", Timestamp: time.Now()},
		Message: &waE2E.Message{RichResponseMessage: &waE2E.AIRichResponseMessage{Submessages: []*waAICommonDeprecated.AIRichResponseSubMessage{
			{MessageText: proto.String("Readable text")}, {ImageMetadata: &waAICommonDeprecated.AIRichResponseInlineImageMetadata{ImageText: proto.String("Excluded media metadata")}},
		}}}})
	message, err := b.cache.Quote(types.MuseJID.String(), "rich")
	if err != nil || *message.Text != "Readable text" {
		t.Fatal("rich text not projected correctly")
	}
	diagnostics, _ := b.diagnostics()
	encoded, _ := json.Marshal(diagnostics)
	if bytes.Contains(encoded, []byte("Readable text")) || bytes.Contains(encoded, []byte("Excluded media metadata")) {
		t.Fatal("diagnostics exposed content")
	}
}

func TestBotRecoveryAcceptsOnlyRequestedMessageFromOwnPhone(t *testing.T) {
	b, f := testBridge(t)
	chat := types.MuseJID.String()
	stamp := time.Now().Truncate(time.Second)
	if err := b.cache.PutMessage(cache.Message{ID: "recover", ChatID: chat, SenderID: chat, Timestamp: stamp, Type: "unsupported"}, false); err != nil {
		t.Fatal(err)
	}
	result, err := b.request("sync_chat", map[string]any{"chat_id": chat})
	if err != nil || result["maximum_messages"] != 1 || f.peers != 1 || f.sends != 0 {
		t.Fatal("incorrect setup recovery request")
	}
	b.handleEvent(&events.Message{Info: types.MessageInfo{MessageSource: types.MessageSource{Chat: types.MuseJID, Sender: types.MuseJID}, ID: "recover", Timestamp: stamp}, Message: &waE2E.Message{Conversation: proto.String("Unchecked auto-parsed copy")}, UnavailableRequestID: "peer-recovery"})
	if _, err = b.cache.Quote(chat, "recover"); err == nil {
		t.Fatal("auto-parsed recovery bypassed trusted outer response")
	}
	body := &waE2E.Message{BotForwardedMessage: &waE2E.FutureProofMessage{Message: &waE2E.Message{RichResponseMessage: &waE2E.AIRichResponseMessage{UnifiedResponse: &waAICommon.AIRichResponseUnifiedResponse{Data: []byte(`{"sections":[{"view_model":{"__typename":"GenAISingleLayoutViewModel","primitive":{"__typename":"GenAIMarkdownTextUXPrimitive","text":"Recovered Muse reply"}}}]}`)}}}}}
	web := &waWeb.WebMessageInfo{Key: &waCommon.MessageKey{ID: proto.String("recover"), RemoteJID: &chat}, MessageTimestamp: proto.Uint64(uint64(stamp.Unix())), Message: body}
	data, _ := proto.Marshal(web)
	response := &waE2E.Message{ProtocolMessage: &waE2E.ProtocolMessage{Type: waE2E.ProtocolMessage_PEER_DATA_OPERATION_REQUEST_RESPONSE_MESSAGE.Enum(), PeerDataOperationRequestResponseMessage: &waE2E.PeerDataOperationRequestResponseMessage{
		PeerDataOperationRequestType: waE2E.PeerDataOperationRequestType_PLACEHOLDER_MESSAGE_RESEND.Enum(),
		PeerDataOperationResult:      []*waE2E.PeerDataOperationRequestResponseMessage_PeerDataOperationResult{{PlaceholderMessageResendResponse: &waE2E.PeerDataOperationRequestResponseMessage_PeerDataOperationResult_PlaceholderMessageResendResponse{WebMessageInfoBytes: data}}},
	}}}
	account, _ := types.ParseJID(b.account)
	event := &events.Message{Info: types.MessageInfo{MessageSource: types.MessageSource{Chat: account, Sender: account}}, Message: response}
	b.handleEvent(event)
	if _, err = b.cache.Quote(chat, "recover"); err == nil {
		t.Fatal("accepted response not from own phone")
	}
	event.Info.IsFromMe = true
	b.handleEvent(event)
	reply, err := b.cache.Quote(chat, "recover")
	if err != nil || *reply.Text != "Recovered Muse reply" {
		t.Fatal("requested reply not recovered")
	}
	if err = b.cache.Clear(); err != nil {
		t.Fatal(err)
	}
	b.handleEvent(event)
	counts, _ := b.cache.Counts()
	if counts["messages"] != int64(0) {
		t.Fatal("unsolicited replay restored cleared content")
	}
	if _, err = b.request("sync_chat", map[string]any{"chat_id": chat}); errorCode(err) != "cache_cleared" {
		t.Fatal("recovery bypassed clear barrier")
	}
}

func TestViewOnceAndUnknownEphemeralExcludedAndEditRevokeApplied(t *testing.T) {
	b, _ := testBridge(t)
	e := &events.Message{Info: types.MessageInfo{MessageSource: types.MessageSource{Chat: types.MuseJID, Sender: types.MuseJID}, ID: "reply", Timestamp: time.Now()}, Message: &waE2E.Message{Conversation: proto.String("private")}, IsViewOnce: true}
	b.handleEvent(e)
	e.IsViewOnce = false
	e.IsEphemeral = true
	b.handleEvent(e)
	counts, _ := b.cache.Counts()
	if counts["messages"] != int64(0) {
		t.Fatal("excluded content retained")
	}
	e.IsEphemeral = false
	b.handleEvent(e)
	e.Message = &waE2E.Message{ProtocolMessage: &waE2E.ProtocolMessage{Type: waE2E.ProtocolMessage_MESSAGE_EDIT.Enum(), Key: &waCommon.MessageKey{ID: proto.String("reply")}, EditedMessage: &waE2E.Message{Conversation: proto.String("edited")}}}
	b.handleEvent(e)
	q, err := b.cache.Quote(types.MuseJID.String(), "reply")
	if err != nil || *q.Text != "edited" {
		t.Fatal("edit not applied")
	}
	e.Message.ProtocolMessage.Type = waE2E.ProtocolMessage_REVOKE.Enum()
	b.handleEvent(e)
	if _, err = b.cache.Quote(types.MuseJID.String(), "reply"); err == nil {
		t.Fatal("revoked retained")
	}
}

func TestHistoryMuseFallbackAndProgress(t *testing.T) {
	b, _ := testBridge(t)
	h := &events.HistorySync{Data: &waHistorySync.HistorySync{SyncType: waHistorySync.HistorySync_RECENT.Enum(), Progress: proto.Uint32(100), Conversations: []*waHistorySync.Conversation{{ID: proto.String(types.MuseJID.String()), Messages: []*waHistorySync.HistorySyncMsg{{Message: &waWeb.WebMessageInfo{Key: &waCommon.MessageKey{ID: proto.String("history-reply")}, MessageTimestamp: proto.Uint64(uint64(time.Now().Unix())), Message: &waE2E.Message{Conversation: proto.String("Muse supplied history")}}}}}}}}
	b.handleEvent(h)
	if _, err := b.cache.Quote(types.MuseJID.String(), "history-reply"); err != nil {
		t.Fatal(err)
	}
	if b.cache.Meta("sync_status") != "complete" {
		t.Fatal("supplied phase incomplete")
	}
	h.Data.SyncType = waHistorySync.HistorySync_PUSH_NAME.Enum()
	h.Data.Progress = nil
	b.handleEvent(h)
	if b.cache.Meta("sync_status") != "complete" {
		t.Fatal("pushname changed content state")
	}
}

func TestInitializeRequiresRelinkForLegacyAndDoesNotConnect(t *testing.T) {
	directory := t.TempDir()
	c, err := cache.Open(directory)
	if err != nil {
		t.Fatal(err)
	}
	_ = c.SetMeta("account_id", "15551234567@s.whatsapp.net")
	_ = c.Close()
	b := newBridge(nil)
	f := &fakeSocket{}
	b.newSocket = func(*store.Device) socket { return f }
	defer b.close()
	result, err := b.request("initialize", map[string]any{"directory": directory, "generation": "test", "key": base64.StdEncoding.EncodeToString(bytes.Repeat([]byte{1}, 32))})
	if err != nil || result["status"] != "relink_required" || result["connected"] != false || f.connects != 0 {
		t.Fatalf("legacy: %v %v", result, err)
	}
}

func TestLogoutPreventsReconnectAndReportsRemoteFailure(t *testing.T) {
	b, f := testBridge(t)
	b.handleEvent(&events.LoggedOut{})
	b.scheduleReconnect()
	if b.status()["status"] != "logged_out" || f.connects != 0 {
		t.Fatal("logout retried")
	}
	if _, err := b.reconnect(); errorCode(err) != "relink_required" {
		t.Fatal("logout reconnect allowed")
	}
	// No authenticated device in this fixture, so no remote logout is attempted.
	result, err := b.disconnect()
	if err != nil || result["remote_logout_failed"] != false {
		t.Fatalf("disconnect: %v %v", result, err)
	}
}

func TestAuthEncryptsAndRestoresSignalAndWASARoot(t *testing.T) {
	dir := t.TempDir()
	key := bytes.Repeat([]byte{7}, 32)
	ctx := context.Background()
	a, err := openAuth(ctx, dir, key)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = a.Close() })
	d := a.NewDevice()
	jid := types.NewJID("15551234567", types.DefaultUserServer)
	d.ID = &jid
	d.LID = types.NewJID("999999999999", types.HiddenUserServer)
	d.Account = &waAdv.ADVSignedDeviceIdentity{Details: []byte{1}, AccountSignature: make([]byte, 64),
		AccountSignatureKey: make([]byte, 32), DeviceSignature: make([]byte, 64)}
	if err = d.Save(ctx); err != nil {
		t.Fatal(err)
	}
	noise := append([]byte{}, d.NoiseKey.Priv[:]...)
	secret := bytes.Repeat([]byte("never-plaintext-secret"), 3)
	if err = d.Sessions.PutSession(ctx, "15557654321.0", secret); err != nil {
		t.Fatal(err)
	}
	if err = d.MsgSecrets.PutMessageSecret(ctx, types.MuseJID, d.LID, "wasa-root", secret); err != nil {
		t.Fatal(err)
	}
	if err = d.ChatSettings.PutWASARootSecretID(ctx, types.MuseJID, "wasa-root"); err != nil {
		t.Fatal(err)
	}
	if err = a.Close(); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(filepath.Join(dir, "whatsmeow-auth.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Contains(data, noise) || bytes.Contains(data, secret) || bytes.Contains(data, []byte("SQLite format 3")) {
		t.Fatal("authentication database contains plaintext")
	}
	a, err = openAuth(ctx, dir, key)
	if err != nil {
		t.Fatal(err)
	}
	d, err = a.GetFirstDevice(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(noise, d.NoiseKey.Priv[:]) {
		t.Fatal("identity changed")
	}
	got, err := d.Sessions.GetSession(ctx, "15557654321.0")
	if err != nil || !bytes.Equal(got, secret) {
		t.Fatal("signal session not restored")
	}
	root, err := d.ChatSettings.GetWASARootSecretID(ctx, types.MuseJID)
	if err != nil || root != "wasa-root" {
		t.Fatal("WASA root reference not restored")
	}
	got, _, err = d.MsgSecrets.GetMessageSecret(ctx, types.MuseJID, d.LID, root)
	if err != nil || !bytes.Equal(got, secret) {
		t.Fatal("WASA secret not restored")
	}
	_ = a.Close()
	if wrong, err := openAuth(ctx, dir, bytes.Repeat([]byte{9}, 32)); err == nil {
		_ = wrong.Close()
		t.Fatal("wrong key accepted")
	}
}

func TestFramingRejectsPartialOversizedAndRedactsErrors(t *testing.T) {
	for _, input := range []string{"{\"id\":1,\"method\":\"status\",\"input\":{}}", strings.Repeat("x", maxFrame) + "\n"} {
		var out bytes.Buffer
		serve(strings.NewReader(input), &out)
		if out.Len() != 0 {
			t.Fatal("incomplete/oversized frame executed")
		}
	}
	var out bytes.Buffer
	serve(strings.NewReader("{\"id\":\"test\",\"method\":\"initialize\",\"input\":{\"key\":\"PRIVATE_CONTENT\"}}\n"), &out)
	if !strings.Contains(out.String(), "invalid_input") || strings.Contains(out.String(), "PRIVATE_CONTENT") {
		t.Fatalf("redaction: %s", out.String())
	}
	if errorCode(errors.New("private-provider-error")) != "internal_failure" {
		t.Fatal("provider error leaked")
	}
}
