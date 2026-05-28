from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


PipelineStage = Literal[
    "intake",
    "master_reconstruction",
    "image_tasks",
    "component_qa",
    "layout_refinement",
    "technical_render",
    "draft_qa",
    "design_polish",
    "final_qa",
    "post_generation",
    "completed",
    "failed",
]


class ContinuationPackage(BaseModel):
    """
    Persistent memory package that must be passed to every later AI call.
    It is not a short summary. It is the project contract/state snapshot.
    """

    current_state_summary: str = ""
    strict_contract: Dict[str, Any] = Field(default_factory=dict)
    must_not_forget: List[str] = Field(default_factory=list)
    next_step_prompt: str = ""
    last_successful_stage: Optional[PipelineStage] = None


class ProjectStatePayload(BaseModel):
    analysis_state: Dict[str, Any] = Field(default_factory=dict)
    semantic_units: List[Dict[str, Any]] = Field(default_factory=list)
    unit_decisions: List[Dict[str, Any]] = Field(default_factory=list)
    final_units: List[Dict[str, Any]] = Field(default_factory=list)
    component_map: List[Dict[str, Any]] = Field(default_factory=list)
    layout_blueprint: Dict[str, Any] = Field(default_factory=dict)
    image_tasks: List[Dict[str, Any]] = Field(default_factory=list)
    component_status: Dict[str, Any] = Field(default_factory=dict)
    layout_versions: List[Dict[str, Any]] = Field(default_factory=list)
    render_history: List[Dict[str, Any]] = Field(default_factory=list)
    approved_component_versions: Dict[str, Any] = Field(default_factory=dict)
    post_brief: Dict[str, Any] = Field(default_factory=dict)
    continuation_package: ContinuationPackage = Field(default_factory=ContinuationPackage)

    @field_validator(
        "semantic_units",
        "unit_decisions",
        "final_units",
        "component_map",
        "image_tasks",
        "layout_versions",
        "render_history",
        mode="before",
    )
    @classmethod
    def filter_non_dict_list_items(cls, value: Any) -> list[dict[str, Any]]:
        """Keep project state loadable even if an AI response used placeholders.

        The master AI must return arrays of objects, but models sometimes emit
        placeholder strings such as "... same generate_label ...". Those items
        must not crash the application. Stage validators record detailed issues
        and later engines regenerate missing image tasks from final_units.
        """
        if value is None:
            return []
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, dict)]


class ProjectStateCreate(BaseModel):
    asset_id: Optional[int] = None
    reconstruction_id: Optional[int] = None
    pipeline_stage: PipelineStage = "intake"
    payload: ProjectStatePayload = Field(default_factory=ProjectStatePayload)


class ProjectStateUpdate(BaseModel):
    pipeline_stage: Optional[PipelineStage] = None
    payload: Optional[ProjectStatePayload] = None
    stage_result: Optional[Dict[str, Any]] = None


class ProjectStateRead(BaseModel):
    id: int
    asset_id: Optional[int]
    reconstruction_id: Optional[int]
    state_version: int
    pipeline_stage: PipelineStage
    payload: ProjectStatePayload
    stage_history: List[Dict[str, Any]]
    created_at: Optional[datetime]
    updated_at: Optional[datetime]

    class Config:
        from_attributes = True
