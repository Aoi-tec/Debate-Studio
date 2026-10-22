import type { Agent, Message } from "../types";
import { TIER_LABEL, stripContextRequest } from "../labels";

const COLORS = ["#5b8def", "#e8833a", "#3aa57a", "#b05bd6", "#d6455b", "#2fa4b5", "#8c7a3c"];

export function avatarFor(agent: Agent | undefined, idx: number) {
  return { emoji: agent?.avatar || (agent?.kind === "chair" ? "🧑‍⚖️" : "🤖"), color: COLORS[idx % COLORS.length] };
}

export function Bubble({ m, agent, idx }: { m: Message; agent?: Agent; idx: number }) {
  if (m.kind === "user" || m.kind === "system") {
    return (
      <div className={`row ${m.kind === "user" ? "row-user" : "row-system"}`} data-testid="bubble-user">
        <div className={m.kind === "user" ? "bubble bubble-user" : "system-note"}>
          {m.kind === "user" && <div className="bubble-meta">あなた（介入）</div>}
          <div className="bubble-text">{m.content}</div>
        </div>
      </div>
    );
  }
  const { text, request } = stripContextRequest(m.content);
  const av = avatarFor(agent, idx);
  const isChair = agent?.kind === "chair";
  const statusNote =
    m.status === "streaming" ? "入力中…" :
    m.status === "incomplete" ? "未確定（採用されていません）" :
    m.status === "failed" ? "失敗" :
    m.status === "quality_failed" ? "品質検証で不採用" : null;
  return (
    <div className={`row ${m.status !== "completed" && m.status !== "streaming" ? "row-dim" : ""}`} data-testid={`bubble-${m.status}`}>
      <div className="avatar" style={{ background: av.color }} title={agent?.name}>{av.emoji}</div>
      <div className="bubble-col">
        <div className="bubble-head">
          <strong>{agent?.name || m.agent_id}</strong>
          <span className="muted">{agent?.role_name}</span>
          {m.round ? <span className="chip">R{m.round}</span> : null}
          {m.phase && <span className="chip chip-ghost">{m.phase}</span>}
          {m.tier && <span className={`chip tier-${m.tier}`}>{TIER_LABEL[m.tier] || m.tier}</span>}
          {m.model_id && <span className="muted small">{m.model_id}</span>}
          {m.meta?.redo && <span className="chip chip-accent">根拠更新後の再発言</span>}
          {m.handoff_id && <span className="chip chip-warn">モデル引継ぎ</span>}
        </div>
        <div className={`bubble ${isChair ? "bubble-chair" : ""} ${m.status === "streaming" ? "bubble-streaming" : ""}`}>
          {!isChair && <div className="ctx-label">議長配布コンテキストで討論中{m.packet_refs?.[0] ? `（Packet v${m.packet_refs[0].packet_version}）` : "（資料なし）"}</div>}
          <div className="bubble-text">{text || (m.status === "streaming" ? "…" : "")}</div>
          {request && (
            <div className="cr-note">
              📮 議長へ追加根拠を要求：{request.question}
              <span className={`chip ${request.criticality === "noncritical" ? "" : "chip-danger"}`}>{request.criticality || "critical"}</span>
            </div>
          )}
          {m.meta?.invalid_citations && <div className="warn-note">⚠ 配布Packetにない出典ID: {m.meta.invalid_citations.join(", ")}（未検証）</div>}
          {statusNote && <div className="status-note">{statusNote}{m.meta?.error ? `：${m.meta.error}` : ""}{m.meta?.reason === "tier_budget_reached" ? "：Tier予算到達で中断" : ""}{m.meta?.problem ? `：${m.meta.problem}` : ""}</div>}
        </div>
      </div>
    </div>
  );
}
