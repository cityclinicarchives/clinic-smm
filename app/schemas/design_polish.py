from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


DesignPolishStatus = Literal["ready", "skipped", "failed"]


class DesignPolishResult(BaseModel):
    project_state_id: int
    pipeline_stage: str
    state_version: int
    status: DesignPolishStatus = "failed"
    polished_path: Optional[str] = None
    source_render_path: Optional[str] = None
    used_render_id: Optional[str] = None
    prompt: Optional[str] = None
    issues: List[str] = Field(default_factory=list)
    ready_for_final_qa: bool = False
    polish_record: Dict[str, Any] = Field(default_factory=dict)
