from __future__ import annotations

from pydantic import BaseModel, Field


class ReconstructionPostResult(BaseModel):
    project_state_id: int
    pipeline_stage: str
    state_version: int
    post_id: int | None = None
    post_title: str = ""
    post_text: str = ""
    cta: str = ""
    final_image_path: str | None = None
    status: str = "draft_created"
    issues: list[str] = Field(default_factory=list)
    post_record: dict = Field(default_factory=dict)
