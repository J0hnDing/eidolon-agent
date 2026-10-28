package cache

import (
	"fmt"
	"testing"
	"time"
)

func putIncoming(t *testing.T, s *Store, id string, at time.Time, historical bool, parents []string, stream string) {
	t.Helper()
	err := s.PutMuseMessage(Message{ID: id, ChatID: museID, SenderID: museID, Timestamp: at, Text: text("private message"), Type: "text"}, historical, testChat, parents, stream)
	if err != nil {
		t.Fatal(err)
	}
}

func incoming(t *testing.T, s *Store, after int64, since time.Time) ([]map[string]any, int64) {
	t.Helper()
	result, err := s.MuseIncoming(testChat, after, since)
	if err != nil {
		t.Fatal(err)
	}
	return result["items"].([]map[string]any), result["next_cursor"].(int64)
}

func TestMuseIncomingSettlesOnceAndRetainsCursorOnStream(t *testing.T) {
	s, now := museStore(t)
	putIncoming(t, s, "first", *now, false, nil, "first")
	putIncoming(t, s, "second", *now, false, nil, "last")
	if items, cursor := incoming(t, s, 0, time.Time{}); len(items) != 0 || cursor != 0 {
		t.Fatalf("skipped unsettled first stream: %#v %d", items, cursor)
	}
	*now = now.Add(5 * time.Second)
	if items, cursor := incoming(t, s, 0, time.Time{}); len(items) != 0 || cursor != 0 {
		t.Fatal("marked incomplete stream settled merely from elapsed time")
	}
	if err := s.Edit(museID, "first", "complete private text"); err != nil {
		t.Fatal(err)
	}
	if err := s.MuseIncomingEdit(testChat, "first", "last"); err != nil {
		t.Fatal(err)
	}
	items, cursor := incoming(t, s, 0, time.Time{})
	if len(items) != 2 || items[0]["message_id"] != "first" || items[1]["message_id"] != "second" {
		t.Fatalf("wrong stable event order: %#v", items)
	}
	if _, copied := items[0]["text"]; copied {
		t.Fatal("event copied a message body")
	}
	putIncoming(t, s, "first", *now, false, nil, "last")
	if items, next := incoming(t, s, cursor, time.Time{}); len(items) != 0 || next != cursor {
		t.Fatalf("duplicate/stream edit restarted a conversation: %#v %d", items, next)
	}
	if err := s.MuseIncomingEdit(testChat, "not-observed", "last"); err != nil {
		t.Fatal(err)
	}
	putIncoming(t, s, "quiet", *now, false, nil, "")
	if items, _ := incoming(t, s, cursor, time.Time{}); len(items) != 0 {
		t.Fatal("unsettled text returned")
	}
	*now = now.Add(2 * time.Second)
	if items, _ := incoming(t, s, cursor, time.Time{}); len(items) != 1 || items[0]["message_id"] != "quiet" {
		t.Fatal("quiet message never settled")
	}
}

func TestMuseIncomingExcludesHistoricalCachedCorrelatedAndPhoneMessages(t *testing.T) {
	s, now := museStore(t)
	putIncoming(t, s, "historical", *now, true, nil, "last")
	putIncoming(t, s, "historical", *now, false, nil, "last")
	put(t, s, Message{ID: "already-cached", ChatID: museID, SenderID: museID, Timestamp: *now, Text: text("old cached text")}, true)
	putIncoming(t, s, "already-cached", *now, false, nil, "last")
	claimMuse(t, s, "pending")
	claimMuse(t, s, "cancelled")
	if err := s.MuseCancel(7); err != nil {
		t.Fatal(err)
	}
	putIncoming(t, s, "late-cancelled-reply", *now, false, []string{"sent-cancelled"}, "last")
	putIncoming(t, s, "pending-reply", *now, false, []string{"sent-pending"}, "last")
	put(t, s, Message{ID: "phone-prompt", ChatID: museID, SenderID: testChat, FromMe: true, Timestamp: *now, Text: text("phone prompt")}, false)
	putIncoming(t, s, "phone-reply", *now, false, []string{"phone-prompt"}, "last")
	for _, m := range []Message{
		{ID: "outgoing", ChatID: museID, SenderID: testChat, FromMe: true, Timestamp: *now, Text: text("outgoing")},
		{ID: "other-chat", ChatID: testChat, SenderID: testChat, Timestamp: *now, Text: text("other")},
		{ID: "wrong-sender", ChatID: museID, SenderID: testChat, Timestamp: *now, Text: text("other")},
	} {
		if err := s.PutMuseMessage(m, false, testChat, nil, "last"); err != nil {
			t.Fatal(err)
		}
	}
	putIncoming(t, s, "real-initiation", *now, false, nil, "last")
	items, cursor := incoming(t, s, 0, time.Time{})
	if len(items) != 1 || items[0]["message_id"] != "real-initiation" || cursor <= 1 {
		t.Fatalf("suppressed messages activated: %#v %d", items, cursor)
	}
}

func TestMuseIncomingPrivacyMutationsAndGrantBaseline(t *testing.T) {
	for _, mutation := range []string{"delete", "expire", "ephemeral", "clear", "account", "reset"} {
		t.Run(mutation, func(t *testing.T) {
			s, now := museStore(t)
			if err := s.SetMeta("account_id", testChat); err != nil {
				t.Fatal(err)
			}
			putIncoming(t, s, "private", *now, false, nil, "last")
			switch mutation {
			case "delete":
				if err := s.Delete(museID, "private"); err != nil {
					t.Fatal(err)
				}
			case "expire":
				*now = now.Add(retention + time.Second)
			case "ephemeral":
				if _, err := s.db.Exec("UPDATE messages SET expires_at=? WHERE id='private'", now.Add(time.Second).UnixMilli()); err != nil {
					t.Fatal(err)
				}
				*now = now.Add(2 * time.Second)
			case "clear":
				if err := s.Clear(); err != nil {
					t.Fatal(err)
				}
			case "account":
				if err := s.SetMeta("account_id", "other-account"); err != nil {
					t.Fatal(err)
				}
			case "reset":
				if err := s.ResetAccount(); err != nil {
					t.Fatal(err)
				}
			}
			if items, cursor := incoming(t, s, 0, time.Time{}); len(items) != 0 || cursor == 0 {
				t.Fatalf("private event exposed or cursor stranded: %#v %d", items, cursor)
			}
		})
	}
	s, now := museStore(t)
	putIncoming(t, s, "before-enable", *now, false, nil, "last")
	*now = now.Add(time.Minute)
	since := *now
	putIncoming(t, s, "late-old-message", now.Add(-time.Hour), false, nil, "last")
	putIncoming(t, s, "after-enable", *now, false, nil, "last")
	if items, _ := incoming(t, s, 0, since); len(items) != 1 || items[0]["message_id"] != "after-enable" {
		t.Fatalf("old history crossed grant baseline: %#v", items)
	}
}

func TestMuseIncomingAssociationAddedAfterCacheInsertSuppressesInitiation(t *testing.T) {
	s, now := museStore(t)
	putIncoming(t, s, "reply-associated-later", *now, false, nil, "last")
	if items, _ := incoming(t, s, 0, time.Time{}); len(items) != 1 {
		t.Fatal("new message reference missing")
	}
	claimMuse(t, s, "fallback-matched")
	if err := s.MuseObserve(testChat, []string{"sent-fallback-matched"}, "reply-associated-later", "last"); err != nil {
		t.Fatal(err)
	}
	if items, cursor := incoming(t, s, 0, time.Time{}); len(items) != 0 || cursor == 0 {
		t.Fatalf("associated reply triggered another conversation: %#v %d", items, cursor)
	}
}

func TestMuseIncomingDefersParentlessCandidateUntilPendingOwnerResolves(t *testing.T) {
	s, now := museStore(t)
	claimMuse(t, s, "starting")
	putIncoming(t, s, "early-reply", *now, false, nil, "last")
	if items, cursor := incoming(t, s, 0, time.Time{}); len(items) != 0 || cursor != 0 {
		t.Fatalf("parentless callback raced past uncertain send: %#v %d", items, cursor)
	}
	if err := s.MuseAccepted("starting", "accepted-message"); err != nil {
		t.Fatal(err)
	}
	put(t, s, Message{ID: "accepted-message", ChatID: museID, SenderID: testChat, FromMe: true, Timestamp: *now, Text: text("accepted prompt")}, false)
	if items, cursor := incoming(t, s, 0, time.Time{}); len(items) != 0 || cursor != 0 {
		t.Fatal("candidate raced past a sole accepted pending prompt")
	}
	if err := s.MuseObserve(testChat, []string{"sent-starting"}, "early-reply", "last"); err != nil {
		t.Fatal(err)
	}
	if items, cursor := incoming(t, s, 0, time.Time{}); len(items) != 0 || cursor == 0 {
		t.Fatal("associated candidate did not advance without an initiation")
	}
}

func TestMuseIncomingPaginationAndRestartPreserveReferenceIdentity(t *testing.T) {
	directory := t.TempDir()
	s, err := Open(directory)
	if err != nil {
		t.Fatal(err)
	}
	for i := range 55 {
		putIncoming(t, s, fmt.Sprintf("incoming-%02d", i), time.Now(), false, nil, "last")
	}
	items, cursor := incoming(t, s, 0, time.Time{})
	if len(items) != 50 || cursor != 50 {
		t.Fatalf("unbounded or incomplete page: %d %d", len(items), cursor)
	}
	if err = s.Close(); err != nil {
		t.Fatal(err)
	}
	s, err = Open(directory)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	items, next := incoming(t, s, cursor, time.Time{})
	if len(items) != 5 || next != 55 || items[0]["message_id"] != "incoming-50" {
		t.Fatalf("restart changed queue identity: %#v %d", items, next)
	}
}
