package main

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"math"
	"regexp"
	"strings"
	"time"
	"unicode/utf8"

	"eidolon/whatsapp/internal/cache"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waCommon"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/proto"
)

var exchangePattern = regexp.MustCompile(`^[a-f0-9]{64}$`)

func museInteger(value any) (int64, bool) {
	switch n := value.(type) {
	case float64:
		if n >= 0 && n <= 9007199254740991 && math.Trunc(n) == n {
			return int64(n), true
		}
	case int:
		if n >= 0 {
			return int64(n), true
		}
	case int64:
		if n >= 0 {
			return n, true
		}
	}
	return 0, false
}

func (b *bridge) museCancel(input map[string]any) (map[string]any, error) {
	if len(input) != 1 {
		return nil, fail("invalid_input")
	}
	grant, ok := museInteger(input["grant_id"])
	if !ok || grant == 0 {
		return nil, fail("invalid_input")
	}
	if err := b.cache.MuseInit(); err != nil {
		return nil, err
	}
	if err := b.cache.MuseCancel(grant); err != nil {
		return nil, err
	}
	return map[string]any{"cancelled": true}, nil
}

func (b *bridge) museIncoming(input map[string]any) (map[string]any, error) {
	for key := range input {
		if key != "account_id" && key != "after_id" && key != "since" {
			return nil, fail("invalid_input")
		}
	}
	after := int64(0)
	if value, present := input["after_id"]; present {
		var ok bool
		after, ok = museInteger(value)
		if !ok {
			return nil, fail("invalid_input")
		}
	}
	var since time.Time
	if value, present := input["since"]; present {
		stamp, ok := value.(string)
		if !ok || len(stamp) > 64 {
			return nil, fail("invalid_input")
		}
		var err error
		since, err = time.Parse(time.RFC3339Nano, stamp)
		if err != nil {
			return nil, fail("invalid_input")
		}
	}
	account := str(input, "account_id")
	_, currentAccount, online := b.connectedAccount()
	if account == "" || account != currentAccount {
		return nil, fail("connection_changed")
	}
	if !online {
		return nil, fail("connection_unavailable")
	}
	result, err := b.cache.MuseIncoming(account, after, since)
	if err != nil {
		return nil, err
	}
	_, currentAccount, online = b.connectedAccount()
	if account != currentAccount {
		return nil, fail("connection_changed")
	}
	if !online {
		return nil, fail("connection_unavailable")
	}
	return result, nil
}

func (b *bridge) museConverse(input map[string]any) (map[string]any, error) {
	for key := range input {
		switch key {
		case "account_id", "grant_id", "exchange_id", "text", "reply_to_message_id", "wait_seconds":
		default:
			return nil, fail("invalid_input")
		}
	}
	grant, ok := museInteger(input["grant_id"])
	id, account := str(input, "exchange_id"), str(input, "account_id")
	if !ok || grant == 0 || !exchangePattern.MatchString(id) {
		return nil, fail("invalid_input")
	}
	waitSeconds := int64(10)
	if value, present := input["wait_seconds"]; present {
		waitSeconds, ok = museInteger(value)
		if !ok || waitSeconds > 20 {
			return nil, fail("invalid_input")
		}
	}
	client, connectedAccount, online := b.connectedAccount()
	if account == "" || account != connectedAccount {
		return nil, fail("connection_changed")
	}
	if !online {
		return nil, fail("connection_unavailable")
	}
	if err := b.cache.MuseInit(); err != nil {
		return nil, err
	}
	// Total send plus polling stays within the transport's 30 second deadline.
	deadline := time.Now().Add(20 * time.Second)
	text, hasText := input["text"]
	if !hasText {
		if _, quotePresent := input["reply_to_message_id"]; quotePresent {
			return nil, fail("invalid_input")
		}
	} else {
		body, stringOK := text.(string)
		if !stringOK || !utf8.ValidString(body) || strings.TrimSpace(body) == "" || utf8.RuneCountInString(body) > 4000 {
			return nil, fail("invalid_input")
		}
		quoteID := str(input, "reply_to_message_id")
		if _, present := input["reply_to_message_id"]; present && (quoteID == "" || len(quoteID) > 256) {
			return nil, fail("invalid_input")
		}
		digestJSON, _ := json.Marshal([]any{account, grant, body, quoteID})
		digestBytes := sha256.Sum256(digestJSON)
		var randomID [16]byte
		if _, err := rand.Read(randomID[:]); err != nil {
			return nil, fail("internal_failure")
		}
		sentID := "3EB0" + strings.ToUpper(hex.EncodeToString(randomID[:]))
		// Validate the retained quote before claiming a brand-new dispatch, but a
		// duplicate receipt remains retrievable after the quote is deleted.
		_, existingErr := b.cache.MuseResult(account, grant, id)
		if existingErr != nil && errorCode(existingErr) != "not_found" {
			return nil, existingErr
		}
		var quoted *cache.Message
		if existingErr != nil && errorCode(existingErr) == "not_found" && quoteID != "" {
			var err error
			quoted, err = b.cache.Quote(types.MuseJID.String(), quoteID)
			if err != nil {
				return nil, err
			}
		}
		_, dispatch, err := b.cache.MuseClaim(account, grant, id, hex.EncodeToString(digestBytes[:]), sentID)
		if err != nil {
			return nil, err
		}
		if dispatch {
			msg := &waE2E.Message{Conversation: proto.String(body)}
			if quoted != nil {
				msg = &waE2E.Message{ExtendedTextMessage: &waE2E.ExtendedTextMessage{Text: proto.String(body), ContextInfo: &waE2E.ContextInfo{StanzaID: proto.String(quoted.ID), Participant: proto.String(quoted.SenderID), RemoteJID: proto.String(types.MuseJID.String()), QuotedMessage: &waE2E.Message{Conversation: quoted.Text}}}}
			}
			current, currentAccount, currentOnline := b.connectedAccount()
			if current == client && currentAccount == account && currentOnline {
				if err = b.museSend(client, account, id, sentID, body, quoteID, msg, deadline); err != nil {
					return nil, err
				}
			}
		}
	}
	waitUntil := time.Now().Add(time.Duration(waitSeconds) * time.Second)
	if deadline.Before(waitUntil) {
		waitUntil = deadline
	}
	for {
		_, currentAccount, _ := b.connectedAccount()
		if currentAccount != account {
			return nil, fail("connection_changed")
		}
		result, err := b.cache.MuseResult(account, grant, id)
		if err != nil {
			return nil, err
		}
		if result["status"] != "pending" || !time.Now().Before(waitUntil) {
			return result, nil
		}
		timer := time.NewTimer(min(100*time.Millisecond, time.Until(waitUntil)))
		select {
		case <-b.ctx.Done():
			timer.Stop()
			return nil, fail("connection_unavailable")
		case <-timer.C:
		}
	}
}

func (b *bridge) museSend(client socket, account, exchangeID, sentID, body, quoteID string, msg *waE2E.Message, deadline time.Time) error {
	timeout := min(b.sendTimeout, time.Until(deadline))
	ctx, cancel := context.WithTimeout(b.ctx, timeout)
	defer cancel()
	b.mu.Lock()
	// Register the cancellation guard and recheck while holding the same mutex
	// used by logout/disconnect. Provider callbacks can run during SendMessage.
	if b.client != client || b.account != account || b.state != "connected" || b.manualStop || !client.IsConnected() || !client.IsLoggedIn() {
		b.mu.Unlock()
		return nil
	}
	b.sendCancel = cancel
	b.mu.Unlock()
	defer func() {
		b.mu.Lock()
		b.sendCancel = nil
		offline := b.state != "connected"
		b.mu.Unlock()
		if offline {
			b.scheduleReconnect()
		}
	}()
	resp, err := client.SendMessage(ctx, types.MuseJID, msg, whatsmeow.SendRequestExtra{ID: sentID, Timeout: timeout})
	if err != nil || resp.ID == "" || len(resp.ID) > 256 {
		return nil
	} // Durable outcome_unknown; never retry.
	if err = b.cache.MuseAccepted(exchangeID, resp.ID); err != nil {
		return fail("outcome_unknown")
	}
	if resp.Timestamp.IsZero() {
		resp.Timestamp = time.Now()
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.account != account || b.client != client || b.state != "connected" || b.manualStop {
		return nil
	}
	return b.cache.PutMessage(cache.Message{ID: resp.ID, ChatID: types.MuseJID.String(), SenderID: account, FromMe: true, Timestamp: resp.Timestamp, Text: &body, Type: "text", ReplyTo: quoteID}, false)
}

func museKeyID(key *waCommon.MessageKey) string {
	if key == nil || key.GetRemoteJID() != "" && key.GetRemoteJID() != types.MuseJID.String() {
		return ""
	}
	return key.GetID()
}

func (b *bridge) observeMuseEdit(e *events.Message, edited *waE2E.Message, replyID string, historical bool) error {
	// Edit targets are provider-supplied ancestry to an existing reply. Copy the
	// event so its outer contexts remain available without mutating the event
	// Whatsmeow may also deliver to other handlers.
	event := *e
	event.Info.MsgBotInfo.EditTargetID = replyID
	return b.observeMuse(&event, edited, replyID, historical)
}

func (b *bridge) observeMuse(e *events.Message, msg *waE2E.Message, replyID string, historical bool) error {
	if historical || e.Info.IsFromMe || e.Info.Chat.ToNonAD() != types.MuseJID || e.Info.Sender.ToNonAD() != types.MuseJID || textContent(msg) == nil {
		return nil
	}
	if err := b.cache.MuseInit(); err != nil {
		return err
	}
	account, ancestors := b.museParents(e, msg)
	if err := b.cache.MuseObserve(account, ancestors, replyID, string(e.Info.MsgBotInfo.EditType)); err != nil {
		return err
	}
	return b.cache.MuseIncomingEdit(account, replyID, string(e.Info.MsgBotInfo.EditType))
}

func (b *bridge) museParents(e *events.Message, msg *waE2E.Message) (string, []string) {
	ancestors := []string{e.Info.MsgBotInfo.EditTargetID}
	meta := e.Info.MsgMetaInfo
	var rootID string
	if b.device != nil && b.device.ChatSettings != nil {
		// The encryption root is a shared secret reference, never prompt ancestry.
		rootID, _ = b.device.ChatSettings.GetWASARootSecretID(b.ctx, types.MuseJID)
	}
	if meta.TargetID != rootID && (meta.TargetChat.IsEmpty() || meta.TargetChat.ToNonAD() == types.MuseJID) {
		ancestors = append(ancestors, meta.TargetID)
	}
	ancestors = append(ancestors, meta.ThreadMessageID)
	fields := map[string]bool{"target": meta.TargetID != "", "target_is_wasa_root": rootID != "" && meta.TargetID == rootID, "thread_parent": meta.ThreadMessageID != "", "quote": false, "association_parent": false, "plugin_parent": false, "edit_target": e.Info.MsgBotInfo.EditTargetID != ""}
	// Bot forwarding/task envelopes may carry the native parent on an outer
	// context rather than the text-bearing message. Inspect only known contexts.
	for _, original := range []*waE2E.Message{msg, e.Message, e.RawMessage} {
		for depth, current := 0, original; current != nil && depth < 8; depth++ {
			ctx := messageContext(current)
			if ctx.GetRemoteJID() == "" || ctx.GetRemoteJID() == types.MuseJID.String() {
				ancestors = append(ancestors, ctx.GetStanzaID())
				fields["quote"] = fields["quote"] || ctx.GetStanzaID() != ""
			}
			mctx := current.GetMessageContextInfo()
			association := museKeyID(mctx.GetMessageAssociation().GetParentMessageKey())
			plugin := museKeyID(mctx.GetBotMetadata().GetPluginMetadata().GetParentPluginMessageKey())
			ancestors = append(ancestors, association, plugin)
			fields["association_parent"] = fields["association_parent"] || association != ""
			fields["plugin_parent"] = fields["plugin_parent"] || plugin != ""
			next := current.GetBotForwardedMessage().GetMessage()
			if next == nil {
				next = current.GetBotTaskMessage().GetMessage()
			}
			current = next
		}
	}
	b.mu.Lock()
	account := b.account
	b.lastMuseCorrelation = fields
	b.mu.Unlock()
	return account, ancestors
}
