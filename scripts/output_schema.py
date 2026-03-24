"""
Pydantic v2 output schemas for all Aria task types, human messages, and quality gates.
Imported by supervisor (Phase 4) for validation and by mission engine (Phase 2) for
structured output. ZERO local workspace imports — this is a leaf-node dependency.
"""

from pydantic import BaseModel, Field, model_validator
from typing import Optional, List, Literal
from datetime import datetime


# --- Task Output Types (SUPV-04) ---

class TaskCompleteOutput(BaseModel):
    """Generic output schema — fallback for all task types."""
    status: Literal["success", "error"]
    summary: str = Field(description="Max 200 characters")
    output_path: Optional[str] = None
    error_detail: Optional[str] = None


class ArticlePublishedOutput(BaseModel):
    url: str
    word_count: int = Field(ge=1)
    canonical: Optional[str] = None
    title: str


class BookUploadedOutput(BaseModel):
    platform: Literal["kdp", "d2d", "google_play", "publishdrive"]
    asin_or_id: str
    live_url: Optional[str] = None
    title: str


class MissionDecompositionOutput(BaseModel):
    mission_id: str
    subtasks: List[str]
    kpis: List[str]
    cadence: Optional[str] = None  # cron expression for recurring missions


# --- Quality Gate (SUPV-07) ---

class QualityGateResult(BaseModel):
    passed: bool
    score: float = Field(ge=0.0, le=1.0)
    issues: List[str] = Field(default_factory=list)
    task_type: str
    validated_at: str  # ISO datetime string


# --- Human-Facing Output (OUTP-01) ---

class HumanMessage(BaseModel):
    text: str = Field(description="Max 4096 characters (Telegram limit)")
    urgency: Literal["emergency", "briefing", "none"] = "none"
    action_required: bool = False

    @model_validator(mode="after")
    def enforce_length(self):
        if len(self.text) > 4096:
            raise ValueError(f"text exceeds Telegram limit: {len(self.text)} chars")
        return self


class DailyBriefing(BaseModel):
    done: List[str] = Field(default_factory=list)
    active: List[str] = Field(default_factory=list)
    tomorrow: List[str] = Field(default_factory=list)
    flag: Optional[str] = None
    action_items: List[str] = Field(default_factory=list)  # OUTP-07: ACTION NEEDED items
    delta_summary: Optional[str] = None                    # OUTP-06: momentum string

    @model_validator(mode="after")
    def enforce_limits(self):
        if len(self.done) > 5:
            raise ValueError("done list exceeds max 5 items")
        if len(self.active) > 3:
            raise ValueError("active list exceeds max 3 items")
        if len(self.tomorrow) > 3:
            raise ValueError("tomorrow list exceeds max 3 items")
        for item in self.done + self.active + self.tomorrow:
            if len(item) > 60:
                raise ValueError(f"item exceeds 60 chars: {item!r}")
        if self.flag and len(self.flag) > 100:
            raise ValueError("flag exceeds 100 chars")
        if len(self.action_items) > 3:
            raise ValueError("action_items exceeds max 3 items")
        for item in self.action_items:
            if len(item) > 80:
                raise ValueError(f"action_items item exceeds 80 chars: {item!r}")
        if self.delta_summary and len(self.delta_summary) > 120:
            raise ValueError("delta_summary exceeds 120 chars")
        return self


class AlertMessage(BaseModel):
    """Emergency alert — sent immediately, separate from daily briefing (OUTP-05)."""
    text: str = Field(description="Max 500 characters — human-readable description")
    category: Literal["credentials_needed", "money_needed", "blocker_critical"]
    task_id: Optional[str] = None
    tried: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def enforce_length(self):
        if len(self.text) > 500:
            raise ValueError(f"alert text exceeds 500 chars: {len(self.text)}")
        return self


# --- Schema registry for supervisor lookup (Phase 4) ---

OUTPUT_SCHEMAS = {
    "alert": AlertMessage,
    "article_published": ArticlePublishedOutput,
    "book_uploaded": BookUploadedOutput,
    "mission_decomposed": MissionDecompositionOutput,
    "task_complete": TaskCompleteOutput,
}

# To add a new task type:
# 1. Define a new BaseModel class above
# 2. Add it to OUTPUT_SCHEMAS dict
# 3. Supervisor will auto-discover it via the registry
