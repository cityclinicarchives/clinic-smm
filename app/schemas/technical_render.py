from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


TechnicalRenderStatus = Literal["ready", "failed"]


class TechnicalRenderResult(BaseModel):
    project_state_id: int
    pipeline_stage: str
    state_version: int
    render_path: Optional[str] = None
    canvas: Dict[str, Any] = Field(default_factory=dict)
    placed_block_count: int = 0
    expected_block_count: int = 0
    issues: List[str] = Field(default_factory=list)
    ready: bool = False


class TechnicalRenderManifest(BaseModel):
    render_id: Optional[str] = None
    status: TechnicalRenderStatus = "ready"
    render_path: Optional[str] = None
    canvas: Dict[str, Any] = Field(default_factory=dict)
    placed_blocks: List[Dict[str, Any]] = Field(default_factory=list)
    issues: List[str] = Field(default_factory=list)
