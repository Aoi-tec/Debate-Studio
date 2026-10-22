import { useRef, useState } from "react";
import { api, errorText, newKey, uploadFiles } from "../api/client";
import { CONCLUSION_LABEL, EVIDENCE_STATE_LABEL, REASON_LABEL } from "../labels";
import type { EvidenceRequest, SessionDetail } from "../types";

function List({ title, items }: { title: string; items?: any[] }) {
  if (!items || !items.length) return null;
  return (
    <div className="card-sec">
      <div className="card-sec-title">{title}</div>
      <ul>{items.map((x, i) => <li key={i}>{typeof x === "string" ? x : x.point ? `${x.agent ?? ""}: ${x.point}` : x.claim || x.text || x.question || JSON.stringify(x)}</li>)}</ul>
    </div>
  );
}

export function CheckpointCard({ report, awaiting, sid, blocked, onDone }: { report: any; awaiting: boolean; sid: string; blocked: boolean; onDone: () => void }) {
  const [mode, setMode] = useState<"" | "redirect">("");
  const [instr, setInstr] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const key = useRef(newKey());
  const decide = async (decision: string) => {
    setBusy(true);
    setErr("");
    try {
      await api(`/api/sessions/${sid}/decision`, { body: { decision, instruction: decision === "redirect" ? instr : undefined, idempotency_key: `${key.current}-${decision}` } });
      onDone();
    } catch (e) {
      setErr(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="card card-checkpoint" data-testid="checkpoint-card">
      <div className="card-title">🧭 議長の中間報告（ラウンド {report.round}）</div>
      <p className="card-lead">{report.progress_summary}</p>
      <List title="合意・解決済み" items={report.resolved_issues} />
      <List title="未合意・未解決" items={report.unresolved_issues} />
      <List title="停滞の理由" items={report.stagnation_reasons} />
      <List title="不足する根拠" items={report.missing_evidence} />
      <List title="次ラウンドの候補" items={report.next_round_questions} />
      <List title="少数意見" items={report.agent_dissent} />
      {awaiting ? (
        <div className="card-actions">
          {blocked && <div className="warn-note">資料不足の確認が未解決のため「続ける／方向修正」はできません（終了は可能・未解決は最終レポートに残ります）</div>}
          <button className="btn btn-primary" disabled={busy || blocked} onClick={() => decide("continue")}>続ける（+1ラウンド）</button>
          <button className="btn" disabled={busy || blocked} onClick={() => setMode(mode ? "" : "redirect")}>方向を修正</button>
          <button className="btn btn-danger-ghost" disabled={busy} onClick={() => decide("finish")}>ここで終了・まとめる</button>
          {mode === "redirect" && (
            <div className="redirect-box">
              <textarea placeholder="次ラウンドへの指示（全Agentに共有されます）" value={instr} onChange={(e) => setInstr(e.target.value)} />
              <button className="btn btn-primary" disabled={busy || !instr.trim()} onClick={() => decide("redirect")}>指示して続ける</button>
            </div>
          )}
          {err && <div className="err">{err}</div>}
          <div className="muted small">選択するまで全Agentの次のAPI呼び出しは停止しています（課金なし）。</div>
        </div>
      ) : null}
    </div>
  );
}

export function EvidenceCard({ er, sid, onDone }: { er: EvidenceRequest; sid: string; onDone: () => void }) {
  const [text, setText] = useState("");
  const [hypo, setHypo] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);
  const keyRef = useRef(newKey());
  const open = er.state === "WAITING_USER_EVIDENCE" || er.state === "STILL_MISSING";
  const can = (a: string) => er.acceptable_inputs.includes(a);
  const send = async (action: string, extra: any = {}) => {
    setBusy(true);
    setErr("");
    try {
      await api(`/api/sessions/${sid}/evidence-requests/${er.evidence_request_id}/respond`, {
        body: { evidence_request_id: er.evidence_request_id, expected_version: er.version, action, idempotency_key: `${keyRef.current}-${er.version}-${action}`, ...extra },
      });
      setText("");
      onDone();
    } catch (e) {
      setErr(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  const upload = async (files: FileList | null) => {
    if (!files || !files.length) return;
    setBusy(true);
    setErr("");
    try {
      const r = await uploadFiles(Array.from(files));
      if (r.errors?.length) setErr(r.errors.map((x: any) => `${x.name}: ${x.message}`).join(" / "));
      if (r.files.length) await send("file_upload", { file_ids: r.files.map((f: any) => f.file_id) });
    } catch (e) {
      setErr(errorText(e));
    } finally {
      setBusy(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  };
  return (
    <div className={`card card-evidence ${open ? "" : "card-closed"}`} data-testid="evidence-card">
      <div className="card-title">
        📎 資料不足の確認 <span className={`chip ${er.criticality === "critical" ? "chip-danger" : ""}`}>{er.criticality === "critical" ? "重要（critical）" : "補足（noncritical）"}</span>
        <span className="chip chip-ghost">{EVIDENCE_STATE_LABEL[er.state] || er.state}</span>
        {er.ask_count > 1 && <span className="chip chip-warn">再確認 {er.ask_count - 1}回目</span>}
      </div>
      <p className="card-lead">{er.question}</p>
      <dl className="kv">
        <dt>要求したAgent</dt><dd>{er.agent_name}：「{er.agent_question}」</dd>
        <dt>なぜ必要か</dt><dd>{er.why_needed}</dd>
        <dt>不足の理由</dt><dd>{REASON_LABEL[er.reason_code] || er.reason_code}{er.detail ? `（${er.detail}）` : ""}</dd>
        <dt>不足項目</dt><dd>{er.missing_fields.join("、") || "—"}</dd>
        <dt>議長が調べた範囲</dt><dd>{er.searched_sources.length ? er.searched_sources.map((s) => `${s.label || s.file_id}（${s.scope}）`).join(" / ") : "検索可能な許可済み資料はありませんでした"}</dd>
        {er.needed_materials && <><dt>推奨される追加資料</dt><dd>{er.needed_materials}</dd></>}
        <dt>未解決時の影響</dt><dd>{er.impact_if_unresolved}</dd>
      </dl>
      {open ? (
        <div className="card-actions">
          {can("file_upload") && (
            <>
              <input ref={fileRef} type="file" multiple hidden onChange={(e) => upload(e.target.files)} />
              <button className="btn btn-primary" disabled={busy} onClick={() => fileRef.current?.click()}>ファイルを追加</button>
            </>
          )}
          {can("user_statement") && (
            <div className="answer-box">
              <textarea placeholder="文章で回答（ユーザー申告として記録され、資料による検証済みとは区別されます）" value={text} onChange={(e) => setText(e.target.value)} />
              <button className="btn" disabled={busy || !text.trim()} onClick={() => send("user_statement", { statement: text })}>文章で回答</button>
            </div>
          )}
          {can("unavailable") && <button className="btn" disabled={busy} onClick={() => send("unavailable")}>今は用意できない</button>}
          {can("withdraw") && <button className="btn btn-ghost" disabled={busy} onClick={() => send("withdraw")}>質問を取り下げる</button>}
          {can("continue_as_hypothesis") && (
            <div className="hypo-box">
              <label><input type="checkbox" checked={hypo} onChange={(e) => setHypo(e.target.checked)} /> 未確認のまま「仮説」として議論することを承認する（事実には格上げされません）</label>
              <button className="btn" disabled={busy || !hypo} onClick={() => send("continue_as_hypothesis", { statement: text || undefined })}>仮説として議論する</button>
            </div>
          )}
          {can("finish_with_unresolved") && <button className="btn btn-danger-ghost" disabled={busy} onClick={() => send("finish_with_unresolved")}>未解決のまま終了（暫定レポート）</button>}
          {err && <div className="err">{err}</div>}
          <div className="muted small">回答・資料の解析が終わるまで討論は再開しません（待機中は課金なし）。</div>
        </div>
      ) : (
        er.responses && er.responses.length > 0 && <div className="muted small">回答: {er.responses.map((r) => `${r.action}${r.statement ? `「${r.statement}」` : ""}`).join(" / ")}</div>
      )}
    </div>
  );
}

export function FinalReportCard({ report, status }: { report: any; status: string | null }) {
  return (
    <div className={`card card-final status-${status}`} data-testid="final-card">
      <div className="card-title">🏁 最終レポート <span className={`chip ${status === "blocked" ? "chip-danger" : status === "provisional" ? "chip-warn" : "chip-ok"}`}>{CONCLUSION_LABEL[status || ""] || status}</span></div>
      {report.relies_on_user_assertions && <div className="warn-note">この結論は資料で検証されていないユーザー申告を含みます（下記「ユーザー申告」参照）。</div>}
      <p className="card-lead">{report.adopted_proposal}</p>
      {report.rationale && <p>{report.rationale}</p>}
      <List title="資料で検証された事項" items={(report.verified_findings || []).map((v: any) => `${v.claim}（${(v.evidence || []).join(", ")}）`)} />
      <List title="ユーザー申告（未検証）" items={report.user_assertions} />
      <List title="仮説（事実ではない）" items={report.hypotheses} />
      <List title="未解決の根拠要求" items={(report.unresolved_evidence || []).map((u: any) => `${u.question}（${u.criticality} / ${u.state}）`)} />
      <List title="少数意見" items={report.minority_opinions} />
      <List title="不確実性" items={report.uncertainties} />
      <List title="次の行動" items={report.next_actions} />
    </div>
  );
}

export function BlockerBar({ d, onAction }: { d: SessionDetail; onAction: (a: string, arg?: string) => void }) {
  const items = d.blockers.filter((b) => b.type !== "checkpoint" && b.type !== "evidence");
  if (!items.length) return null;
  return (
    <div className="blockers">
      {items.map((b, i) => (
        <div key={i} className={`blocker blocker-${b.type}`}>
          <strong>{b.type === "budget" ? "予算停止" : b.type === "quality" ? "品質未達" : b.type === "turn_failed" ? "発言失敗" : b.type === "restart" ? "再起動で中断" : "保留"}</strong>
          ：{b.message}
          {b.reasons && <ul>{b.reasons.map((r, j) => <li key={j}>{r}</li>)}</ul>}
          {b.type === "turn_failed" && b.agent_id && <button className="btn btn-small" onClick={() => onAction("skip", b.agent_id)}>このAgentをスキップ</button>}
        </div>
      ))}
    </div>
  );
}
