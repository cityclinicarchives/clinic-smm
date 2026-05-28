from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


DraftQAStatus = Literal["ok", "needs_repair", "failed"]


class DraftQAResult(BaseModel):
    project_state_id: int
    pipeline_stage: str
    state_version: int
    render_id: Optional[str] = None
    draft_ok: bool = False
    status: DraftQAStatus = "failed"
    score: float = 0.0
    problems: List[str] = Field(default_factory=list)
    layout_repairs: List[Dict[str, Any]] = Field(default_factory=list)
    recommendation: Literal["polish", "repair_layout", "use_draft", "failed"] = "failed"
    checked_render_path: Optional[str] = None
    qa_history_count: int = 0


class DraftRepairResponse(BaseModel):
    project_state_id: int
    pipeline_stage: str
    state_version: int
    repaired: bool = False
    attempt: int = 0
    max_attempts: int = 3
    selected_best: bool = False
    render_path: Optional[str] = None
    issues: List[str] = Field(default_factory=list)
    layout_blueprint: Dict[str, Any] = Field(default_factory=dict)
    qa_after_repair: Optional[DraftQAResult] = None
    stop_reason: Optional[str] = None
