import { useEffect, useRef } from "react";

export function usePolling(callback: () => void | Promise<void>, enabled: boolean, intervalMs = 2000) {
  const callbackRef = useRef(callback);
  const isRunningRef = useRef(false);

  useEffect(() => {
    callbackRef.current = callback;
  }, [callback]);

  useEffect(() => {
    if (!enabled) return;
    const interval = window.setInterval(() => {
      if (isRunningRef.current) return;
      isRunningRef.current = true;
      Promise.resolve(callbackRef.current()).finally(() => {
        isRunningRef.current = false;
      });
    }, intervalMs);
    return () => window.clearInterval(interval);
  }, [enabled, intervalMs]);
}
