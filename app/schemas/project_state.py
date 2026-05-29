from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ContinuationPackage(BaseModel):
    """Persistent memory package for future infographic reconstruction pipelines."""

    current_state_summary: str = ""
    strict_contract: Dict[str, Any] = Field(default_factory=dict)
    must_not_forget: List[str] = Field(default_factory=list)
    next_step_prompt: str = ""
    last_successful_stage: str = "intake"


class ProjectStatePayload(BaseModel):
    """Generic state container kept intentionally lightweight in the cleaned baseline."""

    analysis_state: Dict[str, Any] = Field(default_factory=dict)
    visual_entity_map: List[Dict[str, Any]] = Field(default_factory=list)
    semantic_png_plan: List[Dict[str, Any]] = Field(default_factory=list)
    design_blueprint: Dict[str, Any] = Field(default_factory=dict)
    post: Dict[str, Any] = Field(default_factory=dict)
    qa_checklist: List[str] = Field(default_factory=list)
    continuation_package: ContinuationPackage = Field(default_factory=ContinuationPackage)
    custom: Dict[str, Any] = Field(default_factory=dict)


class ProjectStateCreate(BaseModel):
    asset_id: Optional[int] = None
    pipeline_stage: str = "intake"
    payload: ProjectStatePayload = Field(default_factory=ProjectStatePayload)


class ProjectStateRead(BaseModel):
    id: int
    asset_id: Optional[int]
    state_version: int
    pipeline_stage: str
    payload: ProjectStatePayload

    class Config:
        from_attributes = True
