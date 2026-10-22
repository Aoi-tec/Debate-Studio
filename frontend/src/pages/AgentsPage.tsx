import { useEffect, useState } from "react";
import { api, errorText } from "../api/client";
import { TIER_LABEL } from "../labels";
import type { Agent, Profile, Role, Tier } from "../types";

const TIERS: Tier[] = ["primary_cloud", "downstream_cloud", "local_llm"];

export default function AgentsPage() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [roles, setRoles] = useState<Role[]>([]);
  const [maxAgents, setMax] = useState(10);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const load = async () => {
    const [a, p, r] = await Promise.all([api("/api/agents"), api("/api/profiles"), api("/api/roles")]);
    setAgents(a.agents);
    setMax(a.max_agents);
    setProfiles(p.profiles);
    setRoles(r.roles);
  };
  useEffect(() => {
    load().catch((e) => setErr(errorText(e)));
  }, []);
  const participants = agents.filter((a) => a.kind === "participant");
  const upd = (id: string, patch: Partial<Agent>) => setAgents(agents.map((a) => (a.id === id ? { ...a, ...patch } : a)));
  const add = () => {
    let n = participants.length + 1;
    let id = `agent_${String.fromCharCode(96 + n)}`;
    while (agents.some((a) => a.id === id)) { n++; id = `agent_${n}`; }
    const base = participants[participants.length - 1];
    setAgents([...agents, { ...base, id, name: `Agent ${String.fromCharCode(64 + Math.min(n, 26))}`, avatar: "🤖", role_id: base.role_id }]);
  };
  const remove = () => {
    if (participants.length <= 3) return;
    const last = participants[participants.length - 1];
    setAgents(agents.filter((a) => a.id !== last.id));
  };
  const save = async () => {
    setErr("");
    setMsg("");
    try {
      const r = await api("/api/agents", { method: "PUT", body: { agents } });
      setAgents(r.agents);
      setMsg("保存しました（実行中の討論には影響せず、次のセッションから有効）");
    } catch (e) {
      setErr(errorText(e));
    }
  };
  const total = agents.length;
  return (
    <div className="page">
      <h2>エージェント編成</h2>
      <div className="stepper">
        <button className="btn" onClick={remove} disabled={participants.length <= 3} title="一般Agentは最低3人">−</button>
        <span className="stepper-n" data-testid="agent-count">{total}</span>
        <button className="btn" onClick={add} disabled={total >= maxAgents}>＋</button>
        <span className="muted small">議長1＋一般Agent{participants.length}（最小4人・上限{maxAgents}人）</span>
        {total >= 7 && <span className="warn-note small">人数が多いほど1ラウンドあたりのAPI呼び出しと費用が増えます</span>}
      </div>
      <div className="agent-grid">
        {agents.map((a) => (
          <div key={a.id} className={`agent-card ${a.kind}`}>
            <div className="agent-card-head">
              <input className="avatar-input" value={a.avatar || ""} onChange={(e) => upd(a.id, { avatar: e.target.value })} maxLength={4} />
              <input value={a.name} onChange={(e) => upd(a.id, { name: e.target.value })} />
              <code className="muted small">{a.id}</code>
              {a.kind === "chair" && <span className="chip chip-accent">議長（削除不可）</span>}
            </div>
            <label className="field inline"><span>役割</span>
              <select value={a.role_id} disabled={a.kind === "chair"} onChange={(e) => upd(a.id, { role_id: e.target.value })}>
                {roles.filter((r) => (a.kind === "chair" ? r.id === "chair" : r.id !== "chair")).map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
              </select>
            </label>
            <label className="field inline"><span>API（主）</span>
              <select value={a.profile_id} onChange={(e) => upd(a.id, { profile_id: e.target.value })}>
                {profiles.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
              </select>
            </label>
            <label className="field inline"><span>モデル上書き</span>
              <input value={a.model_override || ""} placeholder="（プロファイルの既定モデル）" onChange={(e) => upd(a.id, { model_override: e.target.value || null })} />
            </label>
            {a.kind === "participant" && <label className="small"><input type="checkbox" checked={a.enabled} onChange={(e) => upd(a.id, { enabled: e.target.checked })} /> 参加</label>}
            <div className="muted small">権限: {a.kind === "chair" ? "資料解析・検索・Context Packet配布" : "テキスト討論のみ（資料解析は付与できません）"}</div>
            <details>
              <summary className="small">フォールバックとトークン予算</summary>
              <table className="tier-table">
                <thead><tr><th>階層</th><th>接続</th><th>予算(入力+出力)</th></tr></thead>
                <tbody>
                  {TIERS.map((t) => (
                    <tr key={t}>
                      <td>{TIER_LABEL[t]}</td>
                      <td>
                        <select value={(t === "primary_cloud" ? a.routing_profiles[t] || a.profile_id : a.routing_profiles[t]) || ""}
                          onChange={(e) => upd(a.id, { routing_profiles: { ...a.routing_profiles, [t]: e.target.value || null } })}>
                          {t !== "primary_cloud" && <option value="">（なし）</option>}
                          {profiles.map((p) => <option key={p.id} value={p.id}>{p.name}{t !== "primary_cloud" && p.quality.status !== "passed" ? "（品質未検証）" : ""}</option>)}
                        </select>
                      </td>
                      <td><input type="number" min={0} placeholder="上限なし" value={a.tier_token_budgets[t] ?? ""}
                        onChange={(e) => upd(a.id, { tier_token_budgets: { ...a.tier_token_budgets, [t]: e.target.value === "" ? null : Number(e.target.value) } })} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div className="muted small">主クラウド→下流クラウド→ローカルの順に切替。下流は「品質検証」に合格した接続だけが使われます。</div>
            </details>
          </div>
        ))}
      </div>
      {err && <div className="err">{err}</div>}
      {msg && <div className="ok">{msg}</div>}
      <button className="btn btn-primary" onClick={save}>保存</button>
    </div>
  );
}
