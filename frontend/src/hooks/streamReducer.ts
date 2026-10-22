import type { Message, SessionDetail, StreamEvent } from "../types";

/** Live state for a session: server snapshot + SSE deltas.
 *  - Events at or below ``lastEventId`` are ignored (no duplicates after reconnect).
 *  - turn.delta fragments are concatenated per message_id only.
 *  - Structural events ask the caller to refetch the snapshot (``needsRefetch``). */
export interface LiveState {
  detail: SessionDetail | null;
  lastEventId: number;
  typing: Record<string, string>; // message_id -> agent_id while streaming
  needsRefetch: number;
  toasts: { id: number; kind: string; text: string }[];
}

export const initialLive: LiveState = { detail: null, lastEventId: 0, typing: {}, needsRefetch: 0, toasts: [] };

const STRUCTURAL = /^(phase\.|session\.|checkpoint\.|evidence\.|file\.|context_packet\.|chair\.|model\.|budget\.|quota\.|compression\.|usage\.)/;

export type Action =
  | { type: "snapshot"; detail: SessionDetail }
  | { type: "event"; event: StreamEvent }
  | { type: "dismiss"; id: number }
  | { type: "reset" };

function upsertMessage(list: Message[], m: Message): Message[] {
  const i = list.findIndex((x) => x.id === m.id);
  if (i < 0) return [...list, m].sort((a, b) => a.seq - b.seq || a.created_at.localeCompare(b.created_at));
  const copy = list.slice();
  copy[i] = { ...copy[i], ...m };
  return copy;
}

export function liveReducer(state: LiveState, action: Action): LiveState {
  switch (action.type) {
    case "reset":
      return initialLive;
    case "dismiss":
      return { ...state, toasts: state.toasts.filter((t) => t.id !== action.id) };
    case "snapshot": {
      const prev = state.detail;
      const incoming = action.detail;
      // keep longer local content for messages still streaming (server stores content at completion)
      const messages = incoming.messages.map((m) => {
        const local = prev?.messages.find((x) => x.id === m.id);
        if (local && m.status === "streaming" && local.content.length > m.content.length) return { ...m, content: local.content };
        return m;
      });
      const extra = (prev?.messages || []).filter((x) => x.status === "streaming" && !incoming.messages.some((m) => m.id === x.id));
      const typing: Record<string, string> = {};
      [...messages, ...extra].forEach((m) => {
        if (m.status === "streaming" && m.agent_id) typing[m.id] = m.agent_id;
      });
      return {
        ...state,
        detail: { ...incoming, messages: [...messages, ...extra].sort((a, b) => a.seq - b.seq) },
        lastEventId: Math.max(state.lastEventId, incoming.last_event_id || 0),
        typing,
      };
    }
    case "event": {
      const ev = action.event;
      if (ev.event_id <= state.lastEventId) return state; // duplicate after reconnect
      let next: LiveState = { ...state, lastEventId: ev.event_id };
      const d = state.detail;
      if (!d) return next;
      if (ev.type === "turn.started") {
        const m: Message = {
          id: ev.message_id, agent_id: ev.agent_id ?? null, seq: Number.MAX_SAFE_INTEGER - 1000 + (ev.event_id % 1000),
          round: ev.round ?? null, phase: ev.phase ?? null, kind: ev.kind || "turn", content: ev.content || "",
          status: ev.kind === "user" || ev.kind === "system" ? "completed" : "streaming", tier: ev.tier ?? null,
          profile_id: ev.profile_id ?? null, model_id: ev.model_id ?? null, created_at: ev.timestamp, completed_at: null,
          handoff_id: null, scope: [], packet_refs: [], meta: {},
        };
        next = { ...next, detail: { ...d, messages: upsertMessage(d.messages, m) } };
        if (m.status === "streaming" && m.agent_id) next.typing = { ...state.typing, [m.id]: m.agent_id };
        return next;
      }
      if (ev.type === "turn.delta") {
        const msgs = d.messages.map((m) => (m.id === ev.message_id ? { ...m, content: m.content + (ev.delta || "") } : m));
        return { ...next, detail: { ...d, messages: msgs } };
      }
      if (ev.type === "turn.completed" || ev.type === "turn.failed") {
        const status = ev.type === "turn.completed" ? "completed" : ev.partial ? "incomplete" : "failed";
        const msgs = d.messages.map((m) => (m.id === ev.message_id ? { ...m, status: status as Message["status"], tier: ev.tier ?? m.tier } : m));
        const typing = { ...state.typing };
        delete typing[ev.message_id];
        return { ...next, typing, detail: { ...d, messages: msgs }, needsRefetch: state.needsRefetch + 1 };
      }
      const toast = (kind: string, text: string) => ({ ...next, toasts: [...state.toasts.slice(-4), { id: ev.event_id, kind, text }] });
      if (ev.type === "warning") next = toast("warn", ev.message);
      else if (ev.type === "error") next = toast("error", ev.message);
      else if (ev.type === "model.fallback_started") next = toast("info", `モデル切替: ${ev.agent_id} ${ev.from_tier} → ${ev.to_tier}（${ev.reason}）`);
      else if (ev.type === "budget.warning") next = toast("warn", `予算警告: ${ev.agent_id} ${ev.tier} ${ev.used}/${ev.limit}`);
      if (STRUCTURAL.test(ev.type) || ev.type === "warning" || ev.type === "error") {
        next = { ...next, needsRefetch: next.needsRefetch + 1 };
      }
      return next;
    }
  }
}
