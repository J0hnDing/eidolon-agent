// Package cache stores bounded, available WhatsApp history, never protocol secrets.
package cache

import (
	"crypto/sha256"
	"database/sql"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"math"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	_ "github.com/ncruces/go-sqlite3/driver"
)

const (
	retention    = 30 * 24 * time.Hour
	maxText      = 20_000
	maxPageBytes = 3 * 1024 * 1024
	museID       = "1807055946647697@bot"
)

// Error contains only a stable code safe to return through private stdio.
type Error struct{ Code string }

func (e *Error) Error() string  { return e.Code }
func failure(code string) error { return &Error{Code: code} }
func storage(err error) error {
	if err != nil {
		return failure("storage_failure")
	}
	return nil
}

var individual = regexp.MustCompile(`^(\d{6,20})(?::\d+)?@(s\.whatsapp\.net|lid|bot)$`)
var group = regexp.MustCompile(`^\d{6,20}(?:-\d{6,20})?@g\.us$`)
var phoneNumber = regexp.MustCompile(`^\+[1-9]\d{6,14}$`)

// Normalize accepts only the identifiers exposed by the integration contract.
func Normalize(id string) string {
	if phoneNumber.MatchString(id) {
		return id[1:] + "@s.whatsapp.net"
	}
	if match := individual.FindStringSubmatch(id); match != nil {
		return match[1] + "@" + match[2]
	}
	if group.MatchString(id) {
		return id
	}
	return ""
}

type Message struct {
	ID, ChatID, SenderID string
	FromMe               bool
	Timestamp            time.Time
	Text                 *string
	Type, ReplyTo        string
	ExpiresAt            *time.Time
	Ephemeral, ViewOnce  bool
	Edited               bool // A streamed replacement, including its first observed fragment.
}

type Store struct {
	db *sql.DB
	mu sync.Mutex
	// Now is injectable before concurrent use, for deterministic retention tests.
	Now func() time.Time
}

func iso(t time.Time) string { return t.UTC().Format("2006-01-02T15:04:05.000Z") }
func nullable(s string) any {
	if s == "" {
		return nil
	}
	return s
}
func truncate(s string, n int) string {
	r := []rune(s)
	if len(r) > n {
		return string(r[:n])
	}
	return s
}

func Open(directory string) (*Store, error) {
	if !filepath.IsAbs(directory) {
		return nil, failure("invalid_input")
	}
	if err := os.MkdirAll(directory, 0700); err != nil {
		return nil, storage(err)
	}
	db, err := sql.Open("sqlite3", filepath.Join(directory, "whatsapp.sqlite"))
	if err != nil {
		return nil, storage(err)
	}
	db.SetMaxOpenConns(1)
	s := &Store{db: db, Now: time.Now}
	_, err = db.Exec(`PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA secure_delete=ON; PRAGMA temp_store=MEMORY; PRAGMA busy_timeout=10000;
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS auth (key TEXT PRIMARY KEY,value BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS aliases (id TEXT PRIMARY KEY,canonical TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS contacts (id TEXT PRIMARY KEY,name TEXT NOT NULL,phone TEXT);
CREATE TABLE IF NOT EXISTS chats (id TEXT PRIMARY KEY,name TEXT NOT NULL,type TEXT NOT NULL,last_message_at TEXT);
CREATE TABLE IF NOT EXISTS messages (chat_id TEXT NOT NULL,id TEXT NOT NULL,sender_id TEXT NOT NULL,
from_me INTEGER NOT NULL,timestamp TEXT NOT NULL,text TEXT,type TEXT NOT NULL,reply_to_message_id TEXT,
expires_at INTEGER,quoted TEXT,PRIMARY KEY(chat_id,id));
CREATE INDEX IF NOT EXISTS messages_timestamp ON messages(timestamp,chat_id,id);
CREATE TABLE IF NOT EXISTS cache_mutations (chat_id TEXT NOT NULL,id TEXT NOT NULL,deleted INTEGER NOT NULL,updated_at INTEGER NOT NULL,PRIMARY KEY(chat_id,id));
CREATE TABLE IF NOT EXISTS chat_clears (chat_id TEXT PRIMARY KEY,cleared_at INTEGER NOT NULL,remove_chat INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS dispatches (id TEXT PRIMARY KEY,digest TEXT NOT NULL,state TEXT NOT NULL,result TEXT);`)
	if err == nil {
		for _, query := range []string{
			"UPDATE chats SET name='Muse' WHERE id=? AND name=id",
			"UPDATE contacts SET name='Muse' WHERE id=? AND name=id",
			"INSERT OR IGNORE INTO contacts(id,name,phone) SELECT id,'Muse',NULL FROM chats WHERE id=?",
		} {
			if _, err = db.Exec(query, museID); err != nil {
				break
			}
		}
	}
	if err == nil {
		err = s.prune()
	}
	if err == nil {
		err = s.MuseInit()
	}
	if err != nil {
		_ = db.Close()
		return nil, storage(err)
	}
	return s, nil
}

func (s *Store) Close() error { s.mu.Lock(); defer s.mu.Unlock(); return storage(s.db.Close()) }

// LegacyAuthPresent identifies an old linked device which this transport cannot
// remotely log out. It never reads authentication record values.
func (s *Store) LegacyAuthPresent() (bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	var present bool
	err := s.db.QueryRow("SELECT EXISTS(SELECT 1 FROM auth)").Scan(&present)
	return present, storage(err)
}
func (s *Store) Meta(key string) string { s.mu.Lock(); defer s.mu.Unlock(); return s.meta(key) }
func (s *Store) meta(key string) string {
	var value string
	_ = s.db.QueryRow("SELECT value FROM metadata WHERE key=?", key).Scan(&value)
	return value
}
func (s *Store) SetMeta(key, value string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	_, err := s.db.Exec("INSERT OR REPLACE INTO metadata VALUES(?,?)", key, value)
	return storage(err)
}
func (s *Store) Canonical(id string) string { s.mu.Lock(); defer s.mu.Unlock(); return s.canonical(id) }
func (s *Store) canonical(id string) string {
	id = Normalize(id)
	if id == "" {
		return ""
	}
	var canonical string
	if s.db.QueryRow("SELECT canonical FROM aliases WHERE id=?", id).Scan(&canonical) == nil && Normalize(canonical) != "" {
		return Normalize(canonical)
	}
	return id
}

func (s *Store) Alias(lid, pn string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	lid, pn = Normalize(lid), Normalize(pn)
	if !strings.HasSuffix(lid, "@lid") || !strings.HasSuffix(pn, "@s.whatsapp.net") {
		return failure("invalid_input")
	}
	tx, err := s.db.Begin()
	if err != nil {
		return storage(err)
	}
	defer tx.Rollback()
	statements := []struct {
		q string
		p []any
	}{
		{"INSERT OR REPLACE INTO aliases VALUES(?,?)", []any{lid, pn}},
		{`INSERT INTO chat_clears SELECT ?,cleared_at,remove_chat FROM chat_clears WHERE chat_id=?
ON CONFLICT(chat_id) DO UPDATE SET remove_chat=CASE WHEN excluded.cleared_at>chat_clears.cleared_at THEN excluded.remove_chat WHEN excluded.cleared_at=chat_clears.cleared_at THEN MAX(chat_clears.remove_chat,excluded.remove_chat) ELSE chat_clears.remove_chat END,cleared_at=MAX(chat_clears.cleared_at,excluded.cleared_at)`, []any{pn, lid}},
		{"INSERT OR IGNORE INTO contacts SELECT ?,name,phone FROM contacts WHERE id=?", []any{pn, lid}},
		{"INSERT OR IGNORE INTO chats SELECT ?,name,type,last_message_at FROM chats WHERE id=?", []any{pn, lid}},
		{"INSERT OR IGNORE INTO messages SELECT ?,id,sender_id,from_me,timestamp,text,type,reply_to_message_id,expires_at,quoted FROM messages WHERE chat_id=?", []any{pn, lid}},
		{`UPDATE messages AS target SET (text,type,quoted)=(SELECT source.text,source.type,source.quoted FROM messages AS source WHERE source.chat_id=? AND source.id=target.id)
WHERE target.chat_id=? AND EXISTS(SELECT 1 FROM cache_mutations AS source_edit WHERE source_edit.chat_id=? AND source_edit.id=target.id AND source_edit.deleted=0 AND source_edit.updated_at>COALESCE((SELECT updated_at FROM cache_mutations WHERE chat_id=? AND id=target.id),0))`, []any{lid, pn, lid, pn}},
		{`INSERT INTO cache_mutations SELECT ?,id,deleted,updated_at FROM cache_mutations WHERE chat_id=?
ON CONFLICT(chat_id,id) DO UPDATE SET deleted=MAX(cache_mutations.deleted,excluded.deleted),updated_at=MAX(cache_mutations.updated_at,excluded.updated_at)`, []any{pn, lid}},
		{"DELETE FROM messages WHERE chat_id=? AND EXISTS(SELECT 1 FROM cache_mutations WHERE chat_id=? AND id=messages.id AND deleted=1)", []any{pn, pn}},
		{"DELETE FROM messages WHERE chat_id=? AND timestamp<=COALESCE((SELECT strftime('%Y-%m-%dT%H:%M:%fZ',cleared_at/1000.0,'unixepoch') FROM chat_clears WHERE chat_id=?),'')", []any{pn, pn}},
		{"UPDATE messages SET sender_id=? WHERE sender_id=?", []any{pn, lid}},
		{"UPDATE contacts SET phone=? WHERE id=?", []any{"+" + strings.Split(pn, "@")[0], pn}},
		{"DELETE FROM contacts WHERE id=?", []any{lid}},
		{"DELETE FROM chats WHERE id=?", []any{lid}},
		{"DELETE FROM messages WHERE chat_id=?", []any{lid}},
		{"DELETE FROM cache_mutations WHERE chat_id=?", []any{lid}},
		{"DELETE FROM chat_clears WHERE chat_id=?", []any{lid}},
		{"UPDATE chats SET last_message_at=(SELECT MAX(timestamp) FROM messages WHERE chat_id=?) WHERE id=?", []any{pn, pn}},
		{"DELETE FROM chats WHERE id=? AND EXISTS(SELECT 1 FROM chat_clears WHERE chat_id=? AND remove_chat=1) AND NOT EXISTS(SELECT 1 FROM messages WHERE chat_id=?)", []any{pn, pn, pn}},
	}
	for _, stmt := range statements {
		if _, err = tx.Exec(stmt.q, stmt.p...); err != nil {
			return storage(err)
		}
	}
	return storage(tx.Commit())
}

func (s *Store) Contact(id, name, phone string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.contact(id, name, phone)
}
func (s *Store) contact(id, name, phone string) error {
	id = s.canonical(id)
	if id == "" || strings.HasSuffix(id, "@g.us") {
		return nil
	}
	var saved string
	_ = s.db.QueryRow("SELECT name FROM contacts WHERE id=?", id).Scan(&saved)
	if name == "" && saved != id {
		name = saved
	}
	if name == "" && id == museID {
		name = "Muse"
	}
	if name == "" {
		name = id
	}
	// Project a phone number only from a canonical phone-number identity.
	phone = ""
	if strings.HasSuffix(id, "@s.whatsapp.net") {
		candidate := "+" + strings.Split(id, "@")[0]
		if phoneNumber.MatchString(candidate) {
			phone = candidate
		}
	}
	_, err := s.db.Exec("INSERT OR REPLACE INTO contacts VALUES(?,?,?)", id, truncate(name, 256), nullable(phone))
	return storage(err)
}

func (s *Store) Chat(id, name string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.chat(id, name)
}

// ChatHistorical cannot recreate a deleted conversation from old sync metadata.
// A newer message may create the conversation again through PutMessage.
func (s *Store) ChatHistorical(id, name string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	id = s.canonical(id)
	if id == "" {
		return nil
	}
	var suppressed bool
	err := s.db.QueryRow(`SELECT EXISTS(SELECT 1 FROM chat_clears WHERE chat_id=? AND remove_chat=1)
AND NOT EXISTS(SELECT 1 FROM chats WHERE id=?)`, id, id).Scan(&suppressed)
	if err != nil {
		return storage(err)
	}
	if suppressed {
		return nil
	}
	return s.chat(id, name)
}
func (s *Store) chat(id, name string) error {
	id = s.canonical(id)
	if id == "" {
		return nil
	}
	var saved string
	_ = s.db.QueryRow("SELECT name FROM chats WHERE id=?", id).Scan(&saved)
	if name == "" && saved != id {
		name = saved
	}
	if name == "" && id == museID {
		name = "Muse"
	}
	if name == "" {
		name = id
	}
	typ := "direct"
	if strings.HasSuffix(id, "@g.us") {
		typ = "group"
	}
	if strings.HasSuffix(id, "@bot") {
		typ = "agent"
	}
	_, err := s.db.Exec(`INSERT INTO chats VALUES(?,?,?,NULL) ON CONFLICT(id) DO UPDATE SET name=excluded.name,type=excluded.type`, id, truncate(name, 256), typ)
	if err == nil && id == museID {
		return s.contact(id, name, "")
	}
	return storage(err)
}

func (s *Store) Prune() error { s.mu.Lock(); defer s.mu.Unlock(); return storage(s.prune()) }
func (s *Store) prune() error {
	_, err := s.db.Exec("DELETE FROM messages WHERE timestamp<? OR (expires_at IS NOT NULL AND expires_at<=?)", iso(s.Now().Add(-retention)), s.Now().UnixMilli())
	if err == nil {
		_, err = s.db.Exec("DELETE FROM cache_mutations WHERE updated_at<?", s.Now().Add(-retention).UnixMilli())
	}
	if err == nil {
		_, err = s.db.Exec("DELETE FROM chat_clears WHERE cleared_at<?", s.Now().Add(-retention).UnixMilli())
	}
	return err
}

func (s *Store) PutMessage(m Message, historical bool) error {
	return s.putMessage(m, historical, nil)
}

// PutMuseMessage records live incoming references atomically with their cached
// text. Exact parent metadata is supplied only by the private bridge handler.
func (s *Store) PutMuseMessage(m Message, historical bool, account string, ancestors []string, stream string) error {
	return s.putMessage(m, historical, &museIncomingObservation{account, ancestors, stream})
}

func (s *Store) putMessage(m Message, historical bool, observation *museIncomingObservation) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	m.ChatID = s.canonical(m.ChatID)
	if m.ChatID == "" || m.ID == "" || utf8.RuneCountInString(m.ID) > 256 || m.Timestamp.IsZero() || m.ViewOnce {
		return nil
	}
	now := s.Now()
	cleared, _ := strconv.ParseInt(s.meta("cleared_at"), 10, 64)
	if m.Timestamp.Before(now.Add(-retention)) || m.Timestamp.After(now.Add(24*time.Hour)) || (cleared > 0 && (historical || m.Timestamp.UnixMilli() <= cleared)) {
		return nil
	}
	if (m.Ephemeral && m.ExpiresAt == nil) || (m.ExpiresAt != nil && !m.ExpiresAt.After(now)) {
		return nil
	}
	var chatCleared bool
	if err := s.db.QueryRow("SELECT EXISTS(SELECT 1 FROM chat_clears WHERE chat_id=? AND cleared_at>=?)", m.ChatID, m.Timestamp.UnixMilli()).Scan(&chatCleared); err != nil {
		return storage(err)
	}
	if chatCleared {
		return nil
	}
	// A replayed original must never undo a prior edit or deletion. Mutation
	// guards retain identifiers only, and expire with the retention window.
	var deleted bool
	if err := s.db.QueryRow("SELECT deleted FROM cache_mutations WHERE chat_id=? AND id=?", m.ChatID, m.ID).Scan(&deleted); err == nil {
		if !m.Edited || historical || deleted {
			return nil
		}
	} else if !errors.Is(err, sql.ErrNoRows) {
		return storage(err)
	}
	m.SenderID = s.canonical(m.SenderID)
	if m.SenderID == "" {
		m.SenderID = m.ChatID
	}
	if m.Text != nil && utf8.RuneCountInString(*m.Text) > maxText {
		m.Text = nil
		m.Type = "unsupported_oversize"
	}
	if m.Text != nil {
		m.Type = "text"
	}
	if m.Type == "" {
		m.Type = "unsupported"
	}
	if len(m.Type) > 64 {
		m.Type = "unsupported"
	}
	if utf8.RuneCountInString(m.ReplyTo) > 256 {
		m.ReplyTo = ""
	}
	var expires any
	if m.ExpiresAt != nil {
		expires = m.ExpiresAt.UnixMilli()
	}
	var quoted any
	if m.Text != nil {
		q, _ := json.Marshal(map[string]any{"key": map[string]any{"id": m.ID, "remoteJid": m.ChatID, "fromMe": m.FromMe, "participant": m.SenderID}, "message": map[string]any{"conversation": *m.Text}})
		quoted = string(q)
	}
	if err := s.chat(m.ChatID, ""); err != nil {
		return err
	}
	var text any
	if m.Text != nil {
		text = *m.Text
	}
	tx, err := s.db.Begin()
	if err != nil {
		return storage(err)
	}
	defer tx.Rollback()
	var existed bool
	if observation != nil {
		if err = tx.QueryRow("SELECT EXISTS(SELECT 1 FROM messages WHERE chat_id=? AND id=?)", m.ChatID, m.ID).Scan(&existed); err != nil {
			return storage(err)
		}
	}
	_, err = tx.Exec("INSERT OR REPLACE INTO messages VALUES(?,?,?,?,?,?,?,?,?,?)", m.ChatID, m.ID, m.SenderID, m.FromMe, iso(m.Timestamp), text, m.Type, nullable(m.ReplyTo), expires, quoted)
	if err != nil {
		return storage(err)
	}
	if m.Edited {
		if _, err = tx.Exec("INSERT OR REPLACE INTO cache_mutations VALUES(?,?,0,?)", m.ChatID, m.ID, now.UnixMilli()); err != nil {
			return storage(err)
		}
	}
	if _, err = tx.Exec("UPDATE chats SET last_message_at=CASE WHEN last_message_at IS NULL OR last_message_at<? THEN ? ELSE last_message_at END WHERE id=?", iso(m.Timestamp), iso(m.Timestamp), m.ChatID); err != nil {
		return storage(err)
	}
	if observation != nil {
		if err = s.recordMuseIncoming(tx, m, historical, existed, observation); err != nil {
			return err
		}
	}
	return storage(tx.Commit())
}

func (s *Store) Edit(chat, id, text string) error {
	return s.edit(chat, id, text, false)
}

// Historical mutation copies may fill initial history but cannot override an
// edit or deletion already observed by this cache.
func (s *Store) EditHistorical(chat, id, text string) error {
	return s.edit(chat, id, text, true)
}
func (s *Store) edit(chat, id, text string, historical bool) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	chat = s.canonical(chat)
	if chat == "" || id == "" {
		return failure("invalid_input")
	}
	if historical {
		var exists bool
		if err := s.db.QueryRow("SELECT EXISTS(SELECT 1 FROM cache_mutations WHERE chat_id=? AND id=?)", chat, id).Scan(&exists); err != nil {
			return storage(err)
		}
		if exists {
			return nil
		}
	}
	var fromMe bool
	var sender string
	err := s.db.QueryRow("SELECT from_me,sender_id FROM messages WHERE chat_id=? AND id=?", chat, id).Scan(&fromMe, &sender)
	if errors.Is(err, sql.ErrNoRows) {
		return nil
	}
	if err != nil {
		return storage(err)
	}
	var value, quoted any
	typ := "unsupported_oversize"
	if utf8.RuneCountInString(text) <= maxText {
		value = text
		typ = "text"
		q, _ := json.Marshal(map[string]any{"key": map[string]any{"id": id, "remoteJid": chat, "fromMe": fromMe, "participant": sender}, "message": map[string]any{"conversation": text}})
		quoted = string(q)
	}
	tx, err := s.db.Begin()
	if err != nil {
		return storage(err)
	}
	defer tx.Rollback()
	if _, err = tx.Exec("UPDATE messages SET text=?,quoted=?,type=? WHERE chat_id=? AND id=?", value, quoted, typ, chat, id); err != nil {
		return storage(err)
	}
	if _, err = tx.Exec("INSERT OR REPLACE INTO cache_mutations VALUES(?,?,0,?)", chat, id, s.Now().UnixMilli()); err != nil {
		return storage(err)
	}
	return storage(tx.Commit())
}

func (s *Store) Delete(chat, id string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	chat = s.canonical(chat)
	if chat == "" || id == "" || utf8.RuneCountInString(id) > 256 {
		return failure("invalid_input")
	}
	tx, err := s.db.Begin()
	if err != nil {
		return storage(err)
	}
	defer tx.Rollback()
	if _, err = tx.Exec("INSERT OR REPLACE INTO cache_mutations VALUES(?,?,1,?)", chat, id, s.Now().UnixMilli()); err != nil {
		return storage(err)
	}
	if _, err = tx.Exec("DELETE FROM messages WHERE chat_id=? AND id=?", chat, id); err != nil {
		return storage(err)
	}
	return storage(tx.Commit())
}

// ClearChat applies an explicit deletion received from the linked account.
func (s *Store) ClearChat(chat string, removeChat bool) error {
	return s.ClearChatAt(chat, removeChat, s.Now())
}

// ClearChatAt uses the provider's deletion time, preserving newer messages and
// preventing older messages not yet seen locally from reappearing in history.
func (s *Store) ClearChatAt(chat string, removeChat bool, at time.Time) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	chat = s.canonical(chat)
	if chat == "" {
		return failure("invalid_input")
	}
	if at.IsZero() {
		at = s.Now()
	}
	tx, err := s.db.Begin()
	if err != nil {
		return storage(err)
	}
	defer tx.Rollback()
	if _, err = tx.Exec(`INSERT INTO chat_clears VALUES(?,?,?) ON CONFLICT(chat_id) DO UPDATE SET
remove_chat=CASE WHEN excluded.cleared_at>chat_clears.cleared_at THEN excluded.remove_chat WHEN excluded.cleared_at=chat_clears.cleared_at THEN MAX(chat_clears.remove_chat,excluded.remove_chat) ELSE chat_clears.remove_chat END,
cleared_at=MAX(chat_clears.cleared_at,excluded.cleared_at)`, chat, at.UnixMilli(), removeChat); err != nil {
		return storage(err)
	}
	var cutoff int64
	if err = tx.QueryRow("SELECT cleared_at,remove_chat FROM chat_clears WHERE chat_id=?", chat).Scan(&cutoff, &removeChat); err != nil {
		return storage(err)
	}
	if _, err = tx.Exec("INSERT OR REPLACE INTO cache_mutations SELECT chat_id,id,1,? FROM messages WHERE chat_id=? AND timestamp<=?", s.Now().UnixMilli(), chat, iso(time.UnixMilli(cutoff))); err != nil {
		return storage(err)
	}
	if _, err = tx.Exec("DELETE FROM messages WHERE chat_id=? AND timestamp<=?", chat, iso(time.UnixMilli(cutoff))); err != nil {
		return storage(err)
	}
	if removeChat {
		_, err = tx.Exec("DELETE FROM chats WHERE id=? AND NOT EXISTS(SELECT 1 FROM messages WHERE chat_id=?)", chat, chat)
	}
	if err != nil {
		return storage(err)
	}
	if _, err = tx.Exec("UPDATE chats SET last_message_at=(SELECT MAX(timestamp) FROM messages WHERE chat_id=?) WHERE id=?", chat, chat); err != nil {
		return storage(err)
	}
	return storage(tx.Commit())
}

func (s *Store) Clear() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	tx, err := s.db.Begin()
	if err != nil {
		return storage(err)
	}
	defer tx.Rollback()
	if _, err = tx.Exec("DELETE FROM messages; DELETE FROM chats; DELETE FROM contacts; DELETE FROM cache_mutations; DELETE FROM chat_clears"); err != nil {
		return storage(err)
	}
	if _, err = tx.Exec("INSERT OR REPLACE INTO metadata VALUES('cleared_at',?),('sync_status','cleared')", strconv.FormatInt(s.Now().UnixMilli(), 10)); err != nil {
		return storage(err)
	}
	if err = tx.Commit(); err != nil {
		return storage(err)
	}
	_, err = s.db.Exec("PRAGMA wal_checkpoint(TRUNCATE)")
	return storage(err)
}

// ResetAccount isolates a replacement account from all cached state belonging
// to its predecessor. The generation identifies this directory rather than a
// WhatsApp account; legacy encrypted authentication is deliberately untouched.
func (s *Store) ResetAccount() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	tx, err := s.db.Begin()
	if err != nil {
		return storage(err)
	}
	defer tx.Rollback()
	_, err = tx.Exec(`DELETE FROM messages;DELETE FROM chats;DELETE FROM contacts;DELETE FROM cache_mutations;DELETE FROM chat_clears;
DELETE FROM aliases;DELETE FROM dispatches;DELETE FROM metadata WHERE key<>'generation';`)
	if err != nil {
		return storage(err)
	}
	if err = tx.Commit(); err != nil {
		return storage(err)
	}
	_, err = s.db.Exec("PRAGMA wal_checkpoint(TRUNCATE)")
	return storage(err)
}

func (s *Store) Coverage() (map[string]any, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.coverage()
}
func (s *Store) coverage() (map[string]any, error) {
	if err := s.prune(); err != nil {
		return nil, storage(err)
	}
	var oldest sql.NullString
	if err := s.db.QueryRow("SELECT MIN(timestamp) FROM messages").Scan(&oldest); err != nil {
		return nil, storage(err)
	}
	state := s.meta("sync_status")
	if state == "" {
		state = "pending"
	}
	var first any
	if oldest.Valid {
		first = oldest.String
	}
	return map[string]any{"retention_cutoff": iso(s.Now().Add(-retention)), "oldest_message_at": first, "last_synced_at": nullable(s.meta("last_synced_at")), "sync_status": state}, nil
}
func (s *Store) Counts() (map[string]any, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.prune(); err != nil {
		return nil, storage(err)
	}
	result := map[string]any{}
	for _, table := range []string{"contacts", "chats", "messages"} {
		var count int64
		if err := s.db.QueryRow("SELECT COUNT(*) FROM " + table).Scan(&count); err != nil {
			return nil, storage(err)
		}
		result[table] = count
	}
	return result, nil
}

func integer(value any) (int64, bool) {
	switch v := value.(type) {
	case int:
		return int64(v), true
	case int64:
		return v, true
	case float64:
		if !math.IsNaN(v) && !math.IsInf(v, 0) && v == math.Trunc(v) && v >= 0 && v < 1e12 {
			return int64(v), true
		}
	case json.Number:
		n, err := v.Int64()
		return n, err == nil
	}
	return 0, false
}

func (s *Store) Read(operation string, input map[string]any) (map[string]any, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.prune(); err != nil {
		return nil, storage(err)
	}
	limit := int64(50)
	if value, ok := input["page_size"]; ok {
		var valid bool
		limit, valid = integer(value)
		if !valid || limit < 1 || limit > 100 {
			return nil, failure("invalid_input")
		}
	}
	allowed := map[string]bool{"query": true, "page_size": true, "cursor": true}
	switch operation {
	case "whatsapp.contact.search":
	case "whatsapp.chat.list":
		allowed["type"] = true
	case "whatsapp.message.list":
		allowed["chat_id"] = true
		delete(allowed, "query")
	case "whatsapp.message.search":
		allowed["chat_id"] = true
		allowed["since"] = true
		allowed["until"] = true
	default:
		return nil, failure("operation_undeclared")
	}
	for key, value := range input {
		if !allowed[key] {
			return nil, failure("invalid_input")
		}
		if key != "page_size" {
			v, ok := value.(string)
			if !ok || len(v) > 4096 {
				return nil, failure("invalid_input")
			}
		}
	}
	query, _ := input["query"].(string)
	query = strings.ToLower(query)
	filter := map[string]any{}
	for key, value := range input {
		if key != "cursor" {
			filter[key] = value
		}
	}
	filter["page_size"] = limit
	signatureData, _ := json.Marshal([]any{operation, filter})
	digest := sha256.Sum256(signatureData)
	signature := hex.EncodeToString(digest[:])
	offset := int64(0)
	if cursor, _ := input["cursor"].(string); cursor != "" {
		var token struct {
			Signature string `json:"signature"`
			Offset    int64  `json:"offset"`
		}
		raw, err := base64.RawURLEncoding.DecodeString(cursor)
		if err != nil || json.Unmarshal(raw, &token) != nil || token.Signature != signature || token.Offset < 0 || token.Offset > 1_000_000_000 {
			return nil, failure("invalid_input")
		}
		offset = token.Offset
	}
	var statement string
	params := []any{}
	switch operation {
	case "whatsapp.contact.search":
		statement = "SELECT id,name,phone FROM contacts WHERE instr(lower(name),?)>0 OR instr(id,?)>0 OR instr(COALESCE(phone,''),?)>0 ORDER BY name,id"
		params = []any{query, query, query}
	case "whatsapp.chat.list":
		statement = "SELECT id,name,type,last_message_at FROM chats WHERE (instr(lower(name),?)>0 OR instr(id,?)>0)"
		params = []any{query, query}
		if typ, _ := input["type"].(string); typ != "" {
			if typ != "direct" && typ != "group" && typ != "agent" {
				return nil, failure("invalid_input")
			}
			statement += " AND type=?"
			params = append(params, typ)
		}
		statement += " ORDER BY last_message_at DESC,id"
	default:
		chat, _ := input["chat_id"].(string)
		if operation == "whatsapp.message.list" && chat == "" || operation == "whatsapp.message.search" && strings.TrimSpace(query) == "" {
			return nil, failure("invalid_input")
		}
		statement = "SELECT id,chat_id,sender_id,from_me,timestamp,text,type,reply_to_message_id FROM messages WHERE 1=1"
		if chat != "" {
			chat = s.canonical(chat)
			if chat == "" {
				return nil, failure("invalid_input")
			}
			statement += " AND chat_id=?"
			params = append(params, chat)
		}
		if query != "" {
			statement += " AND instr(lower(text),?)>0"
			params = append(params, query)
		}
		var since, until time.Time
		for _, date := range []struct{ key, sign string }{{"since", ">="}, {"until", "<="}} {
			if value, ok := input[date.key].(string); ok {
				parsed, err := time.Parse(time.RFC3339Nano, value)
				if err != nil {
					return nil, failure("invalid_input")
				}
				if date.key == "since" {
					since = parsed
				} else {
					until = parsed
				}
				statement += " AND timestamp" + date.sign + "?"
				params = append(params, iso(parsed))
			}
		}
		if !since.IsZero() && !until.IsZero() && since.After(until) {
			return nil, failure("invalid_input")
		}
		statement += " ORDER BY timestamp DESC,chat_id,id"
	}
	params = append(params, limit+1, offset)
	rows, err := s.db.Query(statement+" LIMIT ? OFFSET ?", params...)
	if err != nil {
		return nil, storage(err)
	}
	columns, err := rows.Columns()
	if err != nil {
		rows.Close()
		return nil, storage(err)
	}
	items := []map[string]any{}
	more := false
	pageBytes := 2
	for rows.Next() {
		values := make([]any, len(columns))
		targets := make([]any, len(values))
		for i := range values {
			targets[i] = &values[i]
		}
		if err = rows.Scan(targets...); err != nil {
			rows.Close()
			return nil, storage(err)
		}
		item := map[string]any{}
		for i, column := range columns {
			value := values[i]
			if b, ok := value.([]byte); ok {
				value = string(b)
			}
			if column == "from_me" {
				switch v := value.(type) {
				case int64:
					value = v != 0
				case bool:
					value = v
				}
			}
			item[column] = value
		}
		encoded, _ := json.Marshal(item)
		if int64(len(items)) >= limit || pageBytes+len(encoded)+1 > maxPageBytes {
			more = true
			break
		}
		items = append(items, item)
		pageBytes += len(encoded) + 1
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return nil, storage(err)
	}
	if len(items) == 0 && more {
		return nil, failure("response_too_large")
	}
	var next any
	if more {
		token, _ := json.Marshal(map[string]any{"offset": offset + int64(len(items)), "signature": signature})
		next = base64.RawURLEncoding.EncodeToString(token)
	}
	coverage, err := s.coverage()
	if err != nil {
		return nil, err
	}
	return map[string]any{"items": items, "next_cursor": next, "coverage": coverage}, nil
}

func (s *Store) Quote(chat, id string) (*Message, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.prune(); err != nil {
		return nil, storage(err)
	}
	chat = s.canonical(chat)
	if chat == "" || id == "" {
		return nil, failure("invalid_input")
	}
	var m Message
	var text, reply, quoted sql.NullString
	var timestamp string
	err := s.db.QueryRow("SELECT id,chat_id,sender_id,from_me,timestamp,text,type,reply_to_message_id,quoted FROM messages WHERE chat_id=? AND id=?", chat, id).Scan(&m.ID, &m.ChatID, &m.SenderID, &m.FromMe, &timestamp, &text, &m.Type, &reply, &quoted)
	if errors.Is(err, sql.ErrNoRows) || err == nil && (!text.Valid || !quoted.Valid || m.Type != "text") {
		return nil, failure("not_found")
	}
	if err != nil {
		return nil, storage(err)
	}
	m.Timestamp, err = time.Parse(time.RFC3339Nano, timestamp)
	if err != nil {
		return nil, failure("storage_failure")
	}
	m.Text = &text.String
	m.ReplyTo = reply.String
	m.SenderID = s.canonical(m.SenderID)
	return &m, nil
}

func (s *Store) Dispatch(id, digest string) (map[string]any, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if id == "" || len(id) > 256 || digest == "" || len(digest) > 256 {
		return nil, failure("invalid_input")
	}
	var savedDigest, state string
	var result sql.NullString
	err := s.db.QueryRow("SELECT digest,state,result FROM dispatches WHERE id=?", id).Scan(&savedDigest, &state, &result)
	if err == nil {
		if savedDigest != digest {
			return nil, failure("dispatch_conflict")
		}
		if state != "accepted" || !result.Valid {
			return nil, failure("outcome_unknown")
		}
		var saved map[string]any
		if json.Unmarshal([]byte(result.String), &saved) != nil || saved == nil {
			return nil, failure("outcome_unknown")
		}
		return saved, nil
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return nil, storage(err)
	}
	_, err = s.db.Exec("INSERT INTO dispatches VALUES(?,?,'started',NULL)", id, digest)
	return nil, storage(err)
}

func (s *Store) Accepted(id string, result map[string]any) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	encoded, err := json.Marshal(result)
	if err != nil || len(encoded) > 16*1024 {
		return failure("invalid_input")
	}
	updated, err := s.db.Exec("UPDATE dispatches SET state='accepted',result=? WHERE id=? AND state='started'", string(encoded), id)
	if err != nil {
		return storage(err)
	}
	count, err := updated.RowsAffected()
	if err != nil {
		return storage(err)
	}
	if count != 1 {
		return failure("dispatch_conflict")
	}
	return nil
}
