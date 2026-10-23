import { useEffect, useState } from "react";
import { api, errorText } from "../api/client";

export default function SettingsPage() {
  const [s, setS] = useState<any>(null);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const [priceModel, setPriceModel] = useState("");
  useEffect(() => { api("/api/settings").then(setS).catch((e) => setErr(errorText(e))); }, []);
  if (!s) return <div className="page">{err ? <div className="err">{err}</div> : "読み込み中…"}</div>;
  const set = (sec: string, k: string, v: any) => setS({ ...s, [sec]: { ...s[sec], [k]: v } });
  const num = (sec: string, k: string, label: string, opt = false) => (
    <label className="field"><span>{label}</span>
      <input type="number" value={s[sec][k] ?? ""} placeholder={opt ? "上限なし" : ""} onChange={(e) => set(sec, k, e.target.value === "" ? (opt ? null : 0) : Number(e.target.value))} />
    </label>
  );
  const save = async () => {
    setErr(""); setMsg("");
    try { setS(await api("/api/settings", { method: "PUT", body: s })); setMsg("保存しました（次のセッションから有効）"); } catch (e) { setErr(errorText(e)); }
  };
  return (
    <div className="page">
      <h2>討論・使用量の設定</h2>
      <h3>進行</h3>
      <div className="form-grid">
        {num("debate", "debate_rounds", "既定の討論ラウンド")}
        {num("debate", "max_parallel_requests", "独立提案の並列数")}
        {num("debate", "max_output_tokens", "1発言の本文の上限tokens（思考分は接続ごとに加算）")}
        {num("debate", "truncation_retry_cap", "途切れた時の再生成で広げる上限tokens")}
        {num("debate", "request_timeout_s", "応答タイムアウト(秒)")}
        {num("debate", "max_retries", "再試行回数(429/5xx)")}
        {num("debate", "max_agents", "Agent数の上限")}
      </div>
      <h3>履歴圧縮（会話履歴のみ・完全ログは保持）</h3>
      <div className="form-grid">
        <label className="field"><span>モード</span>
          <select value={s.compression.compression_mode} onChange={(e) => set("compression", "compression_mode", e.target.value)}>
            <option value="auto">Auto</option><option value="off">Off</option><option value="manual">Manual</option>
          </select>
        </label>
        {num("compression", "history_compress_threshold", "圧縮の閾値(tokens)")}
        {num("compression", "keep_recent_messages", "原文保持する直近の発言数")}
        {num("compression", "summary_target_tokens", "要約の目標tokens")}
      </div>
      <h3>議長の資料処理</h3>
      <div className="form-grid">
        {num("chair", "chair_retrieval_budget", "1回の検索で取る引用(tokens)")}
        {num("chair", "agent_packet_target_tokens", "Packetの目標上限(tokens)")}
        {num("chair", "direct_input_threshold_tokens", "全文投入する資料の上限(tokens)")}
        {num("chair", "max_search_queries", "根拠要求1件あたりの検索回数")}
        {num("chair", "max_evidence_reasks", "ユーザーへの再質問の最大回数")}
      </div>
      <h3>セッション上限・フォールバック</h3>
      <div className="form-grid">
        {num("limits", "max_session_tokens", "セッションのトークン上限", true)}
        <label className="field"><span>セッションの概算費用上限(USD)</span><input type="number" step="0.01" value={s.limits.max_estimated_cost ?? ""} placeholder="上限なし" onChange={(e) => set("limits", "max_estimated_cost", e.target.value === "" ? null : Number(e.target.value))} /></label>
        <label className="field"><span>OpenAI/Claudeの追加割合</span><input type="number" step="0.0001" value={s.routing.openai_claude_extra_fraction} onChange={(e) => set("routing", "openai_claude_extra_fraction", Number(e.target.value))} /></label>
        <label className="field"><span>その他クラウドの追加割合</span><input type="number" step="0.0001" value={s.routing.other_cloud_extra_fraction} onChange={(e) => set("routing", "other_cloud_extra_fraction", Number(e.target.value))} /></label>
      </div>
      <label className="small"><input type="checkbox" checked={s.routing.require_quality_validation_for_downstream} onChange={(e) => set("routing", "require_quality_validation_for_downstream", e.target.checked)} /> 下流クラウド／ローカルは品質検証に合格した接続のみ使う</label>
      <h3>単価表（概算費用の計算用・USD / 100万tokens）</h3>
      <table className="file-table">
        <thead><tr><th>モデルID</th><th>入力</th><th>出力</th><th>キャッシュ入力</th><th></th></tr></thead>
        <tbody>
          {Object.entries(s.pricing).map(([m, p]: any) => (
            <tr key={m}>
              <td>{m}</td>
              {(["input_price_per_1m", "output_price_per_1m", "cached_price_per_1m"] as const).map((k) => (
                <td key={k}><input type="number" step="0.01" value={p[k] ?? ""} onChange={(e) => setS({ ...s, pricing: { ...s.pricing, [m]: { ...p, [k]: e.target.value === "" ? (k === "cached_price_per_1m" ? null : 0) : Number(e.target.value) } } })} /></td>
              ))}
              <td><button className="link" onClick={() => { const n = { ...s.pricing }; delete n[m]; setS({ ...s, pricing: n }); }}>削除</button></td>
            </tr>
          ))}
          <tr><td><input value={priceModel} placeholder="モデルIDを追加" onChange={(e) => setPriceModel(e.target.value)} /></td>
            <td colSpan={4}><button className="btn btn-small" disabled={!priceModel} onClick={() => { setS({ ...s, pricing: { ...s.pricing, [priceModel]: { input_price_per_1m: 0, output_price_per_1m: 0, cached_price_per_1m: null } } }); setPriceModel(""); }}>追加</button></td></tr>
        </tbody>
      </table>
      <div className="muted small">単価は固定で埋め込まれていません。未登録のモデルの費用は「未算出」と表示されます。</div>
      {err && <div className="err">{err}</div>}
      {msg && <div className="ok">{msg}</div>}
      <button className="btn btn-primary" onClick={save}>保存</button>
    </div>
  );
}
