export class ApiError extends Error {
  status: number;
  detail: any;
  constructor(status: number, message: string, detail?: any) {
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

let booted: Promise<{ demo_mode: boolean }> | null = null;

export function bootstrap(): Promise<{ demo_mode: boolean }> {
  if (!booted) {
    booted = fetch("/api/auth/bootstrap", { credentials: "same-origin" }).then((r) => {
      if (!r.ok) throw new ApiError(r.status, "認証の初期化に失敗しました");
      return r.json();
    });
  }
  return booted;
}

export async function api<T = any>(path: string, opts: { method?: string; body?: any; form?: FormData; headers?: Record<string, string> } = {}): Promise<T> {
  await bootstrap();
  const headers: Record<string, string> = { "X-Requested-With": "DebateStudio", ...(opts.headers || {}) };
  let body: BodyInit | undefined;
  if (opts.form) body = opts.form;
  else if (opts.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(opts.body);
  }
  const r = await fetch(path, { method: opts.method || (body ? "POST" : "GET"), headers, body, credentials: "same-origin" });
  if (r.status === 204) return undefined as T;
  const text = await r.text();
  let data: any = undefined;
  try {
    data = text ? JSON.parse(text) : undefined;
  } catch {
    data = text;
  }
  if (!r.ok) {
    const d = data?.detail ?? data;
    const msg = (typeof d === "object" && d?.message) || (typeof d === "string" ? d : `HTTP ${r.status}`);
    throw new ApiError(r.status, msg, d?.detail);
  }
  return data as T;
}

export function errorText(e: unknown): string {
  if (e instanceof ApiError) {
    const det = e.detail;
    if (Array.isArray(det)) return `${e.message}: ${det.map((x) => (typeof x === "string" ? x : x.msg || x.message || JSON.stringify(x))).join(" / ")}`;
    if (typeof det === "string") return `${e.message}: ${det}`;
    return e.message;
  }
  return e instanceof Error ? e.message : String(e);
}

export async function uploadFiles(files: File[]): Promise<{ files: any[]; errors: any[] }> {
  const form = new FormData();
  files.forEach((f) => form.append("files", f, f.name));
  return api("/api/files", { method: "POST", form });
}

export function newKey(): string {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}
