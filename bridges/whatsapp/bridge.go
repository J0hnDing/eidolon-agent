package main

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	"eidolon/whatsapp/internal/cache"
	"github.com/skip2/go-qrcode"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/proto/waWeb"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
)

type socket interface {
	ConnectContext(context.Context) error
	Disconnect()
	IsConnected() bool
	IsLoggedIn() bool
	GetQRChannel(context.Context) (<-chan whatsmeow.QRChannelItem, error)
	Logout(context.Context) error
	SendMessage(context.Context, types.JID, *waE2E.Message, ...whatsmeow.SendRequestExtra) (whatsmeow.SendResponse, error)
	SendPeerMessage(context.Context, *waE2E.Message) (whatsmeow.SendResponse, error)
	BuildHistorySyncRequest(*types.MessageInfo, int) *waE2E.Message
	BuildUnavailableMessageRequest(types.JID, types.JID, types.MessageID) *waE2E.Message
	GetBotListV2(context.Context) ([]types.BotListInfo, error)
	ParseWebMessage(types.JID, *waWeb.WebMessageInfo) (*events.Message, error)
}

type bridge struct {
	op                                                           sync.Mutex
	mu                                                           sync.Mutex
	ctx                                                          context.Context
	cancel                                                       context.CancelFunc
	emit                                                         func(map[string]any)
	cache                                                        *cache.Store
	auth                                                         *sqlstore.Container
	device                                                       *store.Device
	client                                                       socket
	directory                                                    string
	state, account, login, qr, errorType                         string
	closed, manualStop, connecting                               bool
	retries                                                      int
	epoch                                                        uint64
	historyAt                                                    time.Time
	historyBatches, receivedMessages, undecryptable, museReplies int
	lastBotFields                                                []string
	lastMuseCorrelation                                          map[string]bool
	recoveryRequests                                             map[string]time.Time
	contentProgress                                              map[string]uint32
	sendTimeout                                                  time.Duration
	sendCancel                                                   context.CancelFunc
	// Factories are replaceable by offline tests; live code always uses Whatsmeow.
	newSocket func(*store.Device) socket
}

func newBridge(emit func(map[string]any)) *bridge {
	ctx, cancel := context.WithCancel(context.Background())
	b := &bridge{ctx: ctx, cancel: cancel, emit: emit, state: "disconnected", sendTimeout: 20 * time.Second, contentProgress: map[string]uint32{}, recoveryRequests: map[string]time.Time{}}
	b.newSocket = func(d *store.Device) socket {
		c := whatsmeow.NewClient(d, waLog.Noop)
		c.EnableAutoReconnect = false // One bounded, observable reconnect owner.
		c.InitialAutoReconnect = false
		c.DisableLoginAutoReconnect = true
		c.EnableDecryptedEventBuffer = false
		c.UseRetryMessageStore = false
		// Dispatch each approved send once, including when a peer requests retry.
		c.PreRetryCallback = func(*events.Receipt, types.MessageID, int, *waE2E.Message) bool { return false }
		c.AddEventHandler(b.handleEvent)
		return c
	}
	return b
}

func str(input map[string]any, key string) string { v, _ := input[key].(string); return v }
func nullable(s string) any {
	if s == "" {
		return nil
	}
	return s
}
func iso(t time.Time) string { return t.UTC().Format("2006-01-02T15:04:05.000Z") }

func (b *bridge) initialize(input map[string]any) (map[string]any, error) {
	if b.cache != nil {
		return nil, fail("already_initialized")
	}
	key, err := base64.StdEncoding.Strict().DecodeString(str(input, "key"))
	if err != nil || len(key) != 32 {
		return nil, fail("invalid_input")
	}
	defer clear(key)
	directory := str(input, "directory")
	if !filepath.IsAbs(directory) || str(input, "generation") == "" {
		return nil, fail("invalid_input")
	}
	if err = os.MkdirAll(directory, 0700); err != nil {
		return nil, fail("storage_unavailable")
	}
	c, err := cache.Open(directory)
	if err != nil {
		return nil, fail("storage_unavailable")
	}
	if err = c.MuseInit(); err != nil {
		_ = c.Close()
		return nil, fail("storage_unavailable")
	}
	a, err := openAuth(b.ctx, directory, key)
	if err != nil {
		_ = c.Close()
		return nil, err
	}
	d, err := a.GetFirstDevice(b.ctx)
	if err != nil {
		_ = a.Close()
		_ = c.Close()
		return nil, fail("authentication_unavailable")
	}
	b.cache, b.auth, b.device, b.directory = c, a, d, directory
	b.account = c.Meta("account_id")
	b.login = c.Meta("account_login")
	if b.account != "" && d.ID == nil {
		b.state = "relink_required"
		b.errorType = "relink_required"
	}
	if c.Meta("whatsmeow_logged_out") == "1" {
		b.state = "logged_out"
		b.errorType = "relink_required"
		b.manualStop = true
	}
	if c.Meta("sync_status") == "syncing" {
		_ = c.SetMeta("sync_status", "partial")
	}
	b.client = b.newSocket(d)
	go b.maintenance()
	if d.ID != nil && b.state != "logged_out" {
		b.startConnect(false)
	}
	return b.status(), nil
}

func (b *bridge) status() map[string]any {
	b.mu.Lock()
	state, account, login, qr, code := b.state, b.account, b.login, b.qr, b.errorType
	c := b.cache
	if c != nil && !b.historyAt.IsZero() && time.Since(b.historyAt) > 2*time.Minute && c.Meta("sync_status") == "syncing" {
		_ = c.SetMeta("sync_status", "partial")
	}
	b.mu.Unlock()
	coverage := map[string]any{"retention_cutoff": iso(time.Now().Add(-30 * 24 * time.Hour)), "oldest_message_at": nil, "last_synced_at": nil, "sync_status": "pending"}
	if c != nil {
		if v, err := c.Coverage(); err == nil {
			coverage = v
		}
	}
	coverage["provider"] = "whatsapp"
	coverage["connected"] = state == "connected"
	coverage["status"] = state
	coverage["account_id"] = nullable(account)
	coverage["account_login"] = nullable(login)
	coverage["qr_data_url"] = nullable(qr)
	coverage["error_type"] = nullable(code)
	return coverage
}
func (b *bridge) notify() {
	if b.emit != nil {
		b.emit(b.status())
	}
}
func (b *bridge) maintenance() {
	t := time.NewTicker(time.Minute)
	defer t.Stop()
	for {
		select {
		case <-b.ctx.Done():
			return
		case <-t.C:
			if b.cache != nil {
				if b.cache.Prune() != nil {
					b.setError("storage_unavailable")
				}
			}
			b.notify()
		}
	}
}
func (b *bridge) setError(code string) { b.mu.Lock(); b.errorType = code; b.mu.Unlock() }

func (b *bridge) startConnect(pair bool) {
	b.mu.Lock()
	if b.closed || b.connecting {
		b.mu.Unlock()
		return
	}
	b.manualStop = false
	b.connecting = true
	b.errorType = ""
	b.qr = ""
	b.epoch++
	epoch, client := b.epoch, b.client
	if pair {
		b.state = "pairing"
	} else {
		b.state = "connecting"
	}
	b.mu.Unlock()
	go func() {
		var codes <-chan whatsmeow.QRChannelItem
		var err error
		if pair {
			codes, err = client.GetQRChannel(b.ctx)
		}
		// Whatsmeow uses this context for the socket's entire lifetime.
		if err == nil {
			err = client.ConnectContext(b.ctx)
		}
		b.mu.Lock()
		if b.epoch != epoch || b.closed {
			b.mu.Unlock()
			return
		}
		b.connecting = false
		if err != nil {
			b.qr = ""
			b.errorType = "provider_unavailable"
			b.state = "disconnected"
		}
		b.mu.Unlock()
		if err != nil {
			if !pair {
				b.scheduleReconnect()
			}
			b.notify()
			return
		}
		if pair {
			go b.readQR(epoch, codes)
		}
		b.notify()
	}()
}

func (b *bridge) readQR(epoch uint64, codes <-chan whatsmeow.QRChannelItem) {
	for {
		select {
		case <-b.ctx.Done():
			return
		case item, ok := <-codes:
			if !ok {
				return
			}
			b.mu.Lock()
			if b.closed || epoch != b.epoch {
				b.mu.Unlock()
				return
			}
			switch item.Event {
			case "code":
				png, err := qrcode.Encode(item.Code, qrcode.Medium, 320)
				if err == nil {
					b.qr = "data:image/png;base64," + base64.StdEncoding.EncodeToString(png)
					b.state = "pairing"
				}
			case "success":
				b.qr = ""
			default:
				b.qr = ""
				if b.state != "connected" {
					b.state = "disconnected"
					b.errorType = "pairing_failed"
				}
			}
			b.mu.Unlock()
			b.notify()
		}
	}
}

func (b *bridge) scheduleReconnect() {
	b.mu.Lock()
	if b.closed || b.manualStop || b.state == "logged_out" || b.device.ID == nil || b.connecting || b.state == "reconnecting" || b.sendCancel != nil {
		b.mu.Unlock()
		return
	}
	if b.retries >= 8 {
		b.state = "disconnected"
		b.errorType = "reconnect_exhausted"
		b.mu.Unlock()
		return
	}
	delay := time.Second * time.Duration(1<<min(b.retries, 5))
	b.retries++
	b.state = "reconnecting"
	epoch := b.epoch
	b.mu.Unlock()
	go func() {
		select {
		case <-b.ctx.Done():
			return
		case <-time.After(delay):
		}
		b.mu.Lock()
		valid := !b.closed && !b.manualStop && epoch == b.epoch && b.state == "reconnecting"
		b.mu.Unlock()
		if valid {
			b.startConnect(false)
		}
	}()
}

func (b *bridge) pair() (map[string]any, error) {
	b.mu.Lock()
	hasDevice := b.device.ID != nil
	state := b.state
	b.mu.Unlock()
	if hasDevice {
		if state == "logged_out" {
			return nil, fail("relink_required")
		}
		return b.reconnect()
	}
	if state == "pairing" {
		return b.status(), nil
	}
	b.startConnect(true)
	return b.status(), nil
}
func (b *bridge) reconnect() (map[string]any, error) {
	b.mu.Lock()
	if b.device.ID == nil || b.state == "logged_out" {
		b.mu.Unlock()
		return nil, fail("relink_required")
	}
	b.retries = 0
	if b.state == "connected" || b.connecting {
		b.mu.Unlock()
		return b.status(), nil
	}
	b.epoch++
	b.manualStop = true
	client := b.client
	b.mu.Unlock()
	client.Disconnect()
	b.startConnect(false)
	return b.status(), nil
}

func (b *bridge) connectedAccount() (socket, string, bool) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.client, b.account, b.state == "connected" && !b.manualStop && b.client != nil && b.client.IsConnected() && b.client.IsLoggedIn()
}

var recipientPattern = regexp.MustCompile(`^(?:[0-9]{5,20}@(s\.whatsapp\.net|lid)|[0-9]{5,20}(?:-[0-9]{1,20})?@g\.us)$`)
var phonePattern = regexp.MustCompile(`^\+[1-9][0-9]{6,14}$`)
var dispatchPattern = regexp.MustCompile(`^[a-zA-Z0-9:_-]{1,200}$`)

func (b *bridge) execute(input map[string]any) (map[string]any, error) {
	client, account, online := b.connectedAccount()
	if str(input, "account_id") == "" || str(input, "account_id") != account {
		return nil, fail("connection_changed")
	}
	if !online {
		return nil, fail("connection_unavailable")
	}
	op := str(input, "operation")
	args, ok := input["input"].(map[string]any)
	if !ok {
		return nil, fail("invalid_input")
	}
	if op != "whatsapp.message.send" {
		return b.cache.Read(op, args)
	}
	text, recipient, dispatch := str(args, "text"), str(args, "recipient"), str(input, "dispatch_id")
	if strings.TrimSpace(text) == "" || !utf8.ValidString(text) || utf8.RuneCountInString(text) > 4000 || !dispatchPattern.MatchString(dispatch) {
		return nil, fail("invalid_input")
	}
	if phonePattern.MatchString(recipient) {
		recipient = recipient[1:] + "@s.whatsapp.net"
	}
	if !recipientPattern.MatchString(recipient) {
		return nil, fail("invalid_input")
	}
	recipient = b.cache.Canonical(recipient)
	if !recipientPattern.MatchString(recipient) {
		return nil, fail("invalid_input")
	}
	to, err := types.ParseJID(recipient)
	if err != nil {
		return nil, fail("invalid_input")
	}
	msg := &waE2E.Message{Conversation: proto.String(text)}
	quoteID := str(args, "reply_to_message_id")
	if value, present := args["reply_to_message_id"]; present && (quoteID == "" || len(quoteID) > 256 || value == nil) {
		return nil, fail("invalid_input")
	}
	if quoteID != "" {
		quoted, err := b.cache.Quote(recipient, quoteID)
		if err != nil {
			return nil, err
		}
		if quoted.Text == nil || quoted.ChatID != recipient {
			return nil, fail("not_found")
		}
		msg = &waE2E.Message{ExtendedTextMessage: &waE2E.ExtendedTextMessage{Text: proto.String(text), ContextInfo: &waE2E.ContextInfo{
			StanzaID: proto.String(quoted.ID), Participant: proto.String(quoted.SenderID), RemoteJID: proto.String(recipient), QuotedMessage: &waE2E.Message{Conversation: quoted.Text},
		}}}
	}
	digestJSON, _ := json.Marshal([]any{account, args})
	digestBytes := sha256.Sum256(digestJSON)
	digest := hex.EncodeToString(digestBytes[:])
	saved, err := b.cache.Dispatch(dispatch, digest)
	if err != nil || saved != nil {
		return saved, err
	}
	// A durable claim is never replayed after this point, including timeouts.
	current, currentAccount, currentOnline := b.connectedAccount()
	if current != client || currentAccount != account || !currentOnline {
		return nil, fail("outcome_unknown")
	}
	ctx, cancel := context.WithTimeout(b.ctx, b.sendTimeout)
	b.mu.Lock()
	b.sendCancel = cancel
	b.mu.Unlock()
	defer func() {
		cancel()
		b.mu.Lock()
		b.sendCancel = nil
		offline := b.state != "connected"
		b.mu.Unlock()
		if offline {
			b.scheduleReconnect()
		}
	}()
	resp, err := client.SendMessage(ctx, to, msg, whatsmeow.SendRequestExtra{Timeout: b.sendTimeout})
	if err != nil || len(resp.ID) == 0 || len(resp.ID) > 256 {
		return nil, fail("outcome_unknown")
	}
	result := map[string]any{"accepted": true, "message_id": resp.ID, "chat_id": recipient}
	if err = b.cache.Accepted(dispatch, result); err != nil {
		return nil, fail("outcome_unknown")
	}
	timestamp := resp.Timestamp
	if timestamp.IsZero() {
		timestamp = time.Now()
	}
	_ = b.cache.PutMessage(cache.Message{ID: resp.ID, ChatID: recipient, SenderID: account, FromMe: true, Timestamp: timestamp, Text: &text, Type: "text", ReplyTo: quoteID}, false)
	return result, nil
}

func (b *bridge) disconnect() (map[string]any, error) {
	b.mu.Lock()
	b.manualStop = true
	b.epoch++
	b.state = "disconnecting"
	b.qr = ""
	client := b.client
	hasDevice := b.device.ID != nil
	b.mu.Unlock()
	failed, err := b.cache.LegacyAuthPresent()
	if err != nil {
		return nil, fail("cleanup_failed")
	}
	if hasDevice {
		ctx, cancel := context.WithTimeout(b.ctx, 5*time.Second)
		if client == nil || !client.IsConnected() || client.Logout(ctx) != nil {
			failed = true
		}
		cancel()
	}
	if client != nil {
		client.Disconnect()
	}
	if b.device.ID != nil {
		if err := b.device.Delete(b.ctx); err != nil {
			return nil, fail("cleanup_failed")
		}
	}
	if err := b.cache.Clear(); err != nil {
		return nil, fail("cleanup_failed")
	}
	_ = b.cache.SetMeta("account_id", "")
	_ = b.cache.SetMeta("account_login", "")
	b.mu.Lock()
	b.account = ""
	b.login = ""
	b.state = "disconnected"
	b.errorType = ""
	b.mu.Unlock()
	return map[string]any{"remote_logout_failed": failed}, nil
}

func (b *bridge) diagnostics() (map[string]any, error) {
	counts, err := b.cache.Counts()
	if err != nil {
		return nil, err
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	return map[string]any{"cache_counts": counts, "history": map[string]any{"transport": "whatsmeow", "batches": b.historyBatches, "received_messages": b.receivedMessages, "undecryptable_messages": b.undecryptable, "muse_replies": b.museReplies, "last_bot_message_fields": b.lastBotFields, "last_muse_correlation_fields": b.lastMuseCorrelation, "state": b.cache.Meta("sync_status"), "last_history_at": nullable(isoOrEmpty(b.historyAt)), "phases": b.contentProgress}}, nil
}
func isoOrEmpty(t time.Time) string {
	if t.IsZero() {
		return ""
	}
	return iso(t)
}

func (b *bridge) discoverAgents() (map[string]any, error) {
	client, _, online := b.connectedAccount()
	if !online {
		return nil, fail("connection_unavailable")
	}
	ctx, cancel := context.WithTimeout(b.ctx, 10*time.Second)
	defer cancel()
	agents, err := client.GetBotListV2(ctx)
	if err != nil {
		return nil, fail("provider_unavailable")
	}
	items := []map[string]any{}
	for _, agent := range agents {
		if len(items) >= 100 {
			break
		}
		id := agent.BotJID.ToNonAD().String()
		if b.cache.Canonical(id) == "" {
			continue
		}
		items = append(items, map[string]any{"id": id, "persona_id": nullable(agent.PersonaID)})
	}
	return map[string]any{"agents": items, "scope": "provider_directory", "saved_chat_verified": false}, nil
}

func (b *bridge) request(method string, input map[string]any) (map[string]any, error) {
	b.op.Lock()
	defer b.op.Unlock()
	if method == "initialize" {
		return b.initialize(input)
	}
	if b.cache == nil {
		return nil, fail("not_initialized")
	}
	switch method {
	case "status":
		return b.status(), nil
	case "pair":
		return b.pair()
	case "reconnect":
		return b.reconnect()
	case "clear_cache":
		if err := b.cache.Clear(); err != nil {
			return nil, err
		}
		return b.status(), nil
	case "disconnect":
		return b.disconnect()
	case "diagnostics":
		return b.diagnostics()
	case "discover_agents":
		return b.discoverAgents()
	case "sync_chat":
		return b.syncChat(input)
	case "muse_converse":
		return b.museConverse(input)
	case "muse_cancel":
		return b.museCancel(input)
	case "muse_incoming":
		return b.museIncoming(input)
	case "execute":
		return b.execute(input)
	default:
		return nil, fail("invalid_method")
	}
}

// This trusted setup action asks the account's own phone for at most 50 older
// messages in one already-cached chat. Catalog reads remain cache-only.
func (b *bridge) syncChat(input map[string]any) (map[string]any, error) {
	client, _, online := b.connectedAccount()
	if !online {
		return nil, fail("connection_unavailable")
	}
	if b.cache.Meta("cleared_at") != "" {
		return nil, fail("cache_cleared")
	}
	chat := b.cache.Canonical(str(input, "chat_id"))
	if chat == "" {
		return nil, fail("invalid_input")
	}
	page, err := b.cache.Read("whatsapp.message.list", map[string]any{"chat_id": chat, "page_size": 1})
	if err != nil {
		return nil, err
	}
	rows := page["items"].([]map[string]any)
	if len(rows) == 0 {
		return nil, fail("not_found")
	}
	row := rows[0]
	jid, _ := types.ParseJID(chat)
	timestamp, err := time.Parse(time.RFC3339Nano, str(row, "timestamp"))
	if err != nil {
		return nil, fail("storage_unavailable")
	}
	fromMe, _ := row["from_me"].(bool)
	info := &types.MessageInfo{MessageSource: types.MessageSource{Chat: jid, IsFromMe: fromMe}, ID: str(row, "id"), Timestamp: timestamp}
	ctx, cancel := context.WithTimeout(b.ctx, 20*time.Second)
	defer cancel()
	request := client.BuildHistorySyncRequest(info, 50)
	maximum := 50
	if jid.Server == types.BotServer {
		// Bot chats may not participate in history-on-demand. Ask our own
		// phone to re-supply this exact cached message instead.
		sender, _ := types.ParseJID(str(row, "sender_id"))
		request = client.BuildUnavailableMessageRequest(jid, sender, info.ID)
		maximum = 1
		b.mu.Lock()
		for key, at := range b.recoveryRequests {
			if time.Since(at) > 2*time.Minute {
				delete(b.recoveryRequests, key)
			}
		}
		if len(b.recoveryRequests) >= 50 {
			b.mu.Unlock()
			return nil, fail("history_request_pending")
		}
		b.recoveryRequests[chat+"\n"+info.ID] = time.Now()
		b.mu.Unlock()
	}
	if _, err = client.SendPeerMessage(ctx, request); err != nil {
		b.mu.Lock()
		delete(b.recoveryRequests, chat+"\n"+info.ID)
		b.mu.Unlock()
		return nil, fail("history_request_failed")
	}
	b.mu.Lock()
	b.historyAt = time.Now()
	b.mu.Unlock()
	if err = b.cache.SetMeta("sync_status", "syncing"); err != nil {
		return nil, err
	}
	return map[string]any{"requested": true, "chat_id": chat, "maximum_messages": maximum}, nil
}

func (b *bridge) close() {
	b.op.Lock()
	defer b.op.Unlock()
	b.mu.Lock()
	if b.closed {
		b.mu.Unlock()
		return
	}
	b.closed = true
	b.manualStop = true
	b.epoch++
	client := b.client
	b.mu.Unlock()
	b.cancel()
	if client != nil {
		client.Disconnect()
	}
	if b.auth != nil {
		_ = b.auth.Close()
	}
	if b.cache != nil {
		_ = b.cache.Close()
	}
}

func (b *bridge) connected() {
	b.mu.Lock()
	if b.closed || b.manualStop || b.device.ID == nil {
		b.mu.Unlock()
		return
	}
	account := b.device.ID.ToNonAD().String()
	old := b.account
	// Switching accounts never exposes another account's retained cache.
	if old != "" && old != account {
		if err := b.cache.ResetAccount(); err != nil {
			b.state = "disconnected"
			b.errorType = "storage_unavailable"
			b.mu.Unlock()
			b.client.Disconnect()
			return
		}
	}
	b.account = account
	b.login = "+" + b.device.ID.User
	b.state = "connected"
	b.qr = ""
	b.errorType = ""
	b.retries = 0
	err := b.cache.SetMeta("account_id", account)
	if err == nil {
		err = b.cache.SetMeta("account_login", b.login)
	}
	if err != nil {
		b.state = "disconnected"
		b.errorType = "storage_unavailable"
	}
	b.mu.Unlock()
	if err != nil {
		b.client.Disconnect()
		return
	}
	if !b.device.LID.IsEmpty() {
		_ = b.cache.Alias(b.device.LID.ToNonAD().String(), account)
	}
	b.notify()
}

func (b *bridge) handleEvent(raw any) {
	b.mu.Lock()
	closed := b.closed
	b.mu.Unlock()
	if closed {
		return
	}
	var err error
	switch e := raw.(type) {
	case *events.Connected:
		b.connected()
		return
	case *events.ManualLoginReconnect:
		b.mu.Lock()
		b.connecting = false
		b.state = "disconnected"
		client := b.client
		b.mu.Unlock()
		go func() { client.Disconnect(); b.scheduleReconnect() }()
		return
	case *events.LoggedOut, *events.StreamReplaced:
		_ = b.cache.SetMeta("whatsmeow_logged_out", "1")
		if b.cache.MuseInit() == nil {
			_ = b.cache.MuseInvalidate()
		}
		b.mu.Lock()
		if b.sendCancel != nil {
			b.sendCancel()
		}
		b.state = "logged_out"
		b.errorType = "relink_required"
		b.qr = ""
		b.manualStop = true
		b.epoch++
		b.mu.Unlock()
		b.notify()
		return
	case *events.Disconnected:
		b.mu.Lock()
		if b.sendCancel != nil {
			b.sendCancel()
		}
		if !b.manualStop && b.state != "logged_out" && b.state != "reconnecting" {
			b.state = "disconnected"
		}
		b.mu.Unlock()
		b.scheduleReconnect()
		b.notify()
		return
	case *events.ConnectFailure, *events.ClientOutdated, *events.TemporaryBan:
		b.mu.Lock()
		b.state = "disconnected"
		b.errorType = "provider_unavailable"
		b.manualStop = true
		b.qr = ""
		b.mu.Unlock()
		b.notify()
		return
	case *events.PairError:
		b.setError("pairing_failed")
		return
	case *events.HistorySync:
		err = b.ingestHistory(e)
	case *events.Message:
		// Whatsmeow also emits auto-parsed peer recovery copies. The trusted
		// outer response below owns exact-ID claims and historical replay rules.
		if e.UnavailableRequestID != "" {
			return
		}
		err = b.ingestMessage(e, false)
	case *events.UndecryptableMessage:
		b.mu.Lock()
		b.undecryptable = min(1_000_000_000, b.undecryptable+1)
		b.mu.Unlock()
		if !e.IsUnavailable && e.Info.Chat.Server == types.BotServer {
			err = b.cache.Chat(e.Info.Chat.ToNonAD().String(), "")
		}
	case *events.Contact:
		if !e.FromFullSync || b.cache.Meta("cleared_at") == "" {
			err = b.cache.Contact(e.JID.ToNonAD().String(), e.Action.GetFullName(), "")
		}
		if err == nil && e.Action.GetLidJID() != "" && e.Action.GetPnJID() != "" {
			err = b.cache.Alias(e.Action.GetLidJID(), e.Action.GetPnJID())
		}
	case *events.PushName:
		err = b.cache.Contact(e.JID.ToNonAD().String(), e.NewPushName, "")
		if err == nil {
			err = b.applyAlias(e.JID, e.JIDAlt)
		}
	case *events.BusinessName:
		err = b.cache.Contact(e.JID.ToNonAD().String(), e.NewBusinessName, "")
	case *events.DeleteForMe:
		err = b.cache.Delete(e.ChatJID.ToNonAD().String(), e.MessageID)
	case *events.ClearChat:
		if !e.FromFullSync || !e.Timestamp.IsZero() {
			err = b.cache.ClearChatAt(e.JID.ToNonAD().String(), false, e.Timestamp)
		}
	case *events.DeleteChat:
		if !e.FromFullSync || !e.Timestamp.IsZero() {
			err = b.cache.ClearChatAt(e.JID.ToNonAD().String(), true, e.Timestamp)
		}
	case *events.JoinedGroup:
		err = b.cache.Chat(e.JID.ToNonAD().String(), e.Name)
	case *events.GroupInfo:
		if e.Name != nil {
			err = b.cache.Chat(e.JID.ToNonAD().String(), e.Name.Name)
		}
	case *events.AppStateSyncError:
		b.setError("history_ingestion_failed")
	case *events.OfflineSyncCompleted:
		if b.cache.Meta("sync_status") == "pending" {
			_ = b.cache.SetMeta("sync_status", "partial")
		}
	}
	if err != nil {
		b.setError("history_ingestion_failed")
	}
}

func (b *bridge) applyAlias(first, second types.JID) error {
	if first.Server == types.HiddenUserServer && second.Server == types.DefaultUserServer {
		return b.cache.Alias(first.ToNonAD().String(), second.ToNonAD().String())
	}
	if second.Server == types.HiddenUserServer && first.Server == types.DefaultUserServer {
		return b.cache.Alias(second.ToNonAD().String(), first.ToNonAD().String())
	}
	return nil
}

func (b *bridge) ingestHistory(e *events.HistorySync) error {
	if e.Data == nil {
		return nil
	}
	d := e.Data
	for _, m := range d.GetPhoneNumberToLidMappings() {
		if err := b.cache.Alias(m.GetLidJID(), m.GetPnJID()); err != nil {
			return err
		}
	}
	cleared := b.cache.Meta("cleared_at") != ""
	if !cleared {
		for _, p := range d.GetPushnames() {
			if err := b.cache.Contact(p.GetID(), p.GetPushname(), ""); err != nil {
				return err
			}
		}
		for _, c := range d.GetInlineContacts() {
			if c.GetLidJID() != "" && c.GetPnJID() != "" {
				_ = b.cache.Alias(c.GetLidJID(), c.GetPnJID())
			}
			if err := b.cache.Contact(c.GetPnJID(), c.GetFullName(), ""); err != nil {
				return err
			}
		}
	}
	for _, conv := range d.GetConversations() {
		jid, err := types.ParseJID(conv.GetID())
		if err != nil {
			continue
		}
		if conv.GetLidJID() != "" && conv.GetPnJID() != "" {
			_ = b.cache.Alias(conv.GetLidJID(), conv.GetPnJID())
		}
		if !cleared {
			if err = b.cache.ChatHistorical(jid.ToNonAD().String(), conv.GetName()); err != nil {
				return err
			}
		}
		for _, m := range conv.GetMessages() {
			event, err := b.client.ParseWebMessage(jid, m.GetMessage())
			if err != nil && jid.Server == types.BotServer && m.GetMessage() != nil {
				// ParseWebMessage treats @bot as a group unless a participant is
				// present. A supplied bot DM has an unambiguous chat/sender.
				web := m.GetMessage()
				sender := jid
				if web.GetKey().GetFromMe() {
					sender, _ = types.ParseJID(b.account)
				}
				event = (&events.Message{Info: types.MessageInfo{MessageSource: types.MessageSource{Chat: jid, Sender: sender, IsFromMe: web.GetKey().GetFromMe()}, ID: web.GetKey().GetID(), Timestamp: time.Unix(int64(web.GetMessageTimestamp()), 0)}, RawMessage: web.GetMessage(), SourceWebMsg: web}).UnwrapRaw()
				err = nil
			}
			if err != nil {
				continue
			}
			if err = b.ingestMessage(event, true); err != nil {
				return err
			}
		}
	}
	b.mu.Lock()
	b.historyAt = time.Now()
	b.historyBatches = min(1_000_000_000, b.historyBatches+1)
	kind := d.GetSyncType().String()
	if kind == "INITIAL_BOOTSTRAP" || kind == "FULL" || kind == "RECENT" || kind == "ON_DEMAND" {
		b.contentProgress[kind] = min(d.GetProgress(), 100)
		state := "complete"
		for _, p := range b.contentProgress {
			if p < 100 {
				state = "syncing"
			}
		}
		if cleared {
			state = "cleared"
		}
		_ = b.cache.SetMeta("sync_status", state)
	}
	b.mu.Unlock()
	if err := b.cache.SetMeta("last_synced_at", iso(time.Now())); err != nil {
		return err
	}
	b.notify()
	return nil
}

func textContent(msg *waE2E.Message) *string {
	if msg == nil {
		return nil
	}
	if msg.Conversation != nil {
		return msg.Conversation
	}
	if ext := msg.GetExtendedTextMessage(); ext != nil && ext.Text != nil {
		return ext.Text
	}
	if rich := msg.GetRichResponseMessage(); rich != nil {
		parts := []string{}
		for _, part := range rich.GetSubmessages() {
			if part.MessageText != nil {
				parts = append(parts, *part.MessageText)
			}
		}
		if len(parts) > 0 {
			text := strings.Join(parts, "\n\n")
			return &text
		}
		return unifiedText(rich.GetUnifiedResponse().GetData())
	}
	return nil
}
func messageContext(msg *waE2E.Message) *waE2E.ContextInfo {
	switch {
	case msg.GetExtendedTextMessage() != nil:
		return msg.GetExtendedTextMessage().GetContextInfo()
	case msg.GetImageMessage() != nil:
		return msg.GetImageMessage().GetContextInfo()
	case msg.GetVideoMessage() != nil:
		return msg.GetVideoMessage().GetContextInfo()
	case msg.GetAudioMessage() != nil:
		return msg.GetAudioMessage().GetContextInfo()
	case msg.GetDocumentMessage() != nil:
		return msg.GetDocumentMessage().GetContextInfo()
	case msg.GetStickerMessage() != nil:
		return msg.GetStickerMessage().GetContextInfo()
	case msg.GetRichResponseMessage() != nil:
		return msg.GetRichResponseMessage().GetContextInfo()
	}
	return nil
}
func (b *bridge) ingestMessage(e *events.Message, historical bool) error {
	if e == nil || e.Message == nil || e.IsViewOnce || e.IsViewOnceV2 || e.IsViewOnceV2Extension {
		return nil
	}
	chat := e.Info.Chat.ToNonAD().String()
	if b.cache.Canonical(chat) == "" {
		return nil
	}
	if err := b.applyAlias(e.Info.Sender, e.Info.SenderAlt); err != nil {
		return err
	}
	if err := b.applyAlias(e.Info.Chat, e.Info.RecipientAlt); err != nil {
		return err
	}
	msg := e.Message
	ephemeral := e.IsEphemeral
	for depth := 0; depth < 8; depth++ {
		inner := msg.GetBotForwardedMessage().GetMessage()
		if inner == nil {
			inner = msg.GetBotTaskMessage().GetMessage()
		}
		if inner == nil {
			break
		}
		unwrapped := (&events.Message{RawMessage: inner}).UnwrapRaw()
		if unwrapped.IsViewOnce || unwrapped.IsViewOnceV2 || unwrapped.IsViewOnceV2Extension {
			return nil
		}
		ephemeral = ephemeral || unwrapped.IsEphemeral
		msg = unwrapped.Message
	}
	if !e.Info.IsFromMe && e.Info.Chat == types.MuseJID {
		fields := []string{}
		msg.ProtoReflect().Range(func(field protoreflect.FieldDescriptor, _ protoreflect.Value) bool {
			if len(fields) < 32 {
				fields = append(fields, string(field.Name()))
			}
			return true
		})
		if msg.GetProtocolMessage() == nil {
			b.mu.Lock()
			b.lastBotFields = fields
			b.mu.Unlock()
		}
	}
	if protocol := msg.GetProtocolMessage(); protocol != nil {
		if protocol.GetType() == waE2E.ProtocolMessage_PEER_DATA_OPERATION_REQUEST_RESPONSE_MESSAGE && e.Info.IsFromMe && e.Info.Sender.Device == 0 && b.cache.Canonical(e.Info.Sender.ToNonAD().String()) == b.account {
			response := protocol.GetPeerDataOperationRequestResponseMessage()
			if response.GetPeerDataOperationRequestType() == waE2E.PeerDataOperationRequestType_PLACEHOLDER_MESSAGE_RESEND {
				for index, part := range response.GetPeerDataOperationResult() {
					if index >= 50 {
						break
					}
					data := part.GetPlaceholderMessageResendResponse().GetWebMessageInfoBytes()
					if len(data) == 0 || len(data) > 1024*1024 {
						continue
					}
					var web waWeb.WebMessageInfo
					if proto.Unmarshal(data, &web) != nil {
						continue
					}
					chatID := web.GetKey().GetRemoteJID()
					id := web.GetKey().GetID()
					jid, parseErr := types.ParseJID(chatID)
					if parseErr != nil || jid.Server != types.BotServer || b.cache.Canonical(chatID) == "" {
						continue
					}
					b.mu.Lock()
					at, requested := b.recoveryRequests[chatID+"\n"+id]
					delete(b.recoveryRequests, chatID+"\n"+id)
					b.mu.Unlock()
					if !requested || time.Since(at) > 2*time.Minute {
						continue
					}
					sender := jid
					if web.GetKey().GetFromMe() {
						sender, _ = types.ParseJID(b.account)
					}
					recovered := (&events.Message{Info: types.MessageInfo{MessageSource: types.MessageSource{Chat: jid, Sender: sender, IsFromMe: web.GetKey().GetFromMe()}, ID: id, Timestamp: time.Unix(int64(web.GetMessageTimestamp()), 0)}, RawMessage: web.GetMessage(), SourceWebMsg: &web}).UnwrapRaw()
					if err := b.ingestMessage(recovered, true); err != nil {
						return err
					}
				}
			}
		}
		switch protocol.GetType() {
		case waE2E.ProtocolMessage_REVOKE:
			return b.cache.Delete(chat, protocol.GetKey().GetID())
		case waE2E.ProtocolMessage_MESSAGE_EDIT:
			if text := textContent(protocol.GetEditedMessage()); text != nil {
				messageID := protocol.GetKey().GetID()
				if err := b.editCached(chat, messageID, *text, historical); err != nil {
					return err
				}
				if remote := protocol.GetKey().GetRemoteJID(); remote != "" && remote != chat {
					return nil
				}
				return b.observeMuseEdit(e, protocol.GetEditedMessage(), messageID, historical)
			}
		}
		return nil
	}
	botEdit := e.Info.Chat.Server == types.BotServer && e.Info.MsgBotInfo.EditTargetID != ""
	if !botEdit && (e.IsEdit || e.Info.Edit == types.EditAttributeMessageEdit) {
		if text := textContent(msg); text != nil {
			if err := b.editCached(chat, e.Info.ID, *text, historical); err != nil {
				return err
			}
			return b.observeMuseEdit(e, msg, e.Info.ID, historical)
		}
		return nil
	}
	messageID := e.Info.ID
	if botEdit {
		messageID = e.Info.MsgBotInfo.EditTargetID
	}
	text := textContent(msg)
	if botEdit && text == nil {
		return nil
	}
	kind := "unsupported"
	if text != nil {
		kind = "text"
	} else if msg.ImageMessage != nil || msg.VideoMessage != nil || msg.AudioMessage != nil || msg.DocumentMessage != nil || msg.StickerMessage != nil {
		kind = "media"
	}
	contextInfo := messageContext(msg)
	var expires *time.Time
	duration := contextInfo.GetExpiration()
	if duration == 0 {
		duration = e.SourceWebMsg.GetEphemeralDuration()
	}
	if duration > 0 {
		expiry := e.Info.Timestamp.Add(time.Duration(duration) * time.Second)
		if start := e.SourceWebMsg.GetEphemeralStartTimestamp(); start > 0 {
			expiry = time.Unix(int64(start), 0).Add(time.Duration(duration) * time.Second)
		}
		expires = &expiry
	}
	if !historical || b.cache.Meta("cleared_at") == "" {
		if e.Info.PushName != "" && e.Info.Sender.Server != types.GroupServer {
			if err := b.cache.Contact(e.Info.Sender.ToNonAD().String(), e.Info.PushName, ""); err != nil {
				return err
			}
		}
	}
	cached := cache.Message{ID: messageID, ChatID: chat, SenderID: e.Info.Sender.ToNonAD().String(), FromMe: e.Info.IsFromMe, Timestamp: e.Info.Timestamp, Text: text, Type: kind, ReplyTo: contextInfo.GetStanzaID(), ExpiresAt: expires, Ephemeral: ephemeral, Edited: botEdit && text != nil}
	var err error
	if text != nil && !e.Info.IsFromMe && e.Info.Chat.ToNonAD() == types.MuseJID && e.Info.Sender.ToNonAD() == types.MuseJID {
		account, ancestors := b.museParents(e, msg)
		err = b.cache.PutMuseMessage(cached, historical, account, ancestors, string(e.Info.MsgBotInfo.EditType))
	} else {
		err = b.cache.PutMessage(cached, historical)
	}
	if err != nil {
		return err
	}
	if err = b.observeMuse(e, msg, messageID, historical); err != nil {
		return err
	}
	b.mu.Lock()
	b.receivedMessages = min(1_000_000_000, b.receivedMessages+1)
	if !e.Info.IsFromMe && e.Info.Chat == types.MuseJID && text != nil {
		b.museReplies = min(1_000_000_000, b.museReplies+1)
	}
	b.mu.Unlock()
	return b.cache.SetMeta("last_synced_at", iso(time.Now()))
}

func (b *bridge) editCached(chat, id, text string, historical bool) error {
	if historical {
		return b.cache.EditHistorical(chat, id, text)
	}
	return b.cache.Edit(chat, id, text)
}
