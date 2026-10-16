// @vitest-environment jsdom
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ActTurn } from "../../api/client";
import { useAwaitingActReply } from "./useAwaitingActReply";

afterEach(() => { cleanup(); vi.useRealTimers(); });

it("closes the reply window at its deadline without waiting for backend polling", () => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-10-09T12:00:00Z"));
  const turn = {
    status: "running", live_messages: [{
      id: 12, role: "assistant", text: "Approve Duo", reply_status: "awaiting", reply_expires_at: "2026-10-09T12:00:05",
    }],
  } as ActTurn;
  const { result } = renderHook(() => useAwaitingActReply(turn));
  expect(result.current?.id).toBe(12);
  act(() => vi.advanceTimersByTime(5000));
  expect(result.current).toBeNull();
});
