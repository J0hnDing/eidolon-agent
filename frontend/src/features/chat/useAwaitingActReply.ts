import { useEffect, useState } from "react";
import { ActTurn } from "../../api/client";
import { parseBackendDateTime } from "../../lib/dateTime";
import { awaitingActReply } from "./actConversationState";

export function useAwaitingActReply(turn: ActTurn | null) {
  const [, refreshDeadline] = useState(0);
  const reply = awaitingActReply(turn);
  const deadline = reply?.reply_expires_at;
  useEffect(() => {
    if (!deadline) return;
    const timer = window.setTimeout(
      () => refreshDeadline((value) => value + 1),
      Math.max(0, parseBackendDateTime(deadline).getTime() - Date.now()),
    );
    return () => window.clearTimeout(timer);
  }, [deadline]);
  return reply;
}
