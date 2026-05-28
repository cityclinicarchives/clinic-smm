from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


FinalImageChoice = Literal["polished", "technical_draft", "none"]
FinalQAStatus = Literal["passed", "fallback_to_draft", "failed"]


class FinalQAResult(BaseModel):
    project_state_id: int
    pipeline_stage: str
    state_version: int
    status: FinalQAStatus
    final_ok: bool
    use_image: FinalImageChoice
    final_image_path: Optional[str] = None
    technical_draft_path: Optional[str] = None
    polished_path: Optional[str] = None
    score: float = 0.0
    problems: List[str] = Field(default_factory=list)
    qa_record: Dict[str, Any] = Field(default_factory=dict)
