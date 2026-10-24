import { describe, expect, it } from "vitest";
import { ChatMessage, appendMessages, createConversation } from "../../lib/chatStore";
import { messageTimestampSeparators } from "./messageTimeline";

function message(id: number, createdAt?: string, kind?: ChatMessage["kind"]): ChatMessage {
  return { id, createdAt, kind, role: "user", content: "Hello" };
}

describe("conversation timestamp separators", () => {
  it("measures three hours from the previous message, not the previous separator", () => {
    const messages = [
      message(1, "2026-10-22T08:00:00Z"),
      message(2, "2026-10-22T10:59:59Z"),
      message(3, "2026-10-22T11:00:00Z"),
      message(4, "2026-10-22T14:00:00Z"),
    ];
    expect([...messageTimestampSeparators(messages).keys()]).toEqual([1, 4]);
  });

  it("starts a new group at local midnight even for a short gap", () => {
    const before = new Date(2026, 9, 22, 23, 59).toISOString();
    const after = new Date(2026, 9, 23, 0, 1).toISOString();
    expect([...messageTimestampSeparators([message(1, before), message(2, after)]).keys()]).toEqual([1, 2]);
  });

  it("skips missing, invalid and transient timestamps without resetting the gap", () => {
    const messages = [
      message(1), message(2, "invalid"), message(3, "2026-10-22T08:00:00"),
      message(4, "2026-10-22T10:59:00Z", "thinking"), message(5),
      message(6, "2026-10-22T13:00:00+02:00"),
    ];
    const separators = messageTimestampSeparators(messages);
    expect([...separators.keys()]).toEqual([3, 6]);
    expect(separators.get(3)?.toISOString()).toBe("2026-10-22T08:00:00.000Z");
  });

  it("stamps new local messages while preserving known and historical timestamps", () => {
    const existing = message(1);
    const known = message(3, "2026-10-22T08:00:00Z");
    const result = appendMessages({ ...createConversation("project"), messages: [existing] }, [message(2), known]);
    expect(result.messages[0].createdAt).toBeUndefined();
    expect(result.messages[1].createdAt).toBe(result.updatedAt);
    expect(result.messages[2].createdAt).toBe(known.createdAt);
  });
});
