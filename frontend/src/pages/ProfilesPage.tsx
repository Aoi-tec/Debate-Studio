import { useEffect, useState } from "react";
import { api, errorText } from "../api/client";
import { PROTOCOL_LABEL, fmtTokens } from "../labels";
import type { Profile } from "../types";

const EFFORT: Record<string, string> = {
  provider_default: "モデル既定（送らない）",
  off: "オフ（思考しない）",
  low: "低",
  medium: "中",
  high: "高",
};
const DEFAULT_BUDGET: Record<string, number> = { provider_default: 4096, off: 0, low: 2048, medium: 4096, high: 8192 };

const FAMILY = { openai_claude: "OpenAI / Claude API（U0+U0/6）", other_cloud: "その他クラウド（U0+U0/4）", local: "ローカル（口座閾値なし）" };

export default function ProfilesPage() {
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [protocols, setProtocols] = useState<string[]>([]);
  const [err, setErr] = useState("");
  const load = () => api("/api/profiles").then((r) => { setProfiles(r.profiles); setProtocols(r.protocols); });
  useEffect(() => {
    load().catch((e) => setErr(errorText(e)));
  }, []);
  return (
    <div className="page">
      <h2>API接続管理</h2>
      <p className="muted small">APIキーはサーバーの <code>.env</code> にのみ保存され、画面には再表示されません。管理名や方式を変更しても固定ID・ENV変数名は変わりません。</p>
      {err && <div className="err">{err}</div>}
      {profiles.map((p) => <ProfileCard key={p.id} p={p} protocols={protocols} reload={load} />)}
      <NewProfile protocols={protocols} reload={load} />
    </div>
  );
}

function ProfileCard({ p, protocols, reload }: { p: Profile; protocols: string[]; reload: () => Promise<void> }) {
  const [f, setF] = useState(p);
  const [key, setKey] = useState("");
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const [quota, setQuota] = useState(p.quota);
  const [adminKey, setAdminKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [models, setModels] = useState<string[]>([]);
  useEffect(() => { setF(p); setQuota(p.quota); }, [p]);
  const budget = f.reasoning.effort === "off" ? 0 : (f.reasoning.budget_tokens ?? DEFAULT_BUDGET[f.reasoning.effort]);
  const run = async (fn: () => Promise<any>, ok?: (r: any) => string) => {
    setErr(""); setMsg(""); setBusy(true);
    try {
      const r = await fn();
      if (ok) setMsg(ok(r));
      await reload();
    } catch (e) {
      setErr(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="profile-card" data-testid={`profile-${p.id}`}>
      <div className="profile-head">
        <input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} />
        <code className="muted small">{p.id}{p.env_var ? ` / ${p.env_var}` : " / キー不要"}</code>
        <span className={`chip ${p.has_key ? "chip-ok" : p.env_var ? "chip-danger" : ""}`}>{p.env_var ? (p.has_key ? "キー登録済み" : "キー未登録") : "ローカル"}</span>
        <span className={`chip ${p.quality.status === "passed" ? "chip-ok" : "chip-ghost"}`}>品質: {p.quality.status}{p.quality.validated_model ? `（${p.quality.validated_model}）` : ""}</span>
      </div>
      <div className="form-grid">
        <label className="field"><span>API方式</span>
          <select value={f.protocol} onChange={(e) => setF({ ...f, protocol: e.target.value })}>
            {protocols.map((x) => <option key={x} value={x}>{PROTOCOL_LABEL[x] || x}</option>)}
          </select>
        </label>
        <label className="field"><span>Base URL</span><input value={f.base_url || ""} placeholder="（方式の既定）" onChange={(e) => setF({ ...f, base_url: e.target.value || null })} /></label>
        <label className="field"><span>既定モデル</span>
          <input list={`models-${p.id}`} value={f.default_model} placeholder="例: 利用するモデルID" onChange={(e) => setF({ ...f, default_model: e.target.value })} />
          <datalist id={`models-${p.id}`}>{models.map((m) => <option key={m} value={m} />)}</datalist>
          <button className="link small" type="button" onClick={() => run(async () => { const r = await api(`/api/profiles/${p.id}/models`); setModels(r.models || []); return r; }, (r) => r.models?.length ? `モデル ${r.models.length} 件を取得しました（入力欄で選択できます）` : `取得できませんでした: ${r.message || "0件"}`)}>サーバーからモデル一覧を取得</button>
        </label>
        <label className="field"><span>課金区分</span>
          <select value={f.billing_family} onChange={(e) => setF({ ...f, billing_family: e.target.value as any })}>
            {Object.entries(FAMILY).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </label>
      </div>
      <label className="small"><input type="checkbox" checked={f.enabled} onChange={(e) => setF({ ...f, enabled: e.target.checked })} /> 有効</label>
      <label className="small"><input type="checkbox" checked={f.allow_document_transfer} onChange={(e) => setF({ ...f, allow_document_transfer: e.target.checked })} /> 添付資料由来のテキスト（議長の解析入力・Context Packet）をこの接続先へ送信することを許可</label>
      <div className="card-sec">
        <div className="card-sec-title">Thinking（思考）と出力上限</div>
        <div className="form-grid">
          <label className="field"><span>思考の強さ</span>
            <select value={f.reasoning.effort} onChange={(e) => setF({ ...f, reasoning: { ...f.reasoning, effort: e.target.value as any } })}>
              {Object.entries(EFFORT).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select>
          </label>
          <label className="field"><span>思考の予算（tokens / 1発言）</span>
            <input type="number" min={0} disabled={f.reasoning.effort === "off"} placeholder={`既定 ${DEFAULT_BUDGET[f.reasoning.effort]}`} value={f.reasoning.budget_tokens ?? ""}
              onChange={(e) => setF({ ...f, reasoning: { ...f.reasoning, budget_tokens: e.target.value === "" ? null : Number(e.target.value) } })} />
          </label>
          <label className="field"><span>コンテキスト長（任意）</span>
            <input type="number" min={1024} placeholder="不明なら空欄" value={f.reasoning.context_window_tokens ?? ""}
              onChange={(e) => setF({ ...f, reasoning: { ...f.reasoning, context_window_tokens: e.target.value === "" ? null : Number(e.target.value) } })} />
          </label>
          <label className="field"><span>同時実行数（この接続）</span>
            <input type="number" min={1} placeholder="制限なし" value={f.max_concurrent_requests ?? ""}
              onChange={(e) => setF({ ...f, max_concurrent_requests: e.target.value === "" ? null : Number(e.target.value) })} />
          </label>
        </div>
        <label className="small"><input type="checkbox" checked={f.reasoning.strip_think_tags} onChange={(e) => setF({ ...f, reasoning: { ...f.reasoning, strip_think_tags: e.target.checked } })} /> 本文中の &lt;think&gt;…&lt;/think&gt; を除去（他のAgentに渡さない・表示しない）</label>
        <div className="muted small">1発言の上限 ＝ 本文の上限（設定画面・既定1500）＋ 思考の予算 {budget.toLocaleString("ja-JP")}。上限で途切れたら1回だけ2倍にして再生成します。ローカルサーバーが同時リクエストでコンテキストを分け合う場合は、同時実行数を 1 にすると各Agentが毎回コンテキストを丸ごと使えます。</div>
      </div>
      <div className="row-actions">
        <button className="btn btn-primary" disabled={busy} onClick={() => run(() => api(`/api/profiles/${p.id}`, { method: "PATCH", body: { name: f.name, protocol: f.protocol, base_url: f.base_url, default_model: f.default_model, enabled: f.enabled, billing_family: f.billing_family, allow_document_transfer: f.allow_document_transfer, reasoning: f.reasoning, max_concurrent_requests: f.max_concurrent_requests } }), () => "保存しました")}>保存</button>
        <button className="btn" disabled={busy} onClick={() => run(() => api(`/api/profiles/${p.id}/test`, { method: "POST" }), (r) => (r.ok ? `接続OK（ストリーミング ${r.streaming ? "可" : "不明"}・${r.latency_ms}ms）` : `接続NG [${r.stage}] ${r.message}`))}>接続テスト</button>
        <button className="btn" disabled={busy} onClick={() => run(() => api(`/api/profiles/${p.id}/validate-quality`, { method: "POST" }), (r) => `品質検証: ${r.status}（合格率 ${Math.round(r.pass_rate * 100)}%）${r.note}`)}>品質検証（下流用）</button>
      </div>
      <div className="row-actions">
        <input type="password" autoComplete="new-password" placeholder={p.has_key ? "新しいAPIキーで差し替え" : "APIキーを登録"} value={key} onChange={(e) => setKey(e.target.value)} />
        <button className="btn" disabled={busy || !key} onClick={() => run(async () => { await api(`/api/profiles/${p.id}/secret`, { method: "PUT", body: { api_key: key } }); setKey(""); }, () => "キーを保存しました（再表示されません）")}>キーを保存</button>
      </div>
      {f.billing_family !== "local" && (
        <details>
          <summary className="small">使用量照会と口座閾値（{FAMILY[f.billing_family]}）</summary>
          <div className="form-grid">
            <label className="field"><span>モード</span>
              <select value={quota.mode} onChange={(e) => setQuota({ ...quota, mode: e.target.value as any })}>
                <option value="auto">auto（管理者Usage APIでU0取得）</option>
                <option value="manual">manual（手動予算のみ）</option>
                <option value="disabled">disabled（口座閾値を使わない）</option>
              </select>
            </label>
            <label className="field"><span>予算の単位</span>
              <select value={quota.scope} onChange={(e) => setQuota({ ...quota, scope: e.target.value as any })}>
                <option value="per_agent">Agentごとに分離（各Agentが同じ枠を個別に持つ）</option>
                <option value="shared">接続全体で共有（全Agentで1つの枠）</option>
              </select>
            </label>
            <label className="field"><span>手動予算（取得不可時・{quota.scope === "per_agent" ? "1Agentあたり" : "全Agent合計"}）</span><input type="number" min={0} value={quota.manual_budget_tokens ?? ""} onChange={(e) => setQuota({ ...quota, manual_budget_tokens: e.target.value === "" ? null : Number(e.target.value) })} /></label>
            <label className="field"><span>初期予算（U0=0の時）</span><input type="number" min={0} value={quota.initial_bootstrap_budget ?? ""} onChange={(e) => setQuota({ ...quota, initial_bootstrap_budget: e.target.value === "" ? null : Number(e.target.value) })} /></label>
            <label className="field"><span>共有プールID</span><input value={quota.quota_pool_id || ""} placeholder={p.id} onChange={(e) => setQuota({ ...quota, quota_pool_id: e.target.value || null })} /></label>
            <label className="field"><span>管理者キー（別管理）</span><input type="password" autoComplete="new-password" placeholder={p.quota.has_admin_key ? "登録済み（差し替え）" : "Admin API Key"} value={adminKey} onChange={(e) => setAdminKey(e.target.value)} /></label>
          </div>
          <div className="row-actions">
            <button className="btn" disabled={busy} onClick={() => run(async () => { await api(`/api/profiles/${p.id}/quota`, { method: "PUT", body: { quota: { mode: quota.mode, scope: quota.scope, admin_env_var: quota.admin_env_var, period_label: quota.period_label, manual_budget_tokens: quota.manual_budget_tokens, initial_bootstrap_budget: quota.initial_bootstrap_budget, quota_pool_id: quota.quota_pool_id }, admin_api_key: adminKey || undefined } }); setAdminKey(""); }, () => "保存しました")}>閾値設定を保存</button>
            <button className="btn" disabled={busy} onClick={() => run(() => api(`/api/profiles/${p.id}/quota/refresh`, { method: "POST" }), (r) => r.status === "readable" ? `当期使用量 ${fmtTokens(r.tokens)} tok（今開始すると閾値 ${fmtTokens(r.threshold_if_started_now)}）` : `取得不可: ${r.detail}`)}>使用量を照会</button>
          </div>
          <div className="muted small">閾値はアプリ独自の自動上限で、提供元の公式な残り枠ではありません。ChatGPT Plus / Claude Pro などチャット契約の利用枠とも別物です。U0はセッション開始時に固定されます。</div>
        </details>
      )}
      {msg && <div className="ok">{msg}</div>}
      {err && <div className="err">{err}</div>}
    </div>
  );
}

const PRESETS: Record<string, any> = {
  strata: { name: "Strata（ローカル）", protocol: "openai_chat_compatible", base_url: "http://127.0.0.1:8080/v1", requires_key: false, billing_family: "local", allow_document_transfer: true, reasoning: { effort: "medium", budget_tokens: null, context_window_tokens: null, strip_think_tags: true }, max_concurrent_requests: 1 },
  ollama: { name: "Ollama（ローカル）", protocol: "openai_chat_compatible", base_url: "http://127.0.0.1:11434/v1", requires_key: false, billing_family: "local", allow_document_transfer: true, reasoning: { effort: "provider_default", budget_tokens: null, context_window_tokens: null, strip_think_tags: true }, max_concurrent_requests: 1 },
  lmstudio: { name: "LM Studio（ローカル）", protocol: "openai_chat_compatible", base_url: "http://127.0.0.1:1234/v1", requires_key: false, billing_family: "local", allow_document_transfer: true, reasoning: { effort: "provider_default", budget_tokens: null, context_window_tokens: null, strip_think_tags: true }, max_concurrent_requests: 1 },
  openai: { name: "OpenAI", protocol: "openai_chat_compatible", base_url: "https://api.openai.com/v1", requires_key: true, billing_family: "openai_claude", allow_document_transfer: false, reasoning: { effort: "provider_default", budget_tokens: null, context_window_tokens: null, strip_think_tags: true }, max_concurrent_requests: null },
  anthropic: { name: "Anthropic", protocol: "anthropic_messages", base_url: "", requires_key: true, billing_family: "openai_claude", allow_document_transfer: false, reasoning: { effort: "provider_default", budget_tokens: null, context_window_tokens: null, strip_think_tags: true }, max_concurrent_requests: null },
  gemini: { name: "Gemini", protocol: "gemini_generate_content", base_url: "", requires_key: true, billing_family: "other_cloud", allow_document_transfer: false, reasoning: { effort: "provider_default", budget_tokens: null, context_window_tokens: null, strip_think_tags: true }, max_concurrent_requests: null },
};

function NewProfile({ protocols, reload }: { protocols: string[]; reload: () => Promise<void> }) {
  const [f, setF] = useState<any>({ name: "", protocol: "openai_chat_compatible", base_url: "", default_model: "", api_key: "", requires_key: true, billing_family: "openai_claude", allow_document_transfer: false, reasoning: null, max_concurrent_requests: null });
  const [err, setErr] = useState("");
  const create = async () => {
    setErr("");
    try {
      await api("/api/profiles", { body: { ...f, base_url: f.base_url || null, api_key: f.api_key || null } });
      setF({ ...f, name: "", api_key: "", default_model: "" });
      await reload();
    } catch (e) {
      setErr(errorText(e));
    }
  };
  return (
    <div className="profile-card new">
      <h3>新規登録</h3>
      <div className="row-actions">
        <span className="small">プリセット:</span>
        {Object.entries(PRESETS).map(([k, v]) => <button key={k} className="btn btn-small" onClick={() => setF({ ...f, ...v, default_model: "", api_key: "" })}>{v.name}</button>)}
      </div>
      <div className="form-grid">
        <label className="field"><span>管理名</span><input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></label>
        <label className="field"><span>API方式</span><select value={f.protocol} onChange={(e) => setF({ ...f, protocol: e.target.value })}>{protocols.map((x) => <option key={x} value={x}>{PROTOCOL_LABEL[x] || x}</option>)}</select></label>
        <label className="field"><span>Base URL（必要な方式のみ）</span><input value={f.base_url} onChange={(e) => setF({ ...f, base_url: e.target.value })} placeholder="https://api.openai.com/v1" /></label>
        <label className="field"><span>既定モデル</span><input value={f.default_model} onChange={(e) => setF({ ...f, default_model: e.target.value })} /></label>
        <label className="field"><span>課金区分</span><select value={f.billing_family} onChange={(e) => setF({ ...f, billing_family: e.target.value })}>{Object.entries(FAMILY).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>
        <label className="field"><span>APIキー</span><input type="password" autoComplete="new-password" disabled={!f.requires_key} value={f.api_key} onChange={(e) => setF({ ...f, api_key: e.target.value })} /></label>
      </div>
      <label className="small"><input type="checkbox" checked={!f.requires_key} onChange={(e) => setF({ ...f, requires_key: !e.target.checked })} /> キー不要（ローカル互換サーバー）</label>
      <label className="small"><input type="checkbox" checked={f.allow_document_transfer} onChange={(e) => setF({ ...f, allow_document_transfer: e.target.checked })} /> 添付資料由来テキストの送信を許可</label>
      {err && <div className="err">{err}</div>}
      <button className="btn btn-primary" disabled={!f.name.trim()} onClick={create}>登録</button>
    </div>
  );
}
