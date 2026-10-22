import { useEffect, useRef, useState } from "react";
import { api, errorText } from "../api/client";
import type { Role } from "../types";

export default function RolesPage() {
  const [roles, setRoles] = useState<Role[]>([]);
  const [sel, setSel] = useState<string | null>(null);
  const [cur, setCur] = useState<Role | null>(null);
  const [preview, setPreview] = useState(false);
  const [diff, setDiff] = useState<string | null>(null);
  const [pending, setPending] = useState("");
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);
  const load = async () => {
    const r = await api("/api/roles");
    setRoles(r.roles);
    if (!sel && r.roles.length) setSel(r.roles[0].id);
  };
  useEffect(() => { load().catch((e) => setErr(errorText(e))); }, []);
  useEffect(() => {
    if (sel) api(`/api/roles/${sel}`).then(setCur).catch((e) => setErr(errorText(e)));
  }, [sel]);
  const run = async (fn: () => Promise<any>, ok: string) => {
    setErr(""); setMsg("");
    try { await fn(); setMsg(ok); await load(); } catch (e) { setErr(errorText(e)); }
  };
  const create = (copy?: Role) => run(async () => {
    const name = prompt("役割の表示名", copy ? `${copy.name}（複製）` : "新しい役割");
    if (!name) return;
    const id = prompt("役割ID（英小文字・数字・_- / 作成後は変更不可）", copy ? `${copy.id}_copy` : "") || undefined;
    const r = await api("/api/roles", { body: { id, name, description: copy?.description || "", agent_md: copy ? "" : `# ${name}\n`, copy_from: copy?.id } });
    setSel(r.id);
  }, "作成しました");
  const importFile = async (files: FileList | null) => {
    if (!files?.length || !cur) return;
    const text = await files[0].text();
    try {
      const r = await api(`/api/roles/${cur.id}/import-agent-md`, { body: { content: text } });
      setDiff(r.diff || "（差分なし）");
      setPending(text);
    } catch (e) { setErr(errorText(e)); }
    if (fileRef.current) fileRef.current.value = "";
  };
  return (
    <div className="page roles">
      <h2>役割ライブラリ</h2>
      <div className="roles-layout">
        <div className="roles-list">
          {roles.map((r) => (
            <button key={r.id} className={`session-item ${r.id === sel ? "active" : ""}`} onClick={() => setSel(r.id)}>
              <div><strong>{r.name}</strong> <code className="muted small">{r.id}</code></div>
              <div className="muted small">{r.description}{r.in_use_by?.length ? ` ・使用中 ${r.in_use_by.length}` : ""}</div>
            </button>
          ))}
          <button className="btn btn-block" onClick={() => create()}>＋ 新規作成</button>
        </div>
        {cur && (
          <div className="roles-edit">
            <div className="form-grid">
              <label className="field"><span>表示名</span><input value={cur.name} onChange={(e) => setCur({ ...cur, name: e.target.value })} /></label>
              <label className="field"><span>説明</span><input value={cur.description} onChange={(e) => setCur({ ...cur, description: e.target.value })} /></label>
            </div>
            <div className="muted small">ID: <code>{cur.id}</code>（不変）・ファイル: roles/{cur.id}/{cur.agent_file}・権限: {cur.capabilities.join(", ")}</div>
            <div className="tabs inline">
              <button className={!preview ? "active" : ""} onClick={() => setPreview(false)}>編集</button>
              <button className={preview ? "active" : ""} onClick={() => setPreview(true)}>プレビュー</button>
            </div>
            {preview ? <pre className="md-preview">{cur.agent_md}</pre> :
              <textarea className="md-editor" value={cur.agent_md || ""} onChange={(e) => setCur({ ...cur, agent_md: e.target.value })} />}
            <div className="row-actions">
              <button className="btn btn-primary" onClick={() => run(() => api(`/api/roles/${cur.id}`, { method: "PUT", body: { name: cur.name, description: cur.description, agent_md: cur.agent_md || "" } }), "保存しました")}>保存</button>
              <button className="btn" onClick={() => create(cur)}>複製</button>
              <input ref={fileRef} type="file" accept=".md,.txt" hidden onChange={(e) => importFile(e.target.files)} />
              <button className="btn" onClick={() => fileRef.current?.click()}>.md / .txt を取り込む</button>
              <button className="btn btn-danger-ghost" disabled={cur.id === "chair" || !!cur.in_use_by?.length} title={cur.in_use_by?.length ? `使用中: ${cur.in_use_by.join(", ")}` : ""}
                onClick={() => { if (confirm("削除しますか？")) run(async () => { await api(`/api/roles/${cur.id}`, { method: "DELETE" }); setSel(null); setCur(null); }, "削除しました"); }}>削除</button>
            </div>
            {diff !== null && (
              <div className="card">
                <div className="card-title">取り込み前の差分確認</div>
                <pre className="diff">{diff}</pre>
                <button className="btn btn-primary" onClick={() => run(async () => { await api(`/api/roles/${cur.id}/import-agent-md`, { body: { content: pending, confirm: true } }); setDiff(null); const r = await api(`/api/roles/${cur.id}`); setCur(r); }, "取り込みました")}>この内容で置換</button>
                <button className="btn" onClick={() => setDiff(null)}>やめる</button>
              </div>
            )}
            <div className="muted small">Agent.MDは討論参加LLMへの役割指示（システム指示）として送られます。討論の添付資料や会話から自動で書き換わることはありません。</div>
            {msg && <div className="ok">{msg}</div>}
            {err && <div className="err">{err}</div>}
          </div>
        )}
      </div>
    </div>
  );
}
