export const STATE_LABEL: Record<string, string> = {
  CREATED: "作成済み（未開始）",
  INITIALIZING: "初期化中",
  RESUMING: "再開中",
  DOCUMENT_PREP: "資料受付",
  CHAIR_DOCUMENT_ANALYSIS: "議長が資料を解析中",
  CONTEXT_DISTRIBUTION: "Context Packet配布中",
  OPENING: "議長の開始発言",
  INDEPENDENT: "独立提案（並列）",
  CHAIR_REVIEW: "議長の整理",
  DEBATE: "討論中",
  CONTEXT_REQUESTED: "追加根拠を要求中",
  CHAIR_EVIDENCE_SEARCH: "議長が根拠を検索中",
  WAITING_USER_EVIDENCE: "資料不足：あなたの回答待ち",
  EVIDENCE_REVALIDATION: "議長が再検証中",
  CHECKPOINT_REPORT: "中間報告を作成中",
  WAITING_USER_DECISION: "中間判断：あなたの選択待ち",
  FINALIZING: "最終統合中",
  COMPLETED: "完了",
  PAUSED: "一時停止",
  PAUSED_BUDGET: "予算停止",
  FAILED: "失敗",
  CANCELLED: "キャンセル",
};

export const TIER_LABEL: Record<string, string> = {
  primary_cloud: "主クラウド",
  downstream_cloud: "下流クラウド",
  local_llm: "ローカル",
};

export const REASON_LABEL: Record<string, string> = {
  NOT_FOUND: "許可済み資料に記載なし",
  UNREADABLE: "判読・抽出できない",
  CONFLICTING: "資料間で矛盾",
  ACCESS_DENIED: "配布権限のない資料にのみ存在",
  INSUFFICIENT_DETAIL: "判断条件が不足",
  ANALYSIS_FAILED: "議長の解析処理が失敗",
};

export const EVIDENCE_STATE_LABEL: Record<string, string> = {
  WAITING_USER_EVIDENCE: "回答待ち",
  ANSWER_RECEIVED: "受領",
  REVALIDATING: "議長が再検証中",
  STILL_MISSING: "まだ不足",
  RESOLVED: "解決",
  DEFERRED: "保留（未解決）",
  WITHDRAWN: "取り下げ",
};

export const FILE_STATUS_LABEL: Record<string, string> = {
  PENDING: "受付",
  PROCESSING: "抽出中",
  READY: "参照可能",
  PARTIAL: "部分抽出",
  FAILED: "失敗",
  DELETED: "削除済み",
};

export const CONCLUSION_LABEL: Record<string, string> = {
  final: "確定（未解決の重要事項なし）",
  provisional: "暫定（仮説・非重要の未解決あり）",
  blocked: "確定不可（重要な根拠が未解決）",
};

export const PROTOCOL_LABEL: Record<string, string> = {
  openai_chat_compatible: "OpenAI Chat Completions / 互換",
  openai_responses: "OpenAI Responses",
  anthropic_messages: "Anthropic Messages",
  gemini_generate_content: "Gemini generateContent",
  fake: "Fake（デモ）",
};

export function fmtTokens(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return n.toLocaleString("ja-JP");
}

export function fmtSize(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`;
  return `${(n / 1024 / 1024).toFixed(1)} MiB`;
}

export function stripContextRequest(text: string): { text: string; request: any | null } {
  const m = text.match(/```context_request\s*(\{[\s\S]*?\})\s*```/);
  if (!m) return { text, request: null };
  let req = null;
  try {
    req = JSON.parse(m[1]);
  } catch {
    req = null;
  }
  return { text: (text.slice(0, m.index) + text.slice((m.index ?? 0) + m[0].length)).trim(), request: req };
}
