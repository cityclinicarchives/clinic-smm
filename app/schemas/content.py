from pydantic import BaseModel, Field


class PostCreateRequest(BaseModel):
    title: str = Field(..., examples=["Анализы на витамин D"])
    platform: str = Field(..., examples=["telegram"])
    text: str | None = Field(default=None, examples=["Текст черновика"])


class GeneratePostRequest(BaseModel):
    topic: str = Field(..., examples=["Как понять, что организму не хватает витамина D"])
    platform: str = Field(default="telegram", examples=["telegram"])
    service_offer: str | None = Field(default=None)
    with_image: bool = Field(default=False)


class ManualEditRequest(BaseModel):
    text: str


class AiRewriteRequest(BaseModel):
    instruction: str


class ImageGenerateRequest(BaseModel):
    instruction: str | None = None


class PostResponse(BaseModel):
    id: int
    title: str
    headline: str | None = None
    platform: str
    status: str
    text: str | None = None
    ai_model: str | None = None
    image_path: str | None = None
    image_prompt: str | None = None
    image_model: str | None = None

    class Config:
        from_attributes = True


class GenerateWeekPlanRequest(BaseModel):
    platform: str = Field(default="telegram", examples=["telegram"])


class CreateFromPlanRequest(BaseModel):
    with_image: bool = Field(default=True)


class ContentPlanItemResponse(BaseModel):
    id: int
    planned_date: str | None = None
    topic: str
    platform: str
    status: str
    source: str | None = None
    created_post_id: int | None = None

    class Config:
        from_attributes = True


class AnalyzeUrlRequest(BaseModel):
    url: str


class ContentInspirationResponse(BaseModel):
    id: int
    source_type: str
    source_url: str | None = None
    original_text: str | None = None
    media_type: str | None = None
    analysis: str | None = None
    idea: str | None = None
    format: str | None = None
    hook: str | None = None
    why_it_works: str | None = None
    clinic_service: str | None = None
    risks: str | None = None
    recommended_topic: str | None = None

    class Config:
        from_attributes = True


class ContentAssetResponse(BaseModel):
    id: int
    source_type: str
    source_url: str | None = None
    text_content: str | None = None
    caption: str | None = None
    media_type: str | None = None
    content_summary: str | None = None
    analysis: str | None = None

    class Config:
        from_attributes = True


class ContentPatternResponse(BaseModel):
    id: int
    asset_id: int | None = None
    hook_type: str | None = None
    emotion: str | None = None
    pain_point: str | None = None
    format: str | None = None
    visual_style: str | None = None
    humor_mechanic: str | None = None
    engagement_reason: str | None = None
    cta_type: str | None = None
    content_mechanic: str | None = None
    analysis: str | None = None

    class Config:
        from_attributes = True


class GenerateFromPatternRequest(BaseModel):
    with_image: bool = Field(default=True)


class SemanticReconstructionAnalysisRequest(BaseModel):
    force_new_state: bool = Field(default=True)


class SemanticReconstructionAnalysisResponse(BaseModel):
    project_state_id: int
    asset_id: int
    pipeline_stage: str
    topic: str | None = None
    visual_entities_count: int = 0
    semantic_png_count: int = 0
    validation_issues: list[str] = Field(default_factory=list)
    analysis_json_path: str | None = None
