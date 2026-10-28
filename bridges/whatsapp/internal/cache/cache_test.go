package cache

import (
	"database/sql"
	"encoding/json"
	"errors"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

const testChat = "14165551234@s.whatsapp.net"

func openTest(t *testing.T) (*Store, *time.Time) {
	t.Helper()
	s, err := Open(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC().Truncate(time.Millisecond)
	s.Now = func() time.Time { return now }
	t.Cleanup(func() { _ = s.Close() })
	return s, &now
}
func text(s string) *string { return &s }
func put(t *testing.T, s *Store, m Message, historical bool) {
	t.Helper()
	if err := s.PutMessage(m, historical); err != nil {
		t.Fatal(err)
	}
}
func items(t *testing.T, s *Store, operation string, input map[string]any) []map[string]any {
	t.Helper()
	result, err := s.Read(operation, input)
	if err != nil {
		t.Fatal(err)
	}
	return result["items"].([]map[string]any)
}
func requireCode(t *testing.T, err error, code string) {
	t.Helper()
	var coded *Error
	if !errors.As(err, &coded) || coded.Code != code {
		t.Fatalf("expected code %s, got %v", code, err)
	}
}

func TestLegacyDatabaseRestorationPreservesAuthAndCachedText(t *testing.T) {
	directory := t.TempDir()
	db, err := sql.Open("sqlite3", filepath.Join(directory, "whatsapp.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	_, err = db.Exec(`CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE auth(key TEXT PRIMARY KEY,value BLOB NOT NULL);
CREATE TABLE contacts(id TEXT PRIMARY KEY,name TEXT NOT NULL,phone TEXT);
CREATE TABLE chats(id TEXT PRIMARY KEY,name TEXT NOT NULL,type TEXT NOT NULL,last_message_at TEXT);
CREATE TABLE messages(chat_id TEXT NOT NULL,id TEXT NOT NULL,sender_id TEXT NOT NULL,from_me INTEGER NOT NULL,timestamp TEXT NOT NULL,text TEXT,type TEXT NOT NULL,reply_to_message_id TEXT,expires_at INTEGER,quoted TEXT,PRIMARY KEY(chat_id,id));
INSERT INTO metadata VALUES('generation','original-generation');INSERT INTO auth VALUES('creds',X'01020304');`)
	if err != nil {
		t.Fatal(err)
	}
	stamp := iso(time.Now().Add(-time.Hour))
	_, err = db.Exec("INSERT INTO chats VALUES(?,?,?,?)", museID, museID, "agent", stamp)
	if err != nil {
		t.Fatal(err)
	}
	_, err = db.Exec("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?)", museID, "legacy-message", museID, 0, stamp, "existing cached reply", "text", nil, nil, `{"key":{"id":"legacy-message","remoteJid":"1807055946647697@bot","fromMe":false},"message":{"conversation":"existing cached reply"}}`)
	if err != nil {
		t.Fatal(err)
	}
	if err = db.Close(); err != nil {
		t.Fatal(err)
	}
	s, err := Open(directory)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = s.Close() }()
	if s.Meta("generation") != "original-generation" {
		t.Fatal("generation lost")
	}
	if legacy, err := s.LegacyAuthPresent(); err != nil || !legacy {
		t.Fatal("legacy remote session was not identified")
	}
	var auth []byte
	if err = s.db.QueryRow("SELECT value FROM auth WHERE key='creds'").Scan(&auth); err != nil || string(auth) != string([]byte{1, 2, 3, 4}) {
		t.Fatal("encrypted legacy auth was altered")
	}
	chats := items(t, s, "whatsapp.chat.list", map[string]any{"query": "Muse"})
	if len(chats) != 1 || chats[0]["type"] != "agent" {
		t.Fatalf("legacy Muse not labeled: %#v", chats)
	}
	contacts := items(t, s, "whatsapp.contact.search", map[string]any{"query": "Muse"})
	if len(contacts) != 1 || contacts[0]["phone"] != nil {
		t.Fatalf("agent contact: %#v", contacts)
	}
	quote, err := s.Quote(museID, "legacy-message")
	if err != nil || quote.Text == nil || *quote.Text != "existing cached reply" || quote.FromMe {
		t.Fatalf("quote restore: %#v %v", quote, err)
	}
	if err = s.Close(); err != nil {
		t.Fatal(err)
	}
	s, err = Open(directory)
	if err != nil {
		t.Fatal(err)
	}
	if got := items(t, s, "whatsapp.message.list", map[string]any{"chat_id": museID}); len(got) != 1 || got[0]["from_me"] != false {
		t.Fatalf("restore failed: %#v", got)
	}
}

func TestRetentionViewOnceExpiryAndEdits(t *testing.T) {
	s, now := openTest(t)
	base := Message{ChatID: testChat, SenderID: testChat, Timestamp: now.Add(-time.Hour), Text: text("hello")}
	for _, tc := range []struct {
		id     string
		change func(*Message)
	}{
		{"retained", func(m *Message) {}},
		{"old", func(m *Message) { m.Timestamp = now.Add(-retention - time.Second) }},
		{"future", func(m *Message) { m.Timestamp = now.Add(25 * time.Hour) }},
		{"once", func(m *Message) { m.ViewOnce = true }},
		{"unknown-expiry", func(m *Message) { m.Ephemeral = true }},
		{"expired", func(m *Message) { expiry := now.Add(-time.Second); m.ExpiresAt = &expiry }},
		{"ephemeral", func(m *Message) { expiry := now.Add(time.Minute); m.Ephemeral = true; m.ExpiresAt = &expiry }},
		{"media", func(m *Message) { m.Text = nil; m.Type = "media" }},
	} {
		m := base
		m.ID = tc.id
		tc.change(&m)
		put(t, s, m, false)
	}
	got := items(t, s, "whatsapp.message.list", map[string]any{"chat_id": testChat})
	if len(got) != 3 {
		t.Fatalf("unexpected retained items %#v", got)
	}
	if err := s.Edit(testChat, "retained", "updated"); err != nil {
		t.Fatal(err)
	}
	quote, err := s.Quote(testChat, "retained")
	if err != nil || *quote.Text != "updated" {
		t.Fatalf("edited quote %#v %v", quote, err)
	}
	_, err = s.Quote(testChat, "media")
	requireCode(t, err, "not_found")
	if err = s.Edit(testChat, "retained", strings.Repeat("x", 20_001)); err != nil {
		t.Fatal(err)
	}
	_, err = s.Quote(testChat, "retained")
	requireCode(t, err, "not_found")
	if err = s.Delete(testChat, "retained"); err != nil {
		t.Fatal(err)
	}
	*now = now.Add(2 * time.Minute)
	counts, err := s.Counts()
	if err != nil || counts["messages"] != int64(1) {
		t.Fatalf("expiry was not applied %#v %v", counts, err)
	}
	*now = now.Add(retention)
	coverage, err := s.Coverage()
	if err != nil || coverage["oldest_message_at"] != nil {
		t.Fatalf("retention did not prune %#v %v", coverage, err)
	}
}

func TestLIDMappingMergesDuplicatesAndCanonicalizesQuotes(t *testing.T) {
	s, now := openTest(t)
	lid := "12345678901@lid"
	groupID := "120363123456789@g.us"
	if err := s.Contact(lid, "Friend", ""); err != nil {
		t.Fatal(err)
	}
	put(t, s, Message{ID: "same", ChatID: lid, SenderID: lid, Timestamp: *now, Text: text("LID duplicate")}, false)
	put(t, s, Message{ID: "same", ChatID: testChat, SenderID: testChat, Timestamp: *now, Text: text("phone copy")}, false)
	put(t, s, Message{ID: "only-lid", ChatID: lid, SenderID: lid, Timestamp: *now, Text: text("unique")}, false)
	put(t, s, Message{ID: "group", ChatID: groupID, SenderID: lid, Timestamp: *now, Text: text("group text")}, false)
	if err := s.Alias(lid, testChat); err != nil {
		t.Fatal(err)
	}
	if s.Canonical("12345678901:4@lid") != testChat {
		t.Fatal("device LID was not canonicalized")
	}
	messages := items(t, s, "whatsapp.message.list", map[string]any{"chat_id": lid})
	if len(messages) != 2 {
		t.Fatalf("dedup %#v", messages)
	}
	quote, err := s.Quote(lid, "same")
	if err != nil || *quote.Text != "phone copy" || quote.ChatID != testChat {
		t.Fatalf("canonical copy %#v %v", quote, err)
	}
	quote, err = s.Quote(groupID, "group")
	if err != nil || quote.SenderID != testChat {
		t.Fatalf("sender alias %#v %v", quote, err)
	}
	_, err = s.Quote(groupID, "same")
	requireCode(t, err, "not_found")
	contacts := items(t, s, "whatsapp.contact.search", map[string]any{"query": "Friend"})
	if len(contacts) != 1 || contacts[0]["id"] != testChat || contacts[0]["phone"] != "+14165551234" {
		t.Fatalf("mapped contact %#v", contacts)
	}
	for _, chat := range items(t, s, "whatsapp.chat.list", map[string]any{}) {
		if chat["id"] == lid {
			t.Fatal("old LID chat retained")
		}
	}
}

func TestClearPreservesSessionAndDispatchButSuppressesHistory(t *testing.T) {
	s, now := openTest(t)
	if err := s.SetMeta("generation", "generation"); err != nil {
		t.Fatal(err)
	}
	if err := s.SetMeta("account_id", testChat); err != nil {
		t.Fatal(err)
	}
	if _, err := s.db.Exec("INSERT INTO auth VALUES('creds',X'0102')"); err != nil {
		t.Fatal(err)
	}
	put(t, s, Message{ID: "before", ChatID: testChat, Timestamp: *now, Text: text("old")}, false)
	if _, err := s.Dispatch("approval:1", "digest"); err != nil {
		t.Fatal(err)
	}
	if err := s.Clear(); err != nil {
		t.Fatal(err)
	}
	for _, table := range []string{"messages", "chats", "contacts"} {
		var count int
		if err := s.db.QueryRow("SELECT COUNT(*) FROM " + table).Scan(&count); err != nil || count != 0 {
			t.Fatal("cache not empty", table, err)
		}
	}
	if s.Meta("generation") != "generation" || s.Meta("account_id") != testChat {
		t.Fatal("identity metadata lost")
	}
	var count int
	if err := s.db.QueryRow("SELECT COUNT(*) FROM auth").Scan(&count); err != nil || count != 1 {
		t.Fatal("auth lost")
	}
	*now = now.Add(time.Minute)
	put(t, s, Message{ID: "old-replay", ChatID: testChat, Timestamp: now.Add(-time.Hour), Text: text("replayed")}, false)
	put(t, s, Message{ID: "new-history", ChatID: testChat, Timestamp: *now, Text: text("history batch")}, true)
	put(t, s, Message{ID: "future-live", ChatID: testChat, Timestamp: *now, Text: text("future live")}, false)
	messages := items(t, s, "whatsapp.message.list", map[string]any{"chat_id": testChat})
	if len(messages) != 1 || messages[0]["id"] != "future-live" {
		t.Fatalf("clear replay suppression %#v", messages)
	}
	_, err := s.Dispatch("approval:1", "digest")
	requireCode(t, err, "outcome_unknown")
}

func TestDispatchNeverReplaysUncertainOrChangedInput(t *testing.T) {
	directory := t.TempDir()
	s, err := Open(directory)
	if err != nil {
		t.Fatal(err)
	}
	result, err := s.Dispatch("approval:42", "digest")
	if err != nil || result != nil {
		t.Fatal(result, err)
	}
	if err = s.Close(); err != nil {
		t.Fatal(err)
	}
	s, err = Open(directory)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	_, err = s.Dispatch("approval:42", "digest")
	requireCode(t, err, "outcome_unknown")
	_, err = s.Dispatch("approval:42", "other")
	requireCode(t, err, "dispatch_conflict")
	accepted := map[string]any{"accepted": true, "message_id": "remote-id", "chat_id": testChat}
	if err = s.Accepted("approval:42", accepted); err != nil {
		t.Fatal(err)
	}
	result, err = s.Dispatch("approval:42", "digest")
	if err != nil || result["message_id"] != "remote-id" {
		t.Fatalf("accepted replay %#v %v", result, err)
	}
	if err = s.Clear(); err != nil {
		t.Fatal(err)
	}
	result, err = s.Dispatch("approval:42", "digest")
	if err != nil || result["message_id"] != "remote-id" {
		t.Fatal("clear erased dispatch identity")
	}
}

func TestPaginationAndSearchValidateFiltersAndBoundResponses(t *testing.T) {
	s, now := openTest(t)
	for i, id := range []string{"a", "b", "c"} {
		put(t, s, Message{ID: id, ChatID: testChat, Timestamp: now.Add(time.Duration(i) * time.Second), Text: text("searchable " + id)}, false)
	}
	input := map[string]any{"query": "searchable", "page_size": float64(2)}
	first, err := s.Read("whatsapp.message.search", input)
	if err != nil {
		t.Fatal(err)
	}
	if len(first["items"].([]map[string]any)) != 2 || first["next_cursor"] == nil {
		t.Fatal(first)
	}
	input["cursor"] = first["next_cursor"]
	last, err := s.Read("whatsapp.message.search", input)
	if err != nil || len(last["items"].([]map[string]any)) != 1 || last["next_cursor"] != nil {
		t.Fatalf("page %#v %v", last, err)
	}
	input["query"] = "other"
	_, err = s.Read("whatsapp.message.search", input)
	requireCode(t, err, "invalid_input")
	for _, bad := range []map[string]any{{"page_size": 101}, {"page_size": 0}, {"page_size": 1.5}, {"page_size": "50"}, {"page_size": nil}, {"cursor": "bad"}, {"unknown": "value"}, {"query": false}} {
		_, err = s.Read("whatsapp.contact.search", bad)
		requireCode(t, err, "invalid_input")
	}
	_, err = s.Read("whatsapp.message.search", map[string]any{"query": "x", "since": "bad"})
	requireCode(t, err, "invalid_input")
	_, err = s.Read("whatsapp.message.list", map[string]any{"chat_id": "status@broadcast"})
	requireCode(t, err, "invalid_input")
	_, err = s.Read("whatsapp.message.search", map[string]any{"query": "x", "since": iso(now.Add(time.Hour)), "until": iso(*now)})
	requireCode(t, err, "invalid_input")
	_, err = s.Read("whatsapp.not.declared", nil)
	requireCode(t, err, "operation_undeclared")
	// JSON encodes these symbols as six-byte escapes; the page must shrink by
	// whole messages, while retaining their complete text and a usable cursor.
	large := strings.Repeat("<", 20_000)
	for i := 0; i < 50; i++ {
		put(t, s, Message{ID: strings.Repeat("x", i+1), ChatID: testChat, Timestamp: *now, Text: &large}, false)
	}
	page, err := s.Read("whatsapp.message.list", map[string]any{"chat_id": testChat, "page_size": 100})
	if err != nil {
		t.Fatal(err)
	}
	encoded, _ := json.Marshal(page)
	if len(encoded) > maxPageBytes+1024 || page["next_cursor"] == nil {
		t.Fatalf("response not bounded: %d", len(encoded))
	}
	for _, m := range page["items"].([]map[string]any) {
		if value := m["text"].(string); strings.HasPrefix(value, "<") && value != large {
			t.Fatal("body truncated")
		}
	}
}

func TestObservedAgentNamesNeverInventHistory(t *testing.T) {
	s, _ := openTest(t)
	if got := items(t, s, "whatsapp.chat.list", nil); len(got) != 0 {
		t.Fatal("agent directory invented chat")
	}
	if err := s.Chat(museID, ""); err != nil {
		t.Fatal(err)
	}
	if err := s.Contact(museID, "My custom Muse", ""); err != nil {
		t.Fatal(err)
	}
	if err := s.Contact(museID, "", ""); err != nil {
		t.Fatal(err)
	}
	contacts := items(t, s, "whatsapp.contact.search", map[string]any{})
	if len(contacts) != 1 || contacts[0]["name"] != "My custom Muse" || contacts[0]["phone"] != nil {
		t.Fatal(contacts)
	}
	for _, id := range []string{"status@broadcast", "abc@bot", "123456@newsletter", "1234@s.whatsapp.net", "user@example.com"} {
		if Normalize(id) != "" {
			t.Fatalf("accepted %s", id)
		}
	}
}

func TestReplacementAccountResetsAllCacheIdentityAndClearBarrier(t *testing.T) {
	s, now := openTest(t)
	for key, value := range map[string]string{"generation": "same-directory", "account_id": testChat, "last_synced_at": iso(*now), "history_diagnostics": "prior-history"} {
		if err := s.SetMeta(key, value); err != nil {
			t.Fatal(err)
		}
	}
	if err := s.Alias("12345678901@lid", testChat); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Dispatch("approval:old-account", "digest"); err != nil {
		t.Fatal(err)
	}
	if _, err := s.db.Exec("INSERT INTO auth VALUES('legacy',X'ABCD')"); err != nil {
		t.Fatal(err)
	}
	put(t, s, Message{ID: "old", ChatID: testChat, Timestamp: *now, Text: text("prior account")}, false)
	if err := s.Clear(); err != nil {
		t.Fatal(err)
	}
	if err := s.ResetAccount(); err != nil {
		t.Fatal(err)
	}
	for _, table := range []string{"messages", "chats", "contacts", "aliases", "dispatches"} {
		var count int
		if err := s.db.QueryRow("SELECT COUNT(*) FROM " + table).Scan(&count); err != nil || count != 0 {
			t.Fatalf("prior account %s not cleared", table)
		}
	}
	if s.Meta("generation") != "same-directory" || s.Meta("account_id") != "" || s.Meta("cleared_at") != "" || s.Meta("history_diagnostics") != "" || s.Meta("last_synced_at") != "" {
		t.Fatal("replacement account inherited old identity")
	}
	var count int
	if err := s.db.QueryRow("SELECT COUNT(*) FROM auth").Scan(&count); err != nil || count != 1 {
		t.Fatal("legacy encrypted auth unexpectedly modified")
	}
	put(t, s, Message{ID: "new", ChatID: "14165559999@s.whatsapp.net", Timestamp: *now, Text: text("new account history")}, true)
	got := items(t, s, "whatsapp.message.search", map[string]any{"query": "new account"})
	if len(got) != 1 {
		t.Fatal("old clear barrier suppressed new account")
	}
}

func TestConcurrentDispatchClaimsOneIdentityBeforeAnySend(t *testing.T) {
	s, _ := openTest(t)
	var workers sync.WaitGroup
	results := make(chan error, 12)
	for i := 0; i < 12; i++ {
		workers.Add(1)
		go func() { defer workers.Done(); _, err := s.Dispatch("approval:concurrent", "digest"); results <- err }()
	}
	workers.Wait()
	close(results)
	claimed := 0
	for err := range results {
		if err == nil {
			claimed++
		} else {
			requireCode(t, err, "outcome_unknown")
		}
	}
	if claimed != 1 {
		t.Fatalf("dispatch claimed %d times", claimed)
	}
}

func TestHistoricalReplayCannotUndoMutationsAcrossAliases(t *testing.T) {
	s, now := openTest(t)
	lid := "999999999999@lid"
	original := Message{ID: "edited", ChatID: lid, SenderID: lid, Timestamp: *now, Text: text("original")}
	put(t, s, original, false)
	if err := s.Edit(lid, original.ID, "latest edit"); err != nil {
		t.Fatal(err)
	}
	if err := s.Alias(lid, testChat); err != nil {
		t.Fatal(err)
	}
	put(t, s, original, true)
	got, err := s.Quote(testChat, original.ID)
	if err != nil || *got.Text != "latest edit" {
		t.Fatal("historical copy overwrote edit")
	}
	if err := s.Delete(testChat, original.ID); err != nil {
		t.Fatal(err)
	}
	put(t, s, original, true)
	if _, err := s.Quote(testChat, original.ID); err == nil {
		t.Fatal("historical copy restored deletion")
	}
	if err := s.Delete(testChat, "not-yet-seen"); err != nil {
		t.Fatal(err)
	}
	original.ID = "not-yet-seen"
	put(t, s, original, true)
	if len(items(t, s, "whatsapp.message.list", map[string]any{"chat_id": testChat})) != 0 {
		t.Fatal("deletion before history was lost")
	}
	*now = now.Add(31 * 24 * time.Hour)
	if err := s.Prune(); err != nil {
		t.Fatal(err)
	}
	var guards int
	if err := s.db.QueryRow("SELECT COUNT(*) FROM cache_mutations").Scan(&guards); err != nil || guards != 0 {
		t.Fatal("mutation identifiers exceeded retention")
	}
}

func TestChatClearCutoffRejectsUnseenHistoryAndPreservesNewMessages(t *testing.T) {
	s, now := openTest(t)
	cutoff := now.Add(-time.Hour)
	put(t, s, Message{ID: "older", ChatID: testChat, Timestamp: cutoff.Add(-time.Minute), Text: text("old")}, false)
	put(t, s, Message{ID: "newer", ChatID: testChat, Timestamp: cutoff.Add(time.Minute), Text: text("new")}, false)
	if err := s.ClearChatAt(testChat, true, cutoff); err != nil {
		t.Fatal(err)
	}
	for _, historical := range []bool{false, true} {
		put(t, s, Message{ID: "not-previously-seen", ChatID: testChat, Timestamp: cutoff, Text: text("old unseen")}, historical)
	}
	if got := items(t, s, "whatsapp.message.list", map[string]any{"chat_id": testChat}); len(got) != 1 || got[0]["id"] != "newer" {
		t.Fatalf("old clear removed newer content or replayed history: %#v", got)
	}
	if got := items(t, s, "whatsapp.chat.list", map[string]any{}); len(got) != 1 || got[0]["last_message_at"] != iso(cutoff.Add(time.Minute)) {
		t.Fatalf("old clear removed newer conversation: %#v", got)
	}
	// Replaying an even older app-state clear cannot weaken the saved cutoff.
	if err := s.ClearChatAt(testChat, false, cutoff.Add(-time.Minute)); err != nil {
		t.Fatal(err)
	}
	put(t, s, Message{ID: "still-old", ChatID: testChat, Timestamp: cutoff.Add(-time.Second), Text: text("old")}, true)
	if got := items(t, s, "whatsapp.message.list", map[string]any{"chat_id": testChat}); len(got) != 1 {
		t.Fatalf("stale clear weakened cutoff: %#v", got)
	}
}

func TestDeletedChatHistoryMetadataAndAliasCannotRestoreOldContent(t *testing.T) {
	s, now := openTest(t)
	lid := "999999999999@lid"
	if err := s.Chat(lid, "saved conversation"); err != nil {
		t.Fatal(err)
	}
	if err := s.ClearChatAt(lid, true, now.Add(-time.Minute)); err != nil {
		t.Fatal(err)
	}
	if err := s.Alias(lid, testChat); err != nil {
		t.Fatal(err)
	}
	if err := s.ChatHistorical(lid, "replayed conversation"); err != nil {
		t.Fatal(err)
	}
	put(t, s, Message{ID: "unseen", ChatID: lid, Timestamp: now.Add(-2 * time.Minute), Text: text("old")}, true)
	if got := items(t, s, "whatsapp.chat.list", map[string]any{}); len(got) != 0 {
		t.Fatalf("deleted conversation restored through alias/history: %#v", got)
	}
	put(t, s, Message{ID: "fresh", ChatID: lid, Timestamp: *now, Text: text("new")}, false)
	if err := s.ChatHistorical(testChat, "known name"); err != nil {
		t.Fatal(err)
	}
	if got := items(t, s, "whatsapp.chat.list", map[string]any{}); len(got) != 1 || got[0]["id"] != testChat {
		t.Fatalf("new message did not restore conversation: %#v", got)
	}
	*now = now.Add(31 * 24 * time.Hour)
	if err := s.Prune(); err != nil {
		t.Fatal(err)
	}
	var count int
	if err := s.db.QueryRow("SELECT COUNT(*) FROM chat_clears").Scan(&count); err != nil || count != 0 {
		t.Fatalf("chat cutoff exceeded retention: %d %v", count, err)
	}
}

func TestAliasClearCutoffFiltersPreviouslyCachedCanonicalMessages(t *testing.T) {
	s, now := openTest(t)
	lid := "999999999999@lid"
	cutoff := now.Add(-time.Hour)
	put(t, s, Message{ID: "old", ChatID: testChat, Timestamp: cutoff.Add(-time.Minute), Text: text("old")}, false)
	put(t, s, Message{ID: "new", ChatID: testChat, Timestamp: cutoff.Add(time.Minute), Text: text("new")}, false)
	if err := s.ClearChatAt(lid, true, cutoff); err != nil {
		t.Fatal(err)
	}
	if err := s.Alias(lid, testChat); err != nil {
		t.Fatal(err)
	}
	got := items(t, s, "whatsapp.message.list", map[string]any{"chat_id": testChat})
	if len(got) != 1 || got[0]["id"] != "new" {
		t.Fatalf("alias merge did not enforce chat deletion cutoff: %#v", got)
	}
}

func TestStreamedInsertProtectsUnsupportedAndMissingOriginals(t *testing.T) {
	for _, originalPresent := range []bool{false, true} {
		t.Run(map[bool]string{false: "missing", true: "unsupported"}[originalPresent], func(t *testing.T) {
			s, now := openTest(t)
			original := Message{ID: "reply", ChatID: museID, SenderID: museID, Timestamp: *now, Type: "unsupported"}
			if originalPresent {
				put(t, s, original, false)
			}
			edited := original
			edited.Edited, edited.Text = true, text("latest stream")
			put(t, s, edited, false)
			put(t, s, original, true)
			edited.Text = text("stale historical edit")
			put(t, s, edited, true)
			q, err := s.Quote(museID, original.ID)
			if err != nil || *q.Text != "latest stream" {
				t.Fatal("history reverted streamed insertion")
			}
			edited.Text = text("new live stream")
			put(t, s, edited, false)
			q, err = s.Quote(museID, original.ID)
			if err != nil || *q.Text != "new live stream" {
				t.Fatal("later live stream was blocked")
			}
			if err := s.Delete(museID, original.ID); err != nil {
				t.Fatal(err)
			}
			put(t, s, edited, false)
			if _, err := s.Quote(museID, original.ID); err == nil {
				t.Fatal("streamed replacement resurrected deleted reply")
			}
		})
	}
}
