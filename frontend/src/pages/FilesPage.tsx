import { useEffect, useRef, useState } from "react";
import { api, errorText, uploadFiles } from "../api/client";
import { FILE_STATUS_LABEL, fmtSize } from "../labels";
import type { FileInfo } from "../types";

export default function FilesPage() {
  const [files, setFiles] = useState<FileInfo[]>([]);
  const [limits, setLimits] = useState<any>(null);
  const [preview, setPreview] = useState<any>(null);
  const [err, setErr] = useState("");
  const [drag, setDrag] = useState(false);
  const ref = useRef<HTMLInputElement>(null);
  const load = () => api("/api/files").then((r) => { setFiles(r.files); setLimits(r.limits); });
  useEffect(() => {
    load().catch((e) => setErr(errorText(e)));
    const t = window.setInterval(() => load().catch(() => undefined), 3000);
    return () => window.clearInterval(t);
  }, []);
  const add = async (list: FileList | null) => {
    if (!list?.length) return;
    setErr("");
    try {
      const r = await uploadFiles(Array.from(list));
      if (r.errors.length) setErr(r.errors.map((x: any) => `${x.name}: ${x.message}`).join(" / "));
      await load();
    } catch (e) {
      setErr(errorText(e));
    }
    if (ref.current) ref.current.value = "";
  };
  return (
    <div className="page">
      <h2>ファイルライブラリ</h2>
      <div className={`dropzone ${drag ? "drag" : ""}`} onClick={() => ref.current?.click()} onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
        onDragLeave={() => setDrag(false)} onDrop={(e) => { e.preventDefault(); setDrag(false); add(e.dataTransfer.files); }}>
        📎 ドラッグ＆ドロップ／クリックでアップロード
        {limits && <div className="muted small">上限 {limits.max_file_mib} MiB/ファイル・{limits.max_session_mib} MiB/セッション・{limits.max_files_per_session}件/セッション。対応: {limits.accepted.join(", ")}（マクロ有効形式・旧Office形式・ZIP・実行ファイルは不可）</div>}
        <input ref={ref} type="file" multiple hidden onChange={(e) => add(e.target.files)} />
      </div>
      {err && <div className="err">{err}</div>}
      <table className="file-table">
        <thead><tr><th>名前</th><th>種類</th><th>サイズ</th><th>状態</th><th>抽出</th><th></th></tr></thead>
        <tbody>
          {files.map((f) => (
            <tr key={f.file_id}>
              <td>{f.name}</td><td>{f.kind}</td><td>{fmtSize(f.size)}</td>
              <td><span className={`chip st-${f.status}`}>{FILE_STATUS_LABEL[f.status] || f.status}</span>{f.error && <div className="err small">{f.error}</div>}</td>
              <td className="small">{f.pages ? `${f.pages}p ` : ""}{f.sheets ? `${f.sheets}シート ` : ""}{f.slides ? `${f.slides}スライド ` : ""}{f.chunks != null ? `${f.chunks}チャンク` : ""}{f.unreadable?.length ? <div className="warn-note">未解析 {f.unreadable.length}件</div> : null}</td>
              <td className="small">
                <button className="link" onClick={() => api(`/api/files/${f.file_id}/preview`).then(setPreview)}>プレビュー</button> ・ <a href={`/api/files/${f.file_id}/download`}>原本</a> ・ <button className="link" onClick={async () => { if (confirm("原本・抽出結果・索引を削除します。よろしいですか？")) { await api(`/api/files/${f.file_id}`, { method: "DELETE" }); load(); } }}>削除</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {preview && (
        <div className="modal" onClick={() => setPreview(null)}>
          <div className="modal-body" onClick={(e) => e.stopPropagation()}>
            <h3>{preview.file.name} の抽出結果（議長が参照するテキスト）</h3>
            {preview.file.unreadable?.length > 0 && <div className="warn-note">読めなかった箇所: {preview.file.unreadable.map((u: any) => `${JSON.stringify(u.location)} ${u.reason}`).join(" / ")}</div>}
            {preview.chunks.map((c: any) => (
              <div key={c.seq} className="chunk"><div className="muted small">{c.label}（{c.quality}）</div><pre>{c.text}</pre></div>
            ))}
            <button className="btn" onClick={() => setPreview(null)}>閉じる</button>
          </div>
        </div>
      )}
    </div>
  );
}
