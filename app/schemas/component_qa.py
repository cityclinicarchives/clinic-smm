from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


ComponentQAStatus = Literal["ok", "needs_repair", "failed"]


class ComponentQAItem(BaseModel):
    component_id: str
    task_id: str
    status: ComponentQAStatus
    score: float = 0.0
    problems: List[str] = Field(default_factory=list)
    repair_needed: bool = False
    repair_instruction: Optional[str] = None
    retry_count: int = 0
    selected_as_best: bool = False
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ComponentQAResponse(BaseModel):
    project_state_id: int
    pipeline_stage: str
    state_version: int
    checked_count: int
    ok_count: int
    needs_repair_count: int
    failed_count: int
    repair_task_count: int
    component_qa: Dict[str, Any] = Field(default_factory=dict)
    issues: List[str] = Field(default_factory=list)


class ComponentRepairResponse(BaseModel):
    project_state_id: int
    pipeline_stage: str
    state_version: int
    repaired_count: int
    skipped_count: int
    maxed_out_count: int
    component_status: Dict[str, Any] = Field(default_factory=dict)
    component_qa: Dict[str, Any] = Field(default_factory=dict)
    issues: List[str] = Field(default_factory=list)
