package cache

import (
	"path/filepath"
	"testing"
	"time"
)

func acceptedMusePrompt(t *testing.T, s *Store, now time.Time) {
	t.Helper()
	claimMuse(t, s, "exchange")
	if err := s.MuseAccepted("exchange", "accepted"); err != nil {
		t.Fatal(err)
	}
	put(t, s, Message{ID: "accepted", ChatID: museID, SenderID: testChat, FromMe: true,
		Timestamp: now, Text: text("prompt"), Type: "text"}, false)
}

func TestMuseSequentialReplyWithoutNativeParentSurvivesRestart(t *testing.T) {
	directory := filepath.Join(t.TempDir(), "cache")
	s, err := Open(directory)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC()
	s.Now = func() time.Time { return now }
	acceptedMusePrompt(t, s, now)
	now = now.Add(time.Second)
	put(t, s, Message{ID: "reply", ChatID: museID, SenderID: museID, Timestamp: now,
		Text: text("actual Muse reply"), Type: "text"}, false)
	if err = s.MuseObserve(testChat, nil, "reply", "last"); err != nil {
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
	result, err := s.MuseResult(testChat, 7, "exchange")
	if err != nil || result["status"] != "replied" || len(result["replies"].([]map[string]any)) != 1 {
		t.Fatalf("root-only reply was lost: %v %v", result, err)
	}
	if _, dispatch, err := s.MuseClaim(testChat, 7, "exchange", "digest", "retry"); err != nil || dispatch {
		t.Fatalf("recovery repeated the send: %v %v", dispatch, err)
	}
}

func TestMuseLiveParentlessReplyBeforeAcceptanceRecoversAfterRestart(t *testing.T) {
	directory := filepath.Join(t.TempDir(), "cache")
	s, err := Open(directory)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC()
	s.Now = func() time.Time { return now }
	claimMuse(t, s, "exchange")
	sentAt := now
	now = now.Add(time.Second)
	reply := Message{ID: "early-reply", ChatID: museID, SenderID: museID,
		Timestamp: now, Text: text("early response"), Type: "text"}
	if err = s.PutMuseMessage(reply, false, testChat, nil, "last"); err != nil {
		t.Fatal(err)
	}
	if err = s.MuseObserve(testChat, nil, reply.ID, "last"); err != nil {
		t.Fatal(err)
	}
	if err = s.MuseAccepted("exchange", "accepted"); err != nil {
		t.Fatal(err)
	}
	put(t, s, Message{ID: "accepted", ChatID: museID, SenderID: testChat, FromMe: true,
		Timestamp: sentAt, Text: text("prompt"), Type: "text"}, false)
	if err = s.Close(); err != nil {
		t.Fatal(err)
	}
	s, err = Open(directory)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	result, err := s.MuseResult(testChat, 7, "exchange")
	if err != nil || result["status"] != "replied" || len(result["replies"].([]map[string]any)) != 1 {
		t.Fatalf("early live event did not recover: %v %v", result, err)
	}
}

func TestMuseSequentialReplyRejectsAmbiguityAndUntrustedMessages(t *testing.T) {
	for _, kind := range []string{"phone_prompt", "competing_exchange", "uncertain_send", "unmatched_parent", "conflicting_quote", "old_reply", "other_account", "revoked", "deleted", "historical_only", "other_sender"} {
		t.Run(kind, func(t *testing.T) {
			s, now := museStore(t)
			acceptedMusePrompt(t, s, *now)
			*now = now.Add(2 * time.Second)
			reply := Message{ID: "reply", ChatID: museID, SenderID: museID, Timestamp: *now,
				Text: text("answer"), Type: "text"}
			var ancestors []string
			account := testChat
			switch kind {
			case "phone_prompt":
				put(t, s, Message{ID: "phone", ChatID: museID, SenderID: testChat, FromMe: true,
					Timestamp: now.Add(-time.Second), Text: text("user prompt"), Type: "text"}, false)
			case "competing_exchange":
				claimMuse(t, s, "other")
			case "uncertain_send":
				if _, err := s.db.Exec("UPDATE muse_exchanges SET state='outcome_unknown',accepted_id='' WHERE id='exchange'"); err != nil {
					t.Fatal(err)
				}
			case "unmatched_parent":
				ancestors = []string{"phone-prompt"}
			case "conflicting_quote":
				reply.ReplyTo = "phone-prompt"
			case "old_reply":
				reply.Timestamp = now.Add(-3 * time.Second)
			case "other_account":
				account = "15557654321@s.whatsapp.net"
			case "revoked":
				if err := s.MuseCancel(7); err != nil {
					t.Fatal(err)
				}
			case "other_sender":
				reply.SenderID = testChat
			}
			put(t, s, reply, kind == "historical_only")
			if kind == "historical_only" {
				// History does not call MuseObserve; caching alone cannot match.
				result, err := s.MuseResult(testChat, 7, "exchange")
				if err != nil || len(result["replies"].([]map[string]any)) != 0 {
					t.Fatalf("history inferred a reply: %v %v", result, err)
				}
				return
			}
			if kind == "deleted" {
				if err := s.Delete(museID, reply.ID); err != nil {
					t.Fatal(err)
				}
			}
			if err := s.MuseObserve(account, ancestors, reply.ID, "last"); err != nil {
				t.Fatal(err)
			}
			result, err := s.MuseResult(testChat, 7, "exchange")
			if err != nil || len(result["replies"].([]map[string]any)) != 0 {
				t.Fatalf("unsafe sequential match: %v %v", result, err)
			}
		})
	}
}

func TestMuseTaggedStreamWithoutTerminalMarkerSettlesAfterQuiet(t *testing.T) {
	for _, stream := range []string{"first", "inner"} {
		t.Run(stream, func(t *testing.T) {
			s, now := museStore(t)
			acceptedMusePrompt(t, s, *now)
			*now = now.Add(time.Second)
			putIncoming(t, s, "reply", *now, false, nil, stream)
			if err := s.MuseObserve(testChat, nil, "reply", stream); err != nil {
				t.Fatal(err)
			}
			*now = now.Add(9 * time.Second)
			result, err := s.MuseResult(testChat, 7, "exchange")
			if err != nil || result["status"] != "pending" {
				t.Fatalf("tagged stream settled too early: %v %v", result, err)
			}
			// Another fragment restarts the trailing quiet window.
			if err = s.MuseObserve(testChat, []string{"sent-exchange"}, "reply", stream); err != nil {
				t.Fatal(err)
			}
			*now = now.Add(9 * time.Second)
			result, err = s.MuseResult(testChat, 7, "exchange")
			if err != nil || result["status"] != "pending" {
				t.Fatalf("new fragment did not restart quiet period: %v %v", result, err)
			}
			*now = now.Add(time.Second)
			result, err = s.MuseResult(testChat, 7, "exchange")
			if err != nil || result["status"] != "replied" {
				t.Fatalf("missing terminal marker stranded reply: %v %v", result, err)
			}
		})
	}
}

func TestMuseIncomingTaggedStreamUsesSameQuietPeriod(t *testing.T) {
	s, now := museStore(t)
	putIncoming(t, s, "opening", *now, false, nil, "first")
	*now = now.Add(9 * time.Second)
	if items, _ := incoming(t, s, 0, time.Time{}); len(items) != 0 {
		t.Fatal("incoming stream settled too soon")
	}
	*now = now.Add(time.Second)
	if items, _ := incoming(t, s, 0, time.Time{}); len(items) != 1 {
		t.Fatal("incoming stream waited forever for missing terminal marker")
	}
}
