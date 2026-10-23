import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { api } from "../api/client";
import type { SessionDetail, StreamEvent } from "../types";
import { initialLive, liveReducer } from "./streamReducer";

/** Loads a session snapshot and keeps it live via SSE (auto-reconnect with Last-Event-ID). */
export function useSessionStream(sessionId: string | null) {
  const [state, dispatch] = useReducer(liveReducer, initialLive);
  const [connected, setConnected] = useState(false);
  const lastId = useRef(0);
  lastId.current = state.lastEventId;

  const refetch = useCallback(async () => {
    if (!sessionId) return;
    const d = await api<SessionDetail>(`/api/sessions/${sessionId}`);
    dispatch({ type: "snapshot", detail: d });
  }, [sessionId]);

  useEffect(() => {
    dispatch({ type: "reset" });
    if (!sessionId) return;
    let es: EventSource | null = null;
    let closed = false;
    let retryTimer: number | undefined;
    const connect = async () => {
      try {
        const d = await api<SessionDetail>(`/api/sessions/${sessionId}`);
        if (closed) return;
        dispatch({ type: "snapshot", detail: d });
        const from = Math.max(lastId.current, d.last_event_id || 0);
        es = new EventSource(`/api/sessions/${sessionId}/events?last_event_id=${from}`, { withCredentials: true });
        es.onopen = () => setConnected(true);
        es.onerror = () => {
          setConnected(false);
          // the browser retries automatically with Last-Event-ID; if it gave up, reconnect manually
          if (es && es.readyState === EventSource.CLOSED && !closed) {
            retryTimer = window.setTimeout(connect, 3000);
          }
        };
        es.onmessage = (m) => handle(m.data);
        const types = [
          "turn.started", "turn.delta", "turn.thinking", "turn.completed", "turn.failed", "phase.changed", "usage.updated",
          "session.started", "session.completed", "session.failed", "session.pause_requested", "session.paused", "session.resumed",
          "checkpoint.started", "checkpoint.reported", "checkpoint.awaiting_user", "checkpoint.decision_received",
          "budget.warning", "budget.exhausted", "quota.refreshed", "quota.unavailable", "model.fallback_started",
          "model.handoff_completed", "model.quality_failed", "session.paused_budget", "file.uploaded", "file.processing",
          "file.ready", "file.partial", "file.failed", "file.attached", "file.detached", "chair.analysis_started",
          "chair.analysis_completed", "chair.analysis_failed", "chair.context_requested", "context_packet.created",
          "context_packet.distributed", "context_packet.invalidated", "evidence.requested", "evidence.searching",
          "evidence.awaiting_user", "evidence.user_responded", "evidence.revalidating", "evidence.resolved",
          "evidence.still_missing", "evidence.deferred", "compression.started", "compression.completed",
          "compression.failed", "warning", "error",
        ];
        types.forEach((t) => es!.addEventListener(t, (m) => handle((m as MessageEvent).data)));
      } catch {
        if (!closed) retryTimer = window.setTimeout(connect, 3000);
      }
    };
    const handle = (raw: string) => {
      try {
        const ev = JSON.parse(raw) as StreamEvent;
        dispatch({ type: "event", event: ev });
      } catch {
        /* ignore malformed */
      }
    };
    connect();
    return () => {
      closed = true;
      if (retryTimer) window.clearTimeout(retryTimer);
      es?.close();
      setConnected(false);
    };
  }, [sessionId]);

  // debounced snapshot refresh after structural events
  useEffect(() => {
    if (!state.needsRefetch || !sessionId) return;
    const t = window.setTimeout(() => {
      refetch().catch(() => undefined);
    }, 250);
    return () => window.clearTimeout(t);
  }, [state.needsRefetch, sessionId, refetch]);

  return { state, connected, refetch, dismiss: (id: number) => dispatch({ type: "dismiss", id }) };
}
