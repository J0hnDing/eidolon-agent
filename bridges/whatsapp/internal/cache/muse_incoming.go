package cache

import (
	"database/sql"
	"time"
)

type museIncomingObservation struct {
	account   string
	ancestors []string
	stream    string
}

func (s *Store) recordMuseIncoming(tx *sql.Tx, m Message, historical, existed bool, observation *museIncomingObservation) error {
	if observation.account == "" || m.ChatID != museID || m.SenderID != museID || m.FromMe || m.Text == nil || m.Type != "text" {
		return nil
	}
	eligible := !historical && !existed
	blocked := false
	parentless := true
	for _, ancestor := range observation.ancestors {
		if ancestor == "" || len(ancestor) > 256 || ancestor == m.ID {
			continue
		}
		parentless = false
		var known bool
		// All known exchange states suppress a new initiation, including a late
		// reply after cancellation or completion. A cached phone prompt also
		// identifies a reply to the user rather than a Muse-initiated message.
		err := tx.QueryRow(`SELECT EXISTS(SELECT 1 FROM muse_exchanges WHERE account=? AND
(sent_id=? OR (accepted_id<>'' AND accepted_id=?) OR EXISTS(SELECT 1 FROM muse_replies WHERE exchange_id=muse_exchanges.id AND message_id=?)))
OR EXISTS(SELECT 1 FROM messages WHERE chat_id=? AND id=? AND from_me=1)`, observation.account, ancestor, ancestor, ancestor, museID, ancestor).Scan(&known)
		if err != nil {
			return storage(err)
		}
		blocked = blocked || known
	}
	eligible = eligible && !blocked
	now := s.Now().UnixMilli()
	_, err := tx.Exec(`INSERT INTO muse_incoming_events(account,message_id,timestamp,observed_at,last_event,stream,eligible,parentless)
VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(account,message_id) DO NOTHING`, observation.account, m.ID, iso(m.Timestamp), now, now, observation.stream, eligible, parentless)
	if err != nil {
		return storage(err)
	}
	if !historical {
		// Existing events keep their eligibility and cursor identity. Edits may
		// settle a stream, but can never promote a historical/suppressed message.
		_, err = tx.Exec(`UPDATE muse_incoming_events SET last_event=?,stream=?,eligible=CASE WHEN ? THEN 0 ELSE eligible END
WHERE account=? AND message_id=?`, now, observation.stream, blocked, observation.account, m.ID)
	}
	return storage(err)
}

// MuseIncomingEdit updates only a previously observed event. Protocol edits of
// older cached messages therefore never create a new conversation.
func (s *Store) MuseIncomingEdit(account, messageID, stream string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	_, err := s.db.Exec("UPDATE muse_incoming_events SET last_event=?,stream=? WHERE account=? AND message_id=?", s.Now().UnixMilli(), stream, account, messageID)
	return storage(err)
}

// MuseIncoming returns reference-only, ordered live-message events. Its durable
// cursor advances over suppressed/unavailable events, but stops before a stream
// that is still changing. Bodies are always read from the rolling message cache.
func (s *Store) MuseIncoming(account string, afterID int64, since time.Time) (map[string]any, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.prune(); err != nil {
		return nil, storage(err)
	}
	rows, err := s.db.Query(`SELECT e.id,e.account,e.message_id,e.timestamp,e.observed_at,e.last_event,e.stream,e.eligible,e.parentless,
EXISTS(SELECT 1 FROM messages m WHERE m.chat_id=? AND m.id=e.message_id AND m.sender_id=? AND m.from_me=0 AND m.type='text' AND m.text IS NOT NULL),
EXISTS(SELECT 1 FROM muse_replies r WHERE r.message_id=e.message_id),
(SELECT COUNT(*) FROM muse_exchanges x WHERE x.account=e.account AND x.state IN ('pending','outcome_unknown') AND x.created_at>?)=1
AND EXISTS(SELECT 1 FROM muse_exchanges x WHERE x.account=e.account AND x.state IN ('pending','outcome_unknown') AND x.created_at>?
AND x.created_at<=e.observed_at AND (x.accepted_id='' OR EXISTS(SELECT 1 FROM messages sent WHERE sent.chat_id=? AND sent.id=x.accepted_id AND sent.from_me=1
AND NOT EXISTS(SELECT 1 FROM messages other WHERE other.chat_id=sent.chat_id AND other.from_me=1 AND other.id<>x.sent_id AND other.id<>x.accepted_id AND other.timestamp>=sent.timestamp))))
FROM muse_incoming_events e WHERE e.id>? ORDER BY e.id LIMIT 50`, museID, museID, s.Now().Add(-retention).UnixMilli(), s.Now().Add(-retention).UnixMilli(), museID, afterID)
	if err != nil {
		return nil, storage(err)
	}
	defer rows.Close()
	items := []map[string]any{}
	cursor := afterID
	for rows.Next() {
		var id, observed, lastEvent int64
		var eventAccount, messageID, timestamp, stream string
		var eligible, parentless, retained, associated, potentialOwner bool
		if err = rows.Scan(&id, &eventAccount, &messageID, &timestamp, &observed, &lastEvent, &stream, &eligible, &parentless, &retained, &associated, &potentialOwner); err != nil {
			return nil, storage(err)
		}
		messageTime, parseErr := time.Parse(time.RFC3339Nano, timestamp)
		include := eligible && retained && !associated && eventAccount == account && parseErr == nil &&
			(since.IsZero() || !messageTime.Before(since) && !time.UnixMilli(observed).Before(since))
		if include && (parentless && potentialOwner || s.museStreamPending(stream, lastEvent)) {
			break
		}
		cursor = id
		if include {
			items = append(items, map[string]any{"id": id, "message_id": messageID, "timestamp": timestamp})
		}
	}
	if err = rows.Err(); err != nil {
		return nil, storage(err)
	}
	return map[string]any{"items": items, "next_cursor": cursor}, nil
}
