import { useEffect, useMemo, useRef, useState } from "react";
import { api, errorText, uploadFiles } from "../api/client";
import { Bubble, avatarFor } from "../components/Bubble";
import { BlockerBar, CheckpointCard, EvidenceCard, FinalReportCard } from "../components/Cards";
import { useSessionStream } from "../hooks/useSessionStream";
import { FILE_STATUS_LABEL, STATE_LABEL, TIER_LABEL, fmtSize, fmtTokens } from "../labels";
import type { FileInfo, SessionDetail } from "../types";

const ACTIVE = ["INITIALIZING", "RESUMING", "DOCUMENT_PREP", "CHAIR_DOCUMENT_ANALYSIS", "CONTEXT_DISTRIBUTION", "OPENING", "INDEPENDENT", "CHAIR_REVIEW", "DEBATE", "CONTEXT_REQUESTED", "CHAIR_EVIDENCE_SEARCH", "EVIDENCE_REVALIDATION", "CHECKPOINT_REPORT", "FINALIZING"];

export default function DebateRoom({ sessionId, setSessionId }: { sessionId: string | null; setSessionId: (id: string | null) => void }) {
  const [sessions, setSessions] = useState<{ id: string; topic: string; state: string; created_at: string }[]>([]);
  const [showNew, setShowNew] = useState(false);
  const { state, connected, refetch, dismiss } = useSessionStream(sessionId);
  const d = state.detail;

  const loadSessions = () => api<{ sessions: any[] }>("/api/sessions").then((r) => setSessions(r.sessions)).catch(() => undefined);
  useEffect(() => {
    loadSessions();
  }, [sessionId, d?.state]);

  return (
    <div className="room">
      <aside className="room-left">
        <button className="btn btn-primary btn-block" onClick={() => setShowNew(true)}>＋ 新しい討論</button>
        <div className="session-list">
          {sessions.map((s) => (
            <button key={s.id} className={`session-item ${s.id === sessionId ? "active" : ""}`} onClick={() => { setShowNew(false); setSessionId(s.id); }}>
              <div className="session-topic">{s.topic}</div>
              <div className="muted small">{STATE_LABEL[s.state] || s.state}</div>
            </button>
          ))}
          {!sessions.length && <div className="muted small pad">まだ討論はありません</div>}
        </div>
      </aside>
      {showNew || !sessionId ? (
        <NewSession onCreated={(id) => { setShowNew(false); setSessionId(id); loadSessions(); }} />
      ) : d ? (
        <Session d={d} connected={connected} typing={state.typing} refetch={refetch} />
      ) : (
        <main className="room-main"><div className="pad muted">読み込み中…</div></main>
      )}
      <div className="toasts">
        {state.toasts.map((t) => (
          <div key={t.id} className={`toast toast-${t.kind}`} onClick={() => dismiss(t.id)}>{t.text}</div>
        ))}
      </div>
    </div>
  );
}

function NewSession({ onCreated }: { onCreated: (id: string) => void }) {
  const [topic, setTopic] = useState("");
  const [rounds, setRounds] = useState(2);
  const [files, setFiles] = useState<FileInfo[]>([]);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [drag, setDrag] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const add = async (list: FileList | File[] | null) => {
    if (!list || !list.length) return;
    setErr("");
    try {
      const r = await uploadFiles(Array.from(list));
      setFiles((f) => [...f, ...r.files]);
      if (r.errors.length) setErr(r.errors.map((x: any) => `${x.name}: ${x.message}`).join(" / "));
    } catch (e) {
      setErr(errorText(e));
    }
  };
  const create = async () => {
    setBusy(true);
    setErr("");
    try {
      const s = await api<SessionDetail>("/api/sessions", { body: { topic, attachment_ids: files.map((f) => f.file_id), debate_rounds: rounds } });
      onCreated(s.id);
    } catch (e) {
      setErr(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <main className="room-main new-session">
      <h2>新しい討論</h2>
      <label className="field">
        <span>議題</span>
        <textarea rows={5} value={topic} onChange={(e) => setTopic(e.target.value)} placeholder="例: 見積AIの設計比較（計算エンジンと対話UIの責務分離をどうするか）" />
      </label>
      <label className="field inline">
        <span>討論ラウンド</span>
        <input type="number" min={1} max={20} value={rounds} onChange={(e) => setRounds(Number(e.target.value))} />
      </label>
      <div className={`dropzone ${drag ? "drag" : ""}`} onDragOver={(e) => { e.preventDefault(); setDrag(true); }} onDragLeave={() => setDrag(false)}
        onDrop={(e) => { e.preventDefault(); setDrag(false); add(e.dataTransfer.files); }} onClick={() => fileRef.current?.click()}>
        📎 資料をドラッグ＆ドロップ、またはクリックして選択（txt / md / csv / json / pdf / docx / xlsx / pptx / 画像）
        <input ref={fileRef} type="file" multiple hidden onChange={(e) => add(e.target.files)} />
      </div>
      {files.length > 0 && (
        <ul className="file-chips">
          {files.map((f) => <li key={f.file_id}>{f.name} <span className="muted small">{fmtSize(f.size)}</span> <button className="link" onClick={() => setFiles(files.filter((x) => x.file_id !== f.file_id))}>×</button></li>)}
        </ul>
      )}
      <p className="muted small">資料は議長（Chair Orchestrator）だけが解析し、一般Agentには根拠付きのテキスト（Context Packet）のみ配布されます。配布先は作成後に右側の「資料」欄で変更できます。</p>
      {err && <div className="err">{err}</div>}
      <button className="btn btn-primary" disabled={busy || !topic.trim()} onClick={create}>作成（開始前に設定を確認できます）</button>
    </main>
  );
}

function Session({ d, connected, typing, refetch }: { d: SessionDetail; connected: boolean; typing: Record<string, string>; refetch: () => Promise<void> }) {
  const [text, setText] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const agents = useMemo(() => Object.fromEntries(d.agents.map((a, i) => [a.id, { a, i }])), [d.agents]);
  const openEvidence = d.evidence_requests.filter((e) => ["WAITING_USER_EVIDENCE", "STILL_MISSING", "REVALIDATING", "ANSWER_RECEIVED"].includes(e.state));
  const terminal = ["COMPLETED", "CANCELLED", "FAILED"].includes(d.state);

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [d.messages.length, d.messages[d.messages.length - 1]?.content.length, openEvidence.length]);

  const act = async (path: string, body?: any) => {
    setErr("");
    setBusy(true);
    try {
      await api(`/api/sessions/${d.id}/${path}`, { method: "POST", body });
      await refetch();
    } catch (e) {
      setErr(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  const send = async (files?: FileList | null) => {
    setErr("");
    try {
      let ids: string[] = [];
      if (files && files.length) {
        const r = await uploadFiles(Array.from(files));
        ids = r.files.map((f: any) => f.file_id);
        if (r.errors.length) setErr(r.errors.map((x: any) => `${x.name}: ${x.message}`).join(" / "));
      }
      if (!text.trim() && !ids.length) return;
      await api(`/api/sessions/${d.id}/messages`, { body: { text, attachment_ids: ids } });
      setText("");
      await refetch();
    } catch (e) {
      setErr(errorText(e));
    } finally {
      if (fileRef.current) fileRef.current.value = "";
    }
  };
  const typingNames = Object.values(typing).map((id) => agents[id]?.a.name).filter(Boolean);

  return (
    <>
      <main className="room-main">
        <header className="room-header">
          <div className="room-topic" title={d.topic}>{d.topic}</div>
          <div className="room-state">
            <span className={`state-pill state-${d.state}`}>{STATE_LABEL[d.state] || d.state}</span>
            {d.progress?.round ? <span className="muted small">ラウンド {d.progress.round}/{d.progress.rounds_target}</span> : null}
            <span className={`dot ${connected ? "on" : "off"}`} title={connected ? "ライブ接続中" : "再接続中"} />
          </div>
          <div className="room-controls">
            {d.state === "CREATED" && <button className="btn btn-primary" disabled={busy} onClick={() => act("start")}>開始</button>}
            {ACTIVE.includes(d.state) && <button className="btn" disabled={busy} onClick={() => act("pause")}>一時停止</button>}
            {["PAUSED", "PAUSED_BUDGET"].includes(d.state) && <button className="btn btn-primary" disabled={busy} onClick={() => act("resume")}>再開</button>}
            {!terminal && d.state !== "CREATED" && <button className="btn btn-danger-ghost" disabled={busy} onClick={() => { if (confirm("討論をキャンセルしますか？")) act("cancel"); }}>終了</button>}
            <a className="btn btn-ghost" href={`/api/sessions/${d.id}`} target="_blank" rel="noreferrer">履歴を開く</a>
          </div>
        </header>
        {err && <div className="err pad">{err}</div>}
        <BlockerBar d={d} onAction={(a, arg) => a === "skip" && act(`skip-agent/${arg}`)} />
        <div className="chat" data-testid="chat">
          {d.messages.map((m) => {
            if (m.kind === "checkpoint") {
              let rep: any = {};
              try { rep = JSON.parse(m.content); } catch { /* */ }
              const latest = [...d.messages].reverse().find((x) => x.kind === "checkpoint")?.id === m.id;
              return <CheckpointCard key={m.id} report={rep} sid={d.id} awaiting={latest && d.state === "WAITING_USER_DECISION"} blocked={openEvidence.length > 0} onDone={refetch} />;
            }
            if (m.kind === "final") {
              let rep: any = {};
              try { rep = JSON.parse(m.content); } catch { /* */ }
              return <FinalReportCard key={m.id} report={rep} status={d.conclusion_status} />;
            }
            const x = m.agent_id ? agents[m.agent_id] : undefined;
            return <Bubble key={m.id} m={m} agent={x?.a} idx={x?.i ?? 0} />;
          })}
          {openEvidence.map((er) => <EvidenceCard key={er.evidence_request_id + er.version} er={er} sid={d.id} onDone={refetch} />)}
          {typingNames.length > 0 && <div className="typing">{typingNames.join("、")} が入力中…</div>}
          <div ref={endRef} />
        </div>
        {!terminal && (
          <div className="composer">
            <input ref={fileRef} type="file" multiple hidden onChange={(e) => send(e.target.files)} />
            <button className="btn btn-icon" title="資料を添付" onClick={() => fileRef.current?.click()}>📎</button>
            <textarea rows={2} value={text} onChange={(e) => setText(e.target.value)} placeholder="介入メッセージ（次の安全な発言境界から全Agentに共有）"
              onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) send(); }} />
            <button className="btn btn-primary" disabled={!text.trim()} onClick={() => send()}>送信</button>
          </div>
        )}
      </main>
      <SidePanel d={d} typing={typing} refetch={refetch} />
    </>
  );
}

function SidePanel({ d, typing, refetch }: { d: SessionDetail; typing: Record<string, string>; refetch: () => Promise<void> }) {
  const [tab, setTab] = useState<"people" | "files" | "packets" | "usage">("people");
  const [packets, setPackets] = useState<any[]>([]);
  useEffect(() => {
    if (tab === "packets") api<{ packets: any[] }>(`/api/sessions/${d.id}/context-packets`).then((r) => setPackets(r.packets)).catch(() => undefined);
  }, [tab, d]);
  const typingSet = new Set(Object.values(typing));
  const lastTier = (aid: string) => [...d.messages].reverse().find((m) => m.agent_id === aid && m.tier)?.tier;
  const budgetRows = (aid: string) => d.budgets.filter((b) => b.agent_id === aid && (b.used_tokens || b.limit_tokens));
  const participants = d.agents.filter((a) => a.kind === "participant");
  const lastRun = d.analysis_runs[d.analysis_runs.length - 1];
  return (
    <aside className="room-right">
      <nav className="tabs">
        {(["people", "files", "packets", "usage"] as const).map((t) => (
          <button key={t} className={tab === t ? "active" : ""} onClick={() => setTab(t)}>{{ people: "参加者", files: "資料", packets: "Packet", usage: "使用量" }[t]}</button>
        ))}
      </nav>
      {tab === "people" && (
        <div className="panel">
          <div className="muted small">フェーズ: {d.progress?.phase}・未解決の根拠要求 {d.evidence_requests.filter((e) => !["RESOLVED", "WITHDRAWN"].includes(e.state)).length} 件</div>
          {d.agents.map((a, i) => {
            const av = avatarFor(a, i);
            const t = lastTier(a.id);
            return (
              <div key={a.id} className="person">
                <div className="avatar small" style={{ background: av.color }}>{av.emoji}</div>
                <div className="person-body">
                  <div><strong>{a.name}</strong> <span className="muted small">{a.role_name}</span> {typingSet.has(a.id) && <span className="chip chip-accent">入力中</span>}</div>
                  <div className="muted small">{a.kind === "chair" ? "資料解析・検索・配布権限あり" : "テキスト討論のみ"}{t ? ` ・現在 ${TIER_LABEL[t]}` : ""}</div>
                  {budgetRows(a.id).map((b) => (
                    <div key={b.tier} className="budget-row">
                      <span>{TIER_LABEL[b.tier]}</span>
                      <span>{fmtTokens(b.used_tokens)} / {b.limit_tokens ? fmtTokens(b.limit_tokens) : "上限なし"}</span>
                      {b.limit_tokens ? <div className="bar"><div style={{ width: `${Math.min(100, (100 * b.used_tokens) / b.limit_tokens)}%` }} /></div> : null}
                    </div>
                  ))}
                </div>
              </div>
            );
          })}
          {d.handoffs.length > 0 && (
            <div className="card-sec"><div className="card-sec-title">モデル切替履歴</div>
              <ul>{d.handoffs.map((h) => <li key={h.id} className="small">{h.agent_id}: {TIER_LABEL[h.from_tier]} → {TIER_LABEL[h.to_tier]}（{h.reason}）</li>)}</ul>
            </div>
          )}
          {d.evidence_requests.length > 0 && (
            <div className="card-sec"><div className="card-sec-title">根拠要求の履歴</div>
              <ul>{d.evidence_requests.map((e) => <li key={e.evidence_request_id} className="small">{e.agent_name}: {e.agent_question}（{e.state}）</li>)}</ul>
            </div>
          )}
        </div>
      )}
      {tab === "files" && <FilesPanel d={d} participants={participants} refetch={refetch} lastRun={lastRun} />}
      {tab === "packets" && (
        <div className="panel">
          <div className="muted small">議長の解析 → Context Packet作成 → Agent配布。一般Agentはこのテキストだけを読みます。</div>
          {packets.filter((p) => p.status === "active").map((p) => (
            <details key={p.packet_id + p.packet_version} className="packet">
              <summary>{d.agents.find((a) => a.id === p.recipient_agent_id)?.name} — v{p.packet_version}・根拠{p.evidence_count}件・約{p.token_estimate}tok・配布{p.deliveries.length}回</summary>
              <div className="small"><b>要約</b> {p.body.summary}</div>
              <ul className="small">{p.body.facts.map((f: any, i: number) => <li key={i}>{f.claim} <span className="muted">[{f.evidence_ids.join(", ")}]</span></li>)}</ul>
              {p.body.open_questions.length > 0 && <div className="small"><b>未解決</b> {p.body.open_questions.join(" / ")}</div>}
              {p.body.omitted_or_unreadable.length > 0 && <div className="small warn-note">読めなかった箇所: {p.body.omitted_or_unreadable.join(" / ")}</div>}
              <ul className="small muted">{p.body.evidence.map((e: any) => <li key={e.id}>{e.id}: {e.source_label}</li>)}</ul>
            </details>
          ))}
          {!packets.length && <div className="muted small">資料が添付されていないか、まだ解析前です。</div>}
          {packets.filter((p) => p.status !== "active").length > 0 && <div className="muted small">過去版 {packets.filter((p) => p.status !== "active").length} 件（失効・置換済み、監査用に保持）</div>}
        </div>
      )}
      {tab === "usage" && (
        <div className="panel">
          <div className="usage-total">合計 {fmtTokens(d.usage.total_tokens)} tok <span className="muted small">（入力 {fmtTokens(d.usage.input_tokens)} / 出力 {fmtTokens(d.usage.output_tokens)}）</span></div>
          <div className="muted small">概算費用: {d.usage.estimated_cost != null && d.usage.cost_complete ? `$${d.usage.estimated_cost.toFixed(4)}` : "未算出（単価未登録のモデルあり）"}</div>
          {Object.entries(d.usage.by_agent).map(([aid, u]: any) => (
            <div key={aid} className="usage-agent">
              <strong>{d.agents.find((a) => a.id === aid)?.name || aid}</strong> {fmtTokens(u.input + u.output)} tok <span className="muted small">[{u.sources.join(",")}]</span>
              <div className="muted small">{Object.entries(u.by_purpose).map(([p, n]: any) => `${p}: ${fmtTokens(n)}`).join(" ・ ")}</div>
            </div>
          ))}
          {d.baselines.length > 0 && (
            <div className="card-sec"><div className="card-sec-title">API口座の閾値（開始時に固定）</div>
              <ul>{d.baselines.map((b, i) => <li key={i} className="small">{b.pool_id}: {b.source === "disabled" ? "無効" : `U0=${fmtTokens(b.u0)} → 閾値 ${fmtTokens(b.limit_tokens)}（${b.source}${b.estimated ? "・推定" : ""}）`}</li>)}</ul>
              <div className="muted small">アプリ独自の自動上限であり、提供元の公式な残り枠ではありません。</div>
            </div>
          )}
        </div>
      )}
    </aside>
  );
}

function FilesPanel({ d, participants, refetch, lastRun }: { d: SessionDetail; participants: any[]; refetch: () => Promise<void>; lastRun: any }) {
  const [err, setErr] = useState("");
  const patch = async (fid: string, body: any) => {
    setErr("");
    try {
      await api(`/api/sessions/${d.id}/attachments/${fid}`, { method: "PATCH", body });
      await refetch();
    } catch (e) {
      setErr(errorText(e));
    }
  };
  const detach = async (fid: string) => {
    try {
      await api(`/api/sessions/${d.id}/attachments/${fid}`, { method: "DELETE" });
      await refetch();
    } catch (e) {
      setErr(errorText(e));
    }
  };
  return (
    <div className="panel">
      <div className="pipeline small">
        {["資料受付", "抽出", "議長による内容整理", "Context Packet作成", "Agent配布"].map((s, i) => <span key={s} className="pipe-step">{i > 0 ? "→ " : ""}{s}</span>)}
      </div>
      {lastRun && <div className="muted small">最新の議長解析: {lastRun.purpose} / {lastRun.status}{lastRun.error ? `（${lastRun.error}）` : ""}</div>}
      {err && <div className="err">{err}</div>}
      {d.attachments.map((f) => (
        <div key={f.file_id} className={`file-card ${f.enabled ? "" : "disabled"}`}>
          <div><strong>{f.name}</strong> <span className={`chip st-${f.status}`}>{FILE_STATUS_LABEL[f.status] || f.status}</span></div>
          <div className="muted small">{f.kind}・{fmtSize(f.size)}{f.pages ? `・${f.pages}ページ` : ""}{f.sheets ? `・${f.sheets}シート` : ""}{f.slides ? `・${f.slides}スライド` : ""}{f.chunks != null ? `・${f.chunks}チャンク` : ""}</div>
          {f.error && <div className="err small">{f.error}</div>}
          {f.unreadable?.length > 0 && <div className="warn-note small">未解析: {f.unreadable.map((u) => u.reason).join(" / ")}</div>}
          <div className="small">配布先：
            <label><input type="checkbox" checked={f.allowed_agent_ids == null} onChange={(e) => patch(f.file_id, { allowed_agent_ids: e.target.checked ? null : [] })} /> 全一般Agent</label>
            {f.allowed_agent_ids != null && participants.map((a) => (
              <label key={a.id}><input type="checkbox" checked={f.allowed_agent_ids!.includes(a.id)}
                onChange={(e) => patch(f.file_id, { allowed_agent_ids: e.target.checked ? [...f.allowed_agent_ids!, a.id] : f.allowed_agent_ids!.filter((x) => x !== a.id) })} /> {a.name}</label>
            ))}
          </div>
          <div className="small">
            <label><input type="checkbox" checked={!!f.enabled} onChange={(e) => patch(f.file_id, { enabled: e.target.checked })} /> 参照ON</label>
            <label><input type="checkbox" checked={!!f.required} onChange={(e) => patch(f.file_id, { required: e.target.checked })} /> 必須資料</label>
            <label><input type="checkbox" checked={!!f.allow_partial} onChange={(e) => patch(f.file_id, { allow_partial: e.target.checked })} /> 部分抽出を許可</label>
          </div>
          <div className="small">
            <a href={`/api/files/${f.file_id}/preview`} target="_blank" rel="noreferrer">抽出プレビュー</a> ・ <a href={`/api/files/${f.file_id}/download`}>原本</a> ・ <button className="link" onClick={() => api(`/api/sessions/${d.id}/files/${f.file_id}/reanalyze`, { method: "POST" }).then(refetch)}>再解析</button> ・ <button className="link" onClick={() => detach(f.file_id)}>紐付け解除</button>
          </div>
        </div>
      ))}
      {!d.attachments.length && <div className="muted small">添付はありません。下の📎または「ファイル」画面から追加できます。</div>}
      <div className="muted small">変更は次の安全な発言境界から反映されます。配布済みの過去のLLM入力からは内容を回収できません。</div>
    </div>
  );
}
