export type Tier = "primary_cloud" | "downstream_cloud" | "local_llm";

export interface Agent {
  id: string;
  name: string;
  kind: "chair" | "participant";
  role_id: string;
  role_name?: string;
  profile_id: string;
  model_override?: string | null;
  enabled: boolean;
  avatar?: string | null;
  routing_profiles: Partial<Record<Tier, string | null>>;
  tier_token_budgets: Partial<Record<Tier, number | null>>;
  capabilities?: string[];
}

export interface Profile {
  id: string;
  name: string;
  protocol: string;
  base_url: string | null;
  default_model: string;
  env_var: string | null;
  enabled: boolean;
  billing_family: "openai_claude" | "other_cloud" | "local";
  allow_document_transfer: boolean;
  has_key: boolean | null;
  quota: {
    mode: "auto" | "manual" | "disabled";
    admin_env_var: string | null;
    period_label: string;
    manual_budget_tokens: number | null;
    initial_bootstrap_budget: number | null;
    quota_pool_id: string | null;
    has_admin_key: boolean;
  };
  quality: { status: string; validated_model: string | null; validated_at: string | null; summary: string | null };
}

export interface Role {
  id: string;
  name: string;
  description: string;
  agent_file: string;
  capabilities: string[];
  in_use_by?: string[];
  agent_md?: string;
}

export interface Message {
  id: string;
  agent_id: string | null;
  seq: number;
  round: number | null;
  phase: string | null;
  kind: "turn" | "chair" | "user" | "system" | "checkpoint" | "final";
  content: string;
  status: "streaming" | "completed" | "incomplete" | "failed" | "quality_failed";
  tier: Tier | null;
  profile_id: string | null;
  model_id: string | null;
  created_at: string;
  completed_at: string | null;
  handoff_id: string | null;
  scope: string[];
  packet_refs: { packet_id: string; packet_version: number }[];
  meta: Record<string, any>;
}

export interface FileInfo {
  file_id: string;
  name: string;
  kind: string;
  size: number;
  status: string;
  error: string | null;
  extract_version: number;
  pages?: number | null;
  sheets?: number | null;
  slides?: number | null;
  chunks?: number | null;
  tokens?: number | null;
  unreadable: { location: Record<string, any>; reason: string }[];
  allowed_agent_ids?: string[] | null;
  enabled?: boolean;
  required?: boolean;
  allow_partial?: boolean;
}

export interface EvidenceRequest {
  evidence_request_id: string;
  session_id: string;
  origin_context_request_id: string;
  state: string;
  reason_code: string;
  criticality: "critical" | "noncritical";
  question: string;
  why_needed: string;
  searched_sources: { file_id: string; label?: string; scope: string }[];
  missing_fields: string[];
  acceptable_inputs: string[];
  impact_if_unresolved: string;
  created_at: string;
  version: number;
  ask_count: number;
  agent_id?: string;
  agent_name?: string;
  agent_question?: string;
  detail?: string;
  needed_materials?: string;
  conflicts?: { evidence_ids: string[]; note?: string }[];
  responses?: { action: string; statement?: string; created_at: string; source_kind?: string; verification_status?: string }[];
}

export interface Blocker {
  type: string;
  message?: string;
  reasons?: string[];
  agent_id?: string;
  evidence_request_id?: string;
  criticality?: string;
  hard?: boolean;
}

export interface SessionDetail {
  id: string;
  topic: string;
  state: string;
  created_at: string;
  started_at: string | null;
  ended_at: string | null;
  error: string | null;
  progress: { phase: string; round: number; rounds_target: number; independent_done: string[]; debate_done: string[]; pending_redo: any[] };
  agents: Agent[];
  settings: { debate_rounds: number };
  final_report: any;
  conclusion_status: string | null;
  blockers: Blocker[];
  messages: Message[];
  usage: { input_tokens: number; output_tokens: number; total_tokens: number; estimated_cost: number | null; cost_complete: boolean; by_agent: Record<string, any> };
  attachments: FileInfo[];
  evidence_requests: EvidenceRequest[];
  last_event_id: number;
  budgets: { agent_id: string; tier: Tier; used_tokens: number; limit_tokens: number | null }[];
  baselines: any[];
  handoffs: any[];
  analysis_runs: any[];
}

export interface StreamEvent {
  event_id: number;
  session_id: string;
  timestamp: string;
  type: string;
  [k: string]: any;
}
