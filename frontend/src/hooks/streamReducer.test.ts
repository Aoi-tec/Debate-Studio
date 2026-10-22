import { describe, expect, it } from "vitest";
import { initialLive, liveReducer } from "./streamReducer";
import type { SessionDetail } from "../types";

const detail = (over: Partial<SessionDetail> = {}): SessionDetail =>
  ({
    id: "s1", topic: "t", state: "DEBATE", created_at: "", started_at: null, ended_at: null, error: null,
    progress: { phase: "DEBATE", round: 1, rounds_target: 2, independent_done: [], debate_done: [], pending_redo: [] },
    agents: [], settings: { debate_rounds: 2 }, final_report: null, conclusion_status: null, blockers: [], messages: [],
    usage: { input_tokens: 0, output_tokens: 0, total_tokens: 0, estimated_cost: null, cost_complete: false, by_agent: {} },
    attachments: [], evidence_requests: [], last_event_id: 10, budgets: [], baselines: [], handoffs: [], analysis_runs: [],
    ...over,
  }) as SessionDetail;

const ev = (id: number, type: string, extra: any = {}) => ({ event_id: id, session_id: "s1", timestamp: "2026-01-01T00:00:00Z", type, ...extra });

describe("liveReducer", () => {
  it("joins deltas per message_id in order and ignores replayed duplicates", () => {
    let s = liveReducer(initialLive, { type: "snapshot", detail: detail() });
    s = liveReducer(s, { type: "event", event: ev(11, "turn.started", { message_id: "m1", agent_id: "a" }) });
    s = liveReducer(s, { type: "event", event: ev(12, "turn.started", { message_id: "m2", agent_id: "b" }) });
    s = liveReducer(s, { type: "event", event: ev(13, "turn.delta", { message_id: "m1", delta: "その案" }) });
    s = liveReducer(s, { type: "event", event: ev(14, "turn.delta", { message_id: "m2", delta: "別案" }) });
    s = liveReducer(s, { type: "event", event: ev(15, "turn.delta", { message_id: "m1", delta: "には課題が" }) });
    // reconnect replays 13..15 → must be ignored
    s = liveReducer(s, { type: "event", event: ev(13, "turn.delta", { message_id: "m1", delta: "その案" }) });
    s = liveReducer(s, { type: "event", event: ev(15, "turn.delta", { message_id: "m1", delta: "には課題が" }) });
    const m1 = s.detail!.messages.find((m) => m.id === "m1")!;
    const m2 = s.detail!.messages.find((m) => m.id === "m2")!;
    expect(m1.content).toBe("その案には課題が");
    expect(m2.content).toBe("別案");
    expect(Object.keys(s.typing).sort()).toEqual(["m1", "m2"]);
    s = liveReducer(s, { type: "event", event: ev(16, "turn.completed", { message_id: "m1" }) });
    expect(s.typing).toEqual({ m2: "b" });
    expect(s.detail!.messages.find((m) => m.id === "m1")!.status).toBe("completed");
  });

  it("ignores events older than the snapshot", () => {
    let s = liveReducer(initialLive, { type: "snapshot", detail: detail({ last_event_id: 50 }) });
    s = liveReducer(s, { type: "event", event: ev(40, "turn.started", { message_id: "old", agent_id: "a" }) });
    expect(s.detail!.messages).toHaveLength(0);
  });

  it("marks partial failures as incomplete and requests a refetch on structural events", () => {
    let s = liveReducer(initialLive, { type: "snapshot", detail: detail() });
    s = liveReducer(s, { type: "event", event: ev(11, "turn.started", { message_id: "m1", agent_id: "a" }) });
    s = liveReducer(s, { type: "event", event: ev(12, "turn.failed", { message_id: "m1", partial: true }) });
    expect(s.detail!.messages[0].status).toBe("incomplete");
    const before = s.needsRefetch;
    s = liveReducer(s, { type: "event", event: ev(13, "evidence.awaiting_user", {}) });
    expect(s.needsRefetch).toBe(before + 1);
  });

  it("keeps longer locally-streamed content when a snapshot arrives mid-stream", () => {
    let s = liveReducer(initialLive, { type: "snapshot", detail: detail() });
    s = liveReducer(s, { type: "event", event: ev(11, "turn.started", { message_id: "m1", agent_id: "a" }) });
    s = liveReducer(s, { type: "event", event: ev(12, "turn.delta", { message_id: "m1", delta: "abc" }) });
    const snap = detail({ last_event_id: 12, messages: [{ id: "m1", agent_id: "a", seq: 5, round: 1, phase: "DEBATE", kind: "turn", content: "", status: "streaming", tier: null, profile_id: null, model_id: null, created_at: "", completed_at: null, handoff_id: null, scope: [], packet_refs: [], meta: {} }] });
    s = liveReducer(s, { type: "snapshot", detail: snap });
    expect(s.detail!.messages[0].content).toBe("abc");
  });
});
