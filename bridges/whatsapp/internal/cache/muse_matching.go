package cache

import (
	"database/sql"
	"errors"
	"time"
)

func (s *Store) museStreamPending(stream string, lastEvent int64) bool {
	quiet := 1500 * time.Millisecond
	switch stream {
	case "first", "inner":
		// Muse sometimes sends a whole rich-text answer marked "first" with no
		// terminal edit. Wait longer for tagged streams, but never indefinitely.
		quiet = 10 * time.Second
	case "":
	default:
		return false
	}
	return s.Now().Sub(time.UnixMilli(lastEvent)) < quiet
}

// sequentialMuseExchange is called with the store lock held. Muse normally
// supplies only its shared encryption root, so exact ancestry may be absent.
// In that case, associate a live reply only with a single accepted pending
// prompt whose retained outgoing message has not been followed by another send.
// The root itself is never evidence of ownership.
func (s *Store) sequentialMuseExchange(account string, ancestors []string, replyID string) (string, error) {
	for _, ancestor := range ancestors {
		if ancestor != "" && ancestor != replyID {
			return "", nil // An explicit, unmatched parent must never be overridden.
		}
	}
	var matched string
	err := s.db.QueryRow(`SELECT e.id FROM muse_exchanges e
JOIN messages sent ON sent.chat_id=? AND sent.id=e.accepted_id AND sent.from_me=1
JOIN messages reply ON reply.chat_id=sent.chat_id AND reply.id=? AND reply.from_me=0
AND reply.sender_id=? AND reply.type='text' AND reply.text IS NOT NULL
WHERE e.account=? AND e.state='pending' AND e.accepted_id<>'' AND e.created_at>?
AND reply.timestamp>=sent.timestamp
AND (reply.reply_to_message_id IS NULL OR reply.reply_to_message_id='' OR reply.reply_to_message_id=e.sent_id OR reply.reply_to_message_id=e.accepted_id)
AND NOT EXISTS(SELECT 1 FROM muse_revoked_grants WHERE grant_id=e.grant_id)
AND (SELECT COUNT(*) FROM muse_exchanges other WHERE other.account=e.account
AND other.state IN ('pending','outcome_unknown') AND other.created_at>?)=1
AND NOT EXISTS(SELECT 1 FROM messages other WHERE other.chat_id=sent.chat_id AND other.from_me=1
AND other.id<>e.sent_id AND other.id<>e.accepted_id AND other.timestamp>=sent.timestamp)
AND NOT EXISTS(SELECT 1 FROM muse_replies WHERE message_id=reply.id AND exchange_id<>e.id)`,
		museID, replyID, museID, account, s.Now().Add(-retention).UnixMilli(),
		s.Now().Add(-retention).UnixMilli()).Scan(&matched)
	if errors.Is(err, sql.ErrNoRows) {
		return "", nil
	}
	return matched, storage(err)
}

// reconcileMuseSequential retries only persisted live, parentless observations.
// A callback may arrive before SendMessage returns its accepted receipt. Cache
// history by itself is deliberately insufficient to infer a response.
func (s *Store) reconcileMuseSequential(exchange *MuseExchange) error {
	if exchange.State != "pending" || exchange.AcceptedID == "" {
		return nil
	}
	rows, err := s.db.Query(`SELECT message_id,last_event,stream FROM muse_incoming_events event
WHERE account=? AND eligible=1 AND parentless=1 AND observed_at>=?
AND NOT EXISTS(SELECT 1 FROM muse_replies WHERE message_id=event.message_id)
ORDER BY id LIMIT 50`, exchange.Account, exchange.CreatedAt)
	if err != nil {
		return storage(err)
	}
	type candidate struct {
		id, stream string
		lastEvent  int64
	}
	var candidates []candidate
	for rows.Next() {
		var event candidate
		if err = rows.Scan(&event.id, &event.lastEvent, &event.stream); err != nil {
			rows.Close()
			return storage(err)
		}
		candidates = append(candidates, event)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return storage(err)
	}
	for _, event := range candidates {
		matched, err := s.sequentialMuseExchange(exchange.Account, nil, event.id)
		if err != nil {
			return err
		}
		if matched == exchange.ID {
			if _, err = s.db.Exec("INSERT OR IGNORE INTO muse_replies VALUES(?,?,?,?)", event.id, matched, event.lastEvent, event.stream); err != nil {
				return storage(err)
			}
		}
	}
	return nil
}
