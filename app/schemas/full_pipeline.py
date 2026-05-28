from __future__ import annotations

from pydantic import BaseModel, Field


class FullPipelineRequest(BaseModel):
    instruction: str | None = Field(default=None, description="Дополнительная инструкция для полного pipeline.")
    platform: str = Field(default="telegram")
    max_component_repair_attempts: int = Field(default=3, ge=0, le=5)
    max_layout_repair_attempts: int = Field(default=3, ge=0, le=5)
    run_polish: bool = Field(default=True)
    generate_post: bool = Field(default=True)


class FullPipelineResponse(BaseModel):
    project_state_id: int | None = None
    pipeline_stage: str = ""
    state_version: int = 0
    status: str
    final_image_path: str | None = None
    post_id: int | None = None
    post_title: str = ""
    post_text: str = ""
    steps: list[dict] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
