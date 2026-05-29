from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class SemanticAnalysisRequest(BaseModel):
    force_new_state: bool = Field(default=True)


class SemanticAnalysisResponse(BaseModel):
    project_state_id: int
    asset_id: int
    pipeline_stage: str
    topic: str | None = None
    visual_entities_count: int = 0
    semantic_png_count: int = 0
    validation_issues: List[str] = Field(default_factory=list)
    payload: Dict[str, Any] = Field(default_factory=dict)


class SemanticPngAsset(BaseModel):
    png_id: str
    entity_id: str
    path: Optional[str] = None
    status: str = "planned"
    meta: Dict[str, Any] = Field(default_factory=dict)
