import { ChatMessage } from "../../lib/chatStore";
import { parseBackendDateTime } from "../../lib/dateTime";

const TIMESTAMP_GAP_MS = 3 * 60 * 60 * 1000;

export function messageTimestampSeparators(messages: ChatMessage[]): Map<number, Date> {
  const separators = new Map<number, Date>();
  let previous: Date | undefined;
  for (const message of messages) {
    if (!message.createdAt || message.kind === "thinking") continue;
    const date = parseBackendDateTime(message.createdAt);
    if (Number.isNaN(date.getTime())) continue;
    if (!previous || date.toDateString() !== previous.toDateString()
      || date.getTime() - previous.getTime() >= TIMESTAMP_GAP_MS) {
      separators.set(message.id, date);
    }
    previous = date;
  }
  return separators;
}
