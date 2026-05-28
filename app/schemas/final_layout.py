from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


LayoutStatus = Literal["ready", "needs_components", "failed"]


class LayoutComponent(BaseModel):
    component_id: str
    path: str
    w: int
    h: int
    final_unit_id: Optional[str] = None
    component_type: Optional[str] = None
    operation: Optional[str] = None
    status: str = "generated"
    qa_status: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class CanvasSpec(BaseModel):
    aspect_ratio: str = "4:5"
    w: int = 1080
    h: int = 1350
    background: str = "#FFFFFF"


class LayoutBlock(BaseModel):
    block_id: str
    component_id: str
    x: int
    y: int
    w: int
    h: int
    z_index: int = 0
    preserve_aspect_ratio: bool = True
    fit_mode: Literal["contain", "cover"] = "contain"
    metadata: Dict[str, Any] = Field(default_factory=dict)


class FinalLayoutBlueprint(BaseModel):
    canvas: CanvasSpec = Field(default_factory=CanvasSpec)
    blocks: List[LayoutBlock] = Field(default_factory=list)
    layout_notes: List[str] = Field(default_factory=list)
    validation_issues: List[str] = Field(default_factory=list)
    status: LayoutStatus = "ready"


class FinalLayoutResponse(BaseModel):
    project_state_id: int
    pipeline_stage: str
    state_version: int
    approved_component_count: int
    block_count: int
    layout_blueprint: Dict[str, Any] = Field(default_factory=dict)
    validation_issues: List[str] = Field(default_factory=list)
    ready: bool = False
