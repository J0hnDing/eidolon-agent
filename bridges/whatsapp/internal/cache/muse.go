package cache

import (
	"database/sql"
	"errors"
	"time"
)

// Muse exchanges retain dispatch identity and references, never duplicate bodies.
// Claims deliberately survive Clear and restart: an uncertain dispatch is final.
func (s *Store) MuseInit() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	_, err := s.db.Exec(`CREATE TABLE IF NOT EXISTS muse_exchanges (
id TEXT PRIMARY KEY,account TEXT NOT NULL,grant_id INTEGER NOT NULL,digest TEXT NOT NULL,
sent_id TEXT NOT NULL,accepted_id TEXT NOT NULL DEFAULT '',state TEXT NOT NULL,created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS muse_replies (
message_id TEXT PRIMARY KEY,exchange_id TEXT NOT NULL,last_event INTEGER NOT NULL,stream TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS muse_revoked_grants (grant_id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS muse_incoming_events (
id INTEGER PRIMARY KEY AUTOINCREMENT,account TEXT NOT NULL,message_id TEXT NOT NULL,
timestamp TEXT NOT NULL,observed_at INTEGER NOT NULL,last_event INTEGER NOT NULL,stream TEXT NOT NULL,
eligible INTEGER NOT NULL,parentless INTEGER NOT NULL DEFAULT 0,UNIQUE(account,message_id));
CREATE INDEX IF NOT EXISTS muse_sent ON muse_exchanges(account,sent_id);
CREATE INDEX IF NOT EXISTS muse_reply_exchange ON muse_replies(exchange_id);
CREATE TRIGGER IF NOT EXISTS muse_clear AFTER INSERT ON metadata WHEN NEW.key='cleared_at'
BEGIN UPDATE muse_exchanges SET state='cancelled'; DELETE FROM muse_replies; END;
CREATE TRIGGER IF NOT EXISTS muse_account_removed AFTER DELETE ON metadata WHEN OLD.key='account_id'
BEGIN INSERT OR IGNORE INTO muse_revoked_grants SELECT grant_id FROM muse_exchanges;
UPDATE muse_exchanges SET state='cancelled'; DELETE FROM muse_replies; END;
CREATE TRIGGER IF NOT EXISTS muse_account_changed BEFORE INSERT ON metadata
WHEN NEW.key='account_id' AND EXISTS(SELECT 1 FROM metadata WHERE key='account_id' AND value<>NEW.value)
BEGIN INSERT OR IGNORE INTO muse_revoked_grants SELECT grant_id FROM muse_exchanges;
UPDATE muse_exchanges SET state='cancelled'; DELETE FROM muse_replies; END;`)
	if err == nil {
		_, err = s.db.Exec(`CREATE TRIGGER IF NOT EXISTS muse_incoming_clear AFTER INSERT ON metadata WHEN NEW.key='cleared_at'
BEGIN UPDATE muse_incoming_events SET eligible=0; END;
CREATE TRIGGER IF NOT EXISTS muse_incoming_account_removed AFTER DELETE ON metadata WHEN OLD.key='account_id'
BEGIN UPDATE muse_incoming_events SET eligible=0; END;
CREATE TRIGGER IF NOT EXISTS muse_incoming_account_changed BEFORE INSERT ON metadata
WHEN NEW.key='account_id' AND EXISTS(SELECT 1 FROM metadata WHERE key='account_id' AND value<>NEW.value)
BEGIN UPDATE muse_incoming_events SET eligible=0; END;`)
	}
	return storage(err)
}

type MuseExchange struct {
	ID, Account, Digest, SentID, AcceptedID, State string
	GrantID                                        int64
	CreatedAt                                      int64
}

func (s *Store) museExchange(id string) (*MuseExchange, error) {
	e := &MuseExchange{}
	err := s.db.QueryRow("SELECT id,account,grant_id,digest,sent_id,accepted_id,state,created_at FROM muse_exchanges WHERE id=?", id).Scan(&e.ID, &e.Account, &e.GrantID, &e.Digest, &e.SentID, &e.AcceptedID, &e.State, &e.CreatedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, failure("not_found")
	}
	return e, storage(err)
}

// MuseClaim commits authority binding and the sole dispatch attempt before send.
func (s *Store) MuseClaim(account string, grant int64, id, digest, sentID string) (*MuseExchange, bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	e, err := s.museExchange(id)
	if err == nil {
		if e.Account != account || e.GrantID != grant || e.Digest != digest {
			return nil, false, failure("exchange_conflict")
		}
		return e, false, nil
	}
	if coded, ok := err.(*Error); !ok || coded.Code != "not_found" {
		return nil, false, err
	}
	var revoked bool
	if err = s.db.QueryRow("SELECT EXISTS(SELECT 1 FROM muse_revoked_grants WHERE grant_id=?)", grant).Scan(&revoked); err != nil {
		return nil, false, storage(err)
	}
	if revoked {
		return nil, false, failure("grant_revoked")
	}
	_, err = s.db.Exec("INSERT INTO muse_exchanges(id,account,grant_id,digest,sent_id,state,created_at) VALUES(?,?,?,?,?,'outcome_unknown',?)", id, account, grant, digest, sentID, s.Now().UnixMilli())
	if err != nil {
		return nil, false, storage(err)
	}
	e, err = s.museExchange(id)
	return e, true, err
}

func (s *Store) MuseAccepted(id, accepted string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	_, err := s.db.Exec("UPDATE muse_exchanges SET accepted_id=?,state=CASE WHEN state='outcome_unknown' THEN 'pending' ELSE state END WHERE id=?", accepted, id)
	return storage(err)
}

func (s *Store) MuseCancel(grant int64) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	tx, err := s.db.Begin()
	if err != nil {
		return storage(err)
	}
	defer tx.Rollback()
	if _, err = tx.Exec("INSERT OR IGNORE INTO muse_revoked_grants VALUES(?)", grant); err == nil {
		_, err = tx.Exec("UPDATE muse_exchanges SET state='cancelled' WHERE grant_id=?", grant)
	}
	if err != nil {
		return storage(err)
	}
	return storage(tx.Commit())
}

func (s *Store) MuseInvalidate() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	_, err := s.db.Exec("INSERT OR IGNORE INTO muse_revoked_grants SELECT grant_id FROM muse_exchanges; UPDATE muse_exchanges SET state='cancelled'; DELETE FROM muse_replies;")
	return storage(err)
}

// MuseObserve prefers provider supplied prompt/reply ancestry. When none is
// available, a single accepted, uninterrupted prompt can identify a live reply.
// A WASA root secret is shared and is never registered as ancestry.
func (s *Store) MuseObserve(account string, ancestors []string, replyID, stream string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if replyID == "" || len(replyID) > 256 {
		return nil
	}
	if err := s.prune(); err != nil {
		return storage(err)
	}
	// Cache insertion can intentionally ignore expired, deleted, view-once, or
	// unsupported messages. Only retained normalized incoming text can advance
	// an exchange; a successful PutMessage call alone does not establish that.
	var retained bool
	if err := s.db.QueryRow("SELECT EXISTS(SELECT 1 FROM messages WHERE chat_id=? AND id=? AND from_me=0 AND type='text' AND text IS NOT NULL)", museID, replyID).Scan(&retained); err != nil {
		return storage(err)
	}
	if !retained {
		return nil
	}
	var matched string
	for _, ancestor := range ancestors {
		if ancestor == "" || len(ancestor) > 256 {
			continue
		}
		rows, err := s.db.Query(`SELECT e.id FROM muse_exchanges e WHERE e.account=?
AND e.state IN ('pending','replied','outcome_unknown') AND e.created_at>?
AND (e.sent_id=? OR (e.accepted_id<>'' AND e.accepted_id=?) OR EXISTS(SELECT 1 FROM muse_replies r WHERE r.exchange_id=e.id AND r.message_id=?))`, account, s.Now().Add(-retention).UnixMilli(), ancestor, ancestor, ancestor)
		if err != nil {
			return storage(err)
		}
		for rows.Next() {
			var id string
			if err = rows.Scan(&id); err != nil {
				rows.Close()
				return storage(err)
			}
			if matched != "" && matched != id {
				rows.Close()
				return nil
			} // Ambiguous ancestry is never guessed.
			matched = id
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return storage(err)
		}
	}
	if matched == "" {
		var err error
		matched, err = s.sequentialMuseExchange(account, ancestors, replyID)
		if err != nil || matched == "" {
			return err
		}
	}
	_, err := s.db.Exec(`INSERT INTO muse_replies VALUES(?,?,?,?) ON CONFLICT(message_id) DO UPDATE SET
last_event=excluded.last_event,stream=excluded.stream WHERE muse_replies.exchange_id=excluded.exchange_id`, replyID, matched, s.Now().UnixMilli(), stream)
	if err == nil {
		_, err = s.db.Exec("UPDATE muse_exchanges SET state='pending' WHERE id=?", matched)
	}
	return storage(err)
}

// MuseResult joins current normalized messages, honoring edits, deletion,
// ephemeral expiry, Clear, and retention without storing response plaintext.
func (s *Store) MuseResult(account string, grant int64, id string) (map[string]any, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.prune(); err != nil {
		return nil, storage(err)
	}
	e, err := s.museExchange(id)
	if err != nil {
		return nil, err
	}
	if e.Account != account || e.GrantID != grant {
		return nil, failure("exchange_conflict")
	}
	if e.State != "cancelled" && e.CreatedAt <= s.Now().Add(-retention).UnixMilli() {
		e.State = "expired"
		if _, err = s.db.Exec("UPDATE muse_exchanges SET state='expired' WHERE id=?", id); err != nil {
			return nil, storage(err)
		}
	}
	if err = s.reconcileMuseSequential(e); err != nil {
		return nil, err
	}
	replies := []map[string]any{}
	var last any
	if e.State != "cancelled" && e.State != "expired" {
		rows, queryErr := s.db.Query(`SELECT m.id,m.text,m.timestamp,r.last_event,r.stream FROM muse_replies r
JOIN messages m ON m.id=r.message_id AND m.chat_id=? WHERE r.exchange_id=? AND m.from_me=0 AND m.type='text' AND m.text IS NOT NULL ORDER BY m.timestamp,m.id LIMIT 20`, museID, id)
		if queryErr != nil {
			return nil, storage(queryErr)
		}
		settled, anyReply := true, false
		for rows.Next() {
			var messageID, body, timestamp, stream string
			var updated int64
			if err = rows.Scan(&messageID, &body, &timestamp, &updated, &stream); err != nil {
				rows.Close()
				return nil, storage(err)
			}
			anyReply = true
			if s.museStreamPending(stream, updated) {
				settled = false
			}
			replies = append(replies, map[string]any{"message_id": messageID, "text": body, "timestamp": timestamp})
			last = iso(time.UnixMilli(updated))
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return nil, storage(err)
		}
		if anyReply && settled {
			e.State = "replied"
		}
		if !anyReply {
			var hadReplies bool
			if err = s.db.QueryRow("SELECT EXISTS(SELECT 1 FROM muse_replies WHERE exchange_id=?)", id).Scan(&hadReplies); err != nil {
				return nil, storage(err)
			}
			if hadReplies {
				// Once every associated body is unavailable, the old exchange
				// cannot claim a completed answer or later resurrect deleted text.
				e.State = "expired"
			}
		}
		if _, err = s.db.Exec("UPDATE muse_exchanges SET state=? WHERE id=?", e.State, id); err != nil {
			return nil, storage(err)
		}
	}
	coverage, err := s.coverage()
	if err != nil {
		return nil, err
	}
	return map[string]any{"exchange_id": id, "status": e.State, "sent_message_id": nullable(e.AcceptedID), "replies": replies, "last_reply_at": last, "coverage": coverage}, nil
}
