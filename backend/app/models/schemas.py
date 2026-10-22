"""Pydantic models for JSON config files and REST payloads."""
from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

Protocol = Literal[
    "openai_chat_compatible",
    "openai_responses",
    "anthropic_messages",
    "gemini_generate_content",
    "fake",
]
BillingFamily = Literal["openai_claude", "other_cloud", "local"]
Tier = Literal["primary_cloud", "downstream_cloud", "local_llm"]
TIERS: tuple[str, ...] = ("primary_cloud", "downstream_cloud", "local_llm")

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,62}$")
_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")


def check_id(v: str) -> str:
    if not _ID_RE.match(v):
        raise ValueError("id must match ^[a-z0-9][a-z0-9_-]{0,62}$")
    return v


# ---------------------------------------------------------------- API profiles
class QuotaConfig(BaseModel):
    """Account-level usage threshold settings (DESIGN 7A.3)."""

    mode: Literal["auto", "manual", "disabled"] = "disabled"
    admin_env_var: Optional[str] = None  # separate admin credential for usage API
    period_label: str = "当月（UTC暦月）"
    manual_budget_tokens: Optional[int] = Field(default=None, ge=0)
    initial_bootstrap_budget: Optional[int] = Field(default=None, ge=0)
    quota_pool_id: Optional[str] = None

    @field_validator("admin_env_var")
    @classmethod
    def _env(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not _ENV_RE.match(v):
            raise ValueError("admin_env_var must be an UPPER_SNAKE env var name")
        return v


class QualityStatus(BaseModel):
    status: Literal["unvalidated", "passed", "failed"] = "unvalidated"
    validated_model: Optional[str] = None
    validated_at: Optional[str] = None
    summary: Optional[str] = None


class ApiProfile(BaseModel):
    id: str
    name: str = Field(min_length=1, max_length=80)
    protocol: Protocol
    base_url: Optional[str] = None
    default_model: str = ""
    env_var: Optional[str] = None
    enabled: bool = True
    billing_family: BillingFamily = "other_cloud"
    allow_document_transfer: bool = False  # may attachment-derived text be sent here?
    quota: QuotaConfig = Field(default_factory=QuotaConfig)
    quality: QualityStatus = Field(default_factory=QualityStatus)

    @field_validator("id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        return check_id(v)

    @field_validator("env_var")
    @classmethod
    def _env(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not _ENV_RE.match(v):
            raise ValueError("env_var must be an UPPER_SNAKE env var name")
        return v

    @field_validator("base_url")
    @classmethod
    def _url(cls, v: Optional[str]) -> Optional[str]:
        if v in (None, ""):
            return None
        if not re.match(r"^https?://", v):
            raise ValueError("base_url must start with http:// or https://")
        return v.rstrip("/")

    @property
    def pool_id(self) -> str:
        return self.quota.quota_pool_id or self.id


class ApiProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    protocol: Protocol
    base_url: Optional[str] = None
    default_model: str = ""
    api_key: Optional[str] = None  # written to .env only, never echoed
    requires_key: bool = True
    billing_family: BillingFamily = "other_cloud"
    allow_document_transfer: bool = False


class ApiProfilePatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    protocol: Optional[Protocol] = None
    base_url: Optional[str] = None
    default_model: Optional[str] = None
    enabled: Optional[bool] = None
    billing_family: Optional[BillingFamily] = None
    allow_document_transfer: Optional[bool] = None


class SecretPut(BaseModel):
    api_key: str = Field(min_length=1, max_length=4096)


class QuotaPut(BaseModel):
    quota: QuotaConfig
    admin_api_key: Optional[str] = None  # optional: writes admin credential to .env


# ---------------------------------------------------------------- roles
class RoleMeta(BaseModel):
    id: str
    name: str = Field(min_length=1, max_length=80)
    description: str = ""
    agent_file: str = "Agent.MD"
    capabilities: list[str] = Field(default_factory=lambda: ["text_debate"])

    @field_validator("id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        return check_id(v)

    @field_validator("agent_file")
    @classmethod
    def _file(cls, v: str) -> str:
        if "/" in v or "\\" in v or v.startswith(".") or not v.lower().endswith(".md"):
            raise ValueError("agent_file must be a plain .md file name")
        return v


class RoleUpsert(BaseModel):
    id: Optional[str] = None
    name: str = Field(min_length=1, max_length=80)
    description: str = ""
    agent_md: str = Field(default="", max_length=200_000)
    copy_from: Optional[str] = None


# ---------------------------------------------------------------- agents
class AgentConfig(BaseModel):
    id: str
    name: str = Field(min_length=1, max_length=40)
    kind: Literal["chair", "participant"]
    role_id: str
    profile_id: str
    model_override: Optional[str] = None
    enabled: bool = True
    avatar: Optional[str] = None
    routing_profiles: dict[str, Optional[str]] = Field(default_factory=dict)
    tier_token_budgets: dict[str, Optional[int]] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        return check_id(v)

    @field_validator("routing_profiles", "tier_token_budgets")
    @classmethod
    def _tiers(cls, v: dict) -> dict:
        for k in v:
            if k not in TIERS:
                raise ValueError(f"unknown tier {k}")
        return v

    @property
    def capabilities(self) -> list[str]:
        if self.kind == "chair":
            return ["document_analysis", "retrieval", "context_distribution"]
        return ["text_debate"]


class AgentsDoc(BaseModel):
    agents: list[AgentConfig]

    @model_validator(mode="after")
    def _rules(self) -> "AgentsDoc":
        chairs = [a for a in self.agents if a.kind == "chair"]
        if len(chairs) != 1:
            raise ValueError("議長(chair)はちょうど1人必要です")
        if chairs[0].role_id != "chair":
            raise ValueError("議長の役割は chair に固定です")
        parts = [a for a in self.agents if a.kind == "participant"]
        if len(parts) < 3:
            raise ValueError("一般Agentは最低3人必要です")
        if any(p.role_id == "chair" for p in parts):
            raise ValueError("一般Agentに chair 役割は割り当てられません")
        ids = [a.id for a in self.agents]
        if len(ids) != len(set(ids)):
            raise ValueError("agent id が重複しています")
        return self


# ---------------------------------------------------------------- debate settings
class CheckpointSettings(BaseModel):
    required_before_finalization: Literal[True] = True
    allowed_decisions: list[str] = Field(default_factory=lambda: ["continue", "redirect", "finish"])
    on_no_decision: Literal["wait"] = "wait"


class RoutingSettings(BaseModel):
    priority: list[str] = Field(default_factory=lambda: list(TIERS))
    baseline_refresh: Literal["session_start_only"] = "session_start_only"
    openai_claude_extra_fraction: float = 1 / 6
    other_cloud_extra_fraction: float = 0.25
    usage_account_scope: str = "configured_billing_period"
    account_usage_unavailable: Literal["manual_budget_required"] = "manual_budget_required"
    agent_budget_units: Literal["input_plus_output_tokens"] = "input_plus_output_tokens"
    require_quality_validation_for_downstream: bool = True


class CompressionSettings(BaseModel):
    compression_mode: Literal["auto", "off", "manual"] = "auto"
    history_compress_threshold: int = Field(default=8000, ge=500)
    keep_recent_messages: int = Field(default=6, ge=1)
    summary_target_tokens: int = Field(default=800, ge=100)
    compression_boundary: Literal["round_end"] = "round_end"


class DebateParams(BaseModel):
    debate_rounds: int = Field(default=2, ge=1, le=20)
    max_parallel_requests: int = Field(default=3, ge=1, le=10)
    max_output_tokens: int = Field(default=800, ge=64, le=32000)
    temperature: float = Field(default=0.7, ge=0, le=2)
    max_agents: int = Field(default=10, ge=4, le=30)
    request_timeout_s: float = Field(default=120, ge=5)
    max_retries: int = Field(default=3, ge=0, le=10)


class ChairSettings(BaseModel):
    chair_retrieval_budget: int = Field(default=3000, ge=200)
    agent_packet_target_tokens: int = Field(default=1500, ge=200)
    chair_retrieval_mode: Literal["auto"] = "auto"
    direct_input_threshold_tokens: int = Field(default=2500, ge=100)
    max_search_queries: int = Field(default=5, ge=1, le=50)
    max_evidence_reasks: int = Field(default=2, ge=0, le=10)


class LimitSettings(BaseModel):
    max_session_tokens: Optional[int] = Field(default=None, ge=1)
    max_estimated_cost: Optional[float] = Field(default=None, ge=0)


class PriceEntry(BaseModel):
    input_price_per_1m: float = Field(ge=0)
    output_price_per_1m: float = Field(ge=0)
    cached_price_per_1m: Optional[float] = Field(default=None, ge=0)


class DebateSettings(BaseModel):
    checkpoint: CheckpointSettings = Field(default_factory=CheckpointSettings)
    routing: RoutingSettings = Field(default_factory=RoutingSettings)
    compression: CompressionSettings = Field(default_factory=CompressionSettings)
    debate: DebateParams = Field(default_factory=DebateParams)
    chair: ChairSettings = Field(default_factory=ChairSettings)
    limits: LimitSettings = Field(default_factory=LimitSettings)
    pricing: dict[str, PriceEntry] = Field(default_factory=dict)


# ---------------------------------------------------------------- sessions
class SessionCreate(BaseModel):
    topic: str = Field(min_length=1, max_length=20_000)
    attachment_ids: list[str] = Field(default_factory=list)
    debate_rounds: Optional[int] = Field(default=None, ge=1, le=20)


class DecisionIn(BaseModel):
    decision: Literal["continue", "redirect", "finish"]
    instruction: Optional[str] = Field(default=None, max_length=10_000)
    idempotency_key: Optional[str] = None


class MessageIn(BaseModel):
    text: str = Field(default="", max_length=20_000)
    attachment_ids: list[str] = Field(default_factory=list)


class AttachIn(BaseModel):
    file_id: str
    allowed_agent_ids: Optional[list[str]] = None  # None = all participants
    required: bool = False
    allow_partial: bool = False


class AttachPatch(BaseModel):
    allowed_agent_ids: Optional[list[str]] = None
    enabled: Optional[bool] = None
    required: Optional[bool] = None
    allow_partial: Optional[bool] = None


class EvidenceResponseIn(BaseModel):
    """contracts/evidence_response.schema.json"""

    evidence_request_id: str
    expected_version: int = Field(ge=1)
    action: Literal[
        "file_upload",
        "user_statement",
        "unavailable",
        "withdraw",
        "continue_as_hypothesis",
        "finish_with_unresolved",
    ]
    file_ids: list[str] = Field(default_factory=list)
    statement: Optional[str] = Field(default=None, max_length=20_000)
    idempotency_key: str = Field(min_length=1, max_length=200)


class BudgetPut(BaseModel):
    tier_token_budgets: dict[str, Optional[int]]


class ImportAgentMd(BaseModel):
    content: str = Field(max_length=200_000)
    confirm: bool = False
