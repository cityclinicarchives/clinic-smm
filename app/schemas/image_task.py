from __future__ import annotations

from typing import Any, Dict, List, Literal

from pydantic import BaseModel, Field, field_validator


ImageTaskOperation = Literal[
    "extract_component",
    "generate_replacement_unit",
    "generate_text_png_block",
    "generate_icon",
    "generate_background",
]


class PngSize(BaseModel):
    w: int = Field(..., ge=64, le=4096)
    h: int = Field(..., ge=64, le=4096)


class ImageTask(BaseModel):
    """Machine-readable atomic task for Image AI.

    One task must produce exactly one reusable PNG component. The task is not a
    vague prompt; it is a contract that later stages can validate and retry.
    """

    task_id: str
    operation: ImageTaskOperation
    final_unit_id: str
    component_ids: List[str] = Field(default_factory=list)
    source_image_required: bool = True
    reference_component_ids: List[str] = Field(default_factory=list)
    instruction_for_image_ai: str
    must_include: List[str] = Field(default_factory=list)
    must_exclude: List[str] = Field(default_factory=list)
    output_png_size: PngSize
    output_format: Literal["png"] = "png"
    transparent_or_neutral_background: bool = True
    max_retries: int = Field(default=3, ge=1, le=3)
    qa_criteria: List[str] = Field(default_factory=list)
    status: Literal["planned", "ready", "failed"] = "planned"
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("task_id", "final_unit_id", "instruction_for_image_ai")
    @classmethod
    def non_empty(cls, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise ValueError("must not be empty")
        return value


class ImageTaskPlan(BaseModel):
    project_state_id: int
    tasks: List[ImageTask]
    validation_issues: List[str] = Field(default_factory=list)
    ready: bool = False


class ImageTaskPrepareResponse(BaseModel):
    project_state_id: int
    pipeline_stage: str
    state_version: int
    image_task_count: int
    validation_issues: List[str] = Field(default_factory=list)
    ready: bool
