package cache

import (
	"encoding/json"
	"strings"
	"testing"
	"time"
)

func museStore(t *testing.T) (*Store, *time.Time) {
	t.Helper()
	s, now := openTest(t)
	if err := s.MuseInit(); err != nil {
		t.Fatal(err)
	}
	return s, now
}

func claimMuse(t *testing.T, s *Store, id string) {
	t.Helper()
	if _, dispatch, err := s.MuseClaim(testChat, 7, id, "digest", "sent-"+id); err != nil || !dispatch {
		t.Fatalf("claim: %v %v", dispatch, err)
	}
}

func TestMuseRestartKeepsUncertainAndAcceptedClaimsWithoutBodies(t *testing.T) {
	directory := t.TempDir()
	s, err := Open(directory)
	if err != nil {
		t.Fatal(err)
	}
	if err = s.MuseInit(); err != nil {
		t.Fatal(err)
	}
	claimMuse(t, s, "uncertain")
	claimMuse(t, s, "accepted")
	if err = s.MuseAccepted("accepted", "stable-message-id"); err != nil {
		t.Fatal(err)
	}
	if err = s.Close(); err != nil {
		t.Fatal(err)
	}
	s, err = Open(directory)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	if err = s.MuseInit(); err != nil {
		t.Fatal(err)
	}
	for _, id := range []string{"uncertain", "accepted"} {
		e, dispatch, err := s.MuseClaim(testChat, 7, id, "digest", "fresh-id")
		if err != nil || dispatch || e.SentID != "sent-"+id {
			t.Fatalf("restart replayed claim: %v %v", dispatch, err)
		}
		result, err := s.MuseResult(testChat, 7, id)
		if err != nil {
			t.Fatal(err)
		}
		if id == "uncertain" && result["status"] != "outcome_unknown" || id == "accepted" && (result["status"] != "pending" || result["sent_message_id"] != "stable-message-id") {
			t.Fatalf("restart changed receipt: %v", result)
		}
	}
	var columns string
	if err = s.db.QueryRow("SELECT sql FROM sqlite_master WHERE name='muse_exchanges'").Scan(&columns); err != nil {
		t.Fatal(err)
	}
	if strings.Contains(columns, "text") || strings.Contains(columns, "body") || strings.Contains(columns, "result TEXT") {
		t.Fatal("ledger duplicates plaintext")
	}
}

func TestMuseCurrentCacheHonorsPrivacyMutationsAndExpiry(t *testing.T) {
	s, now := museStore(t)
	claimMuse(t, s, "exchange")
	if err := s.MuseAccepted("exchange", "accepted"); err != nil {
		t.Fatal(err)
	}
	put(t, s, Message{ID: "reply", ChatID: museID, SenderID: museID, Timestamp: *now, Text: text("original"), Type: "text"}, false)
	if err := s.MuseObserve(testChat, []string{"sent-exchange"}, "reply", "last"); err != nil {
		t.Fatal(err)
	}
	if err := s.Edit(museID, "reply", "edited"); err != nil {
		t.Fatal(err)
	}
	result, err := s.MuseResult(testChat, 7, "exchange")
	if err != nil {
		t.Fatal(err)
	}
	if replies := result["replies"].([]map[string]any); result["status"] != "replied" || len(replies) != 1 || replies[0]["text"] != "edited" {
		t.Fatalf("stale body: %v", result)
	}
	if err = s.Delete(museID, "reply"); err != nil {
		t.Fatal(err)
	}
	result, err = s.MuseResult(testChat, 7, "exchange")
	if err != nil || result["status"] != "expired" || result["last_reply_at"] != nil || len(result["replies"].([]map[string]any)) != 0 {
		t.Fatalf("deleted completed reply did not expire: %v %v", result, err)
	}
	claimMuse(t, s, "ephemeral")
	expiry := now.Add(time.Second)
	put(t, s, Message{ID: "ephemeral-reply", ChatID: museID, SenderID: museID, Timestamp: *now, Text: text("private"), Type: "text", ExpiresAt: &expiry, Ephemeral: true}, false)
	if err = s.MuseObserve(testChat, []string{"sent-ephemeral"}, "ephemeral-reply", "last"); err != nil {
		t.Fatal(err)
	}
	*now = now.Add(2 * time.Second)
	result, err = s.MuseResult(testChat, 7, "ephemeral")
	if err != nil || result["status"] != "expired" || len(result["replies"].([]map[string]any)) != 0 {
		t.Fatalf("expired ephemeral body still answered or pending: %v %v", result, err)
	}
	*now = now.Add(31 * 24 * time.Hour)
	result, err = s.MuseResult(testChat, 7, "exchange")
	if err != nil || result["status"] != "expired" {
		t.Fatalf("exchange not expired: %v %v", result, err)
	}
	_, dispatch, err := s.MuseClaim(testChat, 7, "exchange", "digest", "retry")
	if err != nil || dispatch {
		t.Fatal("expired dispatch replayed")
	}
}

func TestMuseUnretainedMessagesNeverAssociate(t *testing.T) {
	for _, kind := range []string{"absent", "expired", "view_once", "oversize", "media", "from_me", "deleted", "retention"} {
		t.Run(kind, func(t *testing.T) {
			s, now := museStore(t)
			claimMuse(t, s, "exchange")
			if err := s.MuseAccepted("exchange", "accepted"); err != nil {
				t.Fatal(err)
			}
			message := Message{ID: "unretained", ChatID: museID, SenderID: museID, Timestamp: *now, Text: text("private body"), Type: "text"}
			switch kind {
			case "expired":
				expired := now.Add(-time.Second)
				message.ExpiresAt = &expired
			case "view_once":
				message.ViewOnce = true
			case "oversize":
				message.Text = text(strings.Repeat("x", maxText+1))
			case "media":
				message.Text = nil
				message.Type = "media"
			case "from_me":
				message.FromMe = true
			case "retention":
				message.Timestamp = now.Add(-retention - time.Second)
			}
			if kind != "absent" {
				put(t, s, message, false)
			}
			if kind == "deleted" {
				if err := s.Delete(museID, message.ID); err != nil {
					t.Fatal(err)
				}
			}
			if err := s.MuseObserve(testChat, []string{"sent-exchange"}, message.ID, "last"); err != nil {
				t.Fatal(err)
			}
			result, err := s.MuseResult(testChat, 7, "exchange")
			if err != nil || result["status"] != "pending" || len(result["replies"].([]map[string]any)) != 0 {
				t.Fatalf("unretained message advanced exchange: %v %v", result, err)
			}
			var refs int
			if err = s.db.QueryRow("SELECT COUNT(*) FROM muse_replies").Scan(&refs); err != nil || refs != 0 {
				t.Fatalf("unretained reply reference persisted: %d %v", refs, err)
			}
		})
	}
}

func TestMuseClearPersistsCancellationAndNeverRestoresCachedBody(t *testing.T) {
	s, now := museStore(t)
	claimMuse(t, s, "exchange")
	put(t, s, Message{ID: "reply", ChatID: museID, SenderID: museID, Timestamp: *now, Text: text("sensitive"), Type: "text"}, false)
	if err := s.MuseObserve(testChat, []string{"sent-exchange"}, "reply", "last"); err != nil {
		t.Fatal(err)
	}
	if err := s.Clear(); err != nil {
		t.Fatal(err)
	}
	// Replayed history and even fresh unsolicited response cannot restore a
	// cancelled exchange or reconstruct its deleted plaintext.
	put(t, s, Message{ID: "reply", ChatID: museID, SenderID: museID, Timestamp: *now, Text: text("sensitive"), Type: "text"}, true)
	if err := s.MuseObserve(testChat, []string{"sent-exchange"}, "reply", "last"); err != nil {
		t.Fatal(err)
	}
	result, err := s.MuseResult(testChat, 7, "exchange")
	if err != nil || result["status"] != "cancelled" || len(result["replies"].([]map[string]any)) != 0 {
		t.Fatalf("clear resurrected: %v %v", result, err)
	}
	_, dispatch, err := s.MuseClaim(testChat, 7, "exchange", "digest", "retry")
	if err != nil || dispatch {
		t.Fatal("clear replayed dispatch")
	}
	var replies int
	if err = s.db.QueryRow("SELECT COUNT(*) FROM muse_replies").Scan(&replies); err != nil || replies != 0 {
		t.Fatal("clear kept reply references")
	}
}

func TestMuseExactAncestryAmbiguityAndRevokedGrant(t *testing.T) {
	s, now := museStore(t)
	claimMuse(t, s, "one")
	claimMuse(t, s, "two")
	put(t, s, Message{ID: "reply", ChatID: museID, SenderID: museID, Timestamp: *now, Text: text("answer"), Type: "text"}, false)
	for _, ancestors := range [][]string{{"shared-wasa-root"}, {"sent-one", "sent-two"}} {
		if err := s.MuseObserve(testChat, ancestors, "reply", "last"); err != nil {
			t.Fatal(err)
		}
		result, err := s.MuseResult(testChat, 7, "one")
		if err != nil || len(result["replies"].([]map[string]any)) != 0 {
			t.Fatal("guessed ambiguous response")
		}
	}
	if err := s.MuseCancel(7); err != nil {
		t.Fatal(err)
	}
	_, _, err := s.MuseClaim(testChat, 7, "new", "digest", "new-id")
	requireCode(t, err, "grant_revoked")
	_, _, err = s.MuseClaim("other", 7, "one", "digest", "new-id")
	requireCode(t, err, "exchange_conflict")
	_, _, err = s.MuseClaim(testChat, 8, "one", "digest", "new-id")
	requireCode(t, err, "exchange_conflict")
}

func TestMuseReplyBoundPreservesWholeBodies(t *testing.T) {
	s, now := museStore(t)
	claimMuse(t, s, "exchange")
	for index := range 25 {
		id := string(rune('a' + index))
		body := strings.Repeat("\x00", 20_000)
		put(t, s, Message{ID: id, ChatID: museID, SenderID: museID, Timestamp: *now, Text: &body, Type: "text"}, false)
		if err := s.MuseObserve(testChat, []string{"sent-exchange"}, id, "last"); err != nil {
			t.Fatal(err)
		}
	}
	result, err := s.MuseResult(testChat, 7, "exchange")
	if err != nil {
		t.Fatal(err)
	}
	replies := result["replies"].([]map[string]any)
	encoded, err := json.Marshal(result)
	if err != nil || len(replies) != 20 || len(encoded) > 3*1024*1024 {
		t.Fatal("reply result exceeded bound")
	}
	for _, reply := range replies {
		if len(reply["text"].(string)) != 20_000 {
			t.Fatal("reply silently truncated")
		}
	}
}
