from pydantic import BaseModel, Field


class PostCreateRequest(BaseModel):
    title: str = Field(..., examples=["Анализы на витамин D"])
    platform: str = Field(..., examples=["telegram"])
    text: str | None = Field(default=None, examples=["Текст черновика"])


class GeneratePostRequest(BaseModel):
    topic: str = Field(..., examples=["Как понять, что организму не хватает витамина D"])
    platform: str = Field(default="telegram", examples=["telegram"])
    service_offer: str | None = Field(
        default=None,
        examples=["В клинике можно сдать анализ на витамин D и получить консультацию врача."],
    )
    with_image: bool = Field(
        default=False,
        description="Если true — сразу сгенерировать изображение к посту.",
    )


class ManualEditRequest(BaseModel):
    text: str = Field(..., examples=["Полный исправленный текст поста"])


class AiRewriteRequest(BaseModel):
    instruction: str = Field(
        ...,
        examples=["Сделай текст короче, убери повторы и добавь мягкий призыв записаться."],
    )


class ImageGenerateRequest(BaseModel):
    instruction: str | None = Field(
        default=None,
        examples=["Сделай изображение без врача, только пациент на ресепшене."],
    )


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
    with_image: bool = Field(default=True, description="Если true — сразу создать изображение к посту.")


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
    url: str = Field(..., examples=["https://example.com/post"])


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
    with_image: bool = Field(default=True, description="Если true — создать пост сразу с изображением.")


class MasterReconstructionRequest(BaseModel):
    instruction: str | None = Field(default=None, description="Дополнительная инструкция для master analytical reconstruction call.")


class MasterReconstructionResponse(BaseModel):
    project_state_id: int
    asset_id: int | None = None
    pipeline_stage: str
    state_version: int
    master_validation_issues: list[str] = Field(default_factory=list)


class ImageTaskPrepareResponse(BaseModel):
    project_state_id: int
    pipeline_stage: str
    state_version: int
    image_task_count: int
    validation_issues: list[str] = Field(default_factory=list)
    ready: bool


class ComponentGenerationResponse(BaseModel):
    project_state_id: int
    pipeline_stage: str
    state_version: int
    generated_count: int
    failed_count: int
    component_status: dict = Field(default_factory=dict)
    issues: list[str] = Field(default_factory=list)


class ComponentQAResponse(BaseModel):
    project_state_id: int
    pipeline_stage: str
    state_version: int
    checked_count: int
    ok_count: int
    needs_repair_count: int
    failed_count: int
    repair_task_count: int
    component_qa: dict = Field(default_factory=dict)
    issues: list[str] = Field(default_factory=list)


class ComponentRepairResponse(BaseModel):
    project_state_id: int
    pipeline_stage: str
    state_version: int
    repaired_count: int
    skipped_count: int
    maxed_out_count: int
    component_status: dict = Field(default_factory=dict)
    component_qa: dict = Field(default_factory=dict)
    issues: list[str] = Field(default_factory=list)


class FinalLayoutEndpointResponse(BaseModel):
    project_state_id: int
    pipeline_stage: str
    state_version: int
    approved_component_count: int
    block_count: int
    layout_blueprint: dict = Field(default_factory=dict)
    validation_issues: list[str] = Field(default_factory=list)
    ready: bool


class TechnicalRenderEndpointResponse(BaseModel):
    project_state_id: int
    pipeline_stage: str
    state_version: int
    render_path: str | None = None
    canvas: dict = Field(default_factory=dict)
    placed_block_count: int
    expected_block_count: int
    issues: list[str] = Field(default_factory=list)
    ready: bool


class DraftQAEndpointResponse(BaseModel):
    project_state_id: int
    pipeline_stage: str
    state_version: int
    render_id: str | None = None
    draft_ok: bool
    status: str
    score: float
    problems: list[str] = Field(default_factory=list)
    layout_repairs: list[dict] = Field(default_factory=list)
    recommendation: str
    checked_render_path: str | None = None
    qa_history_count: int = 0


class DraftRepairEndpointResponse(BaseModel):
    project_state_id: int
    pipeline_stage: str
    state_version: int
    repaired: bool
    attempt: int
    max_attempts: int
    selected_best: bool = False
    render_path: str | None = None
    issues: list[str] = Field(default_factory=list)
    layout_blueprint: dict = Field(default_factory=dict)

class DesignPolishEndpointResponse(BaseModel):
    project_state_id: int
    pipeline_stage: str
    state_version: int
    status: str
    polished_path: str | None = None
    source_render_path: str | None = None
    used_render_id: str | None = None
    issues: list[str] = Field(default_factory=list)
    ready_for_final_qa: bool = False
    polish_record: dict = Field(default_factory=dict)



class FinalQAEndpointResponse(BaseModel):
    project_state_id: int
    pipeline_stage: str
    state_version: int
    status: str
    final_ok: bool
    use_image: str
    final_image_path: str | None = None
    technical_draft_path: str | None = None
    polished_path: str | None = None
    score: float = 0.0
    problems: list[str] = Field(default_factory=list)
    qa_record: dict = Field(default_factory=dict)


class ReconstructionPostEndpointResponse(BaseModel):
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



class FullPipelineRequest(BaseModel):
    instruction: str | None = Field(default=None, description="Дополнительная инструкция для полного автоматического pipeline.")
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
