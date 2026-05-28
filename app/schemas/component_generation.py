from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


ComponentStatus = Literal["pending", "generated", "failed", "needs_repair"]


class ComponentRecord(BaseModel):
    """Persistent record for one generated PNG component.

    This record is intentionally redundant: later QA/repair/layout stages must be
    able to reconstruct the exact image-task contract without asking the model to
    remember previous context.
    """

    component_id: str
    task_id: str
    final_unit_id: str
    operation: str

    # Optional semantic links used by future QA/repair stages.
    source_unit_id: Optional[str] = None
    reference_unit_id: Optional[str] = None
    reference_component_ids: List[str] = Field(default_factory=list)

    path: Optional[str] = None
    status: ComponentStatus = "pending"
    retry_count: int = 0
    best_version: bool = True
    error: Optional[str] = None

    # Full task contract saved for downstream stages.
    prompt: Optional[str] = None
    instruction_for_image_ai: Optional[str] = None
    must_include: List[str] = Field(default_factory=list)
    must_exclude: List[str] = Field(default_factory=list)
    qa_criteria: List[str] = Field(default_factory=list)
    source_image_required: bool = True
    output_size: Dict[str, int] = Field(default_factory=dict)
    task_contract: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ComponentGenerationResponse(BaseModel):
    project_state_id: int
    pipeline_stage: str
    state_version: int
    generated_count: int
    skipped_count: int = 0
    failed_count: int
    component_status: Dict[str, Any] = Field(default_factory=dict)
    issues: List[str] = Field(default_factory=list)
