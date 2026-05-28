from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import ContentPost, ProjectState
from app.schemas.content import (
    AiRewriteRequest,
    AnalyzeUrlRequest,
    ContentAssetResponse,
    ContentInspirationResponse,
    ContentPatternResponse,
    ContentPlanItemResponse,
    CreateFromPlanRequest,
    GenerateFromPatternRequest,
    GeneratePostRequest,
    MasterReconstructionRequest,
    MasterReconstructionResponse,
    ImageTaskPrepareResponse,
    ComponentGenerationResponse,
    ComponentQAResponse,
    ComponentRepairResponse,
    FinalLayoutEndpointResponse,
    TechnicalRenderEndpointResponse,
    DraftQAEndpointResponse,
    DraftRepairEndpointResponse,
    DesignPolishEndpointResponse,
    FinalQAEndpointResponse,
    ReconstructionPostEndpointResponse,
    FullPipelineRequest,
    FullPipelineResponse,
    GenerateWeekPlanRequest,
    ImageGenerateRequest,
    ManualEditRequest,
    PostCreateRequest,
    PostResponse,
)
from app.services.content_plan import (
    PlanItemNotFoundError,
    PlanItemStatusError,
    create_post_from_plan_item,
    generate_week_plan,
    get_plan_item_or_raise,
    list_plan_items,
)
from app.services.image_task_engine import (
    ImageTaskError,
    prepare_image_tasks,
)
from app.services.component_generator import (
    ComponentGenerationError,
    execute_image_tasks,
)
from app.services.component_qa_engine import (
    ComponentQAError,
    run_component_qa,
)
from app.services.repair_loop import (
    RepairLoopError,
    run_component_repair_loop,
)
from app.services.final_layout_engine import (
    FinalLayoutError,
    finalize_layout,
)
from app.services.technical_renderer import (
    TechnicalRenderError,
    render_technical_draft,
)
from app.services.draft_qa_engine import (
    DraftQAError,
    run_draft_qa,
)
from app.services.layout_repair_loop import (
    LayoutRepairError,
    run_layout_repair_loop,
)
from app.services.design_polish_engine import (
    DesignPolishError,
    run_design_polish,
)
from app.services.final_qa_engine import (
    FinalQAError,
    run_final_qa,
)
from app.services.reconstruction_post_writer import (
    ReconstructionPostError,
    generate_post_from_reconstruction_state,
)
from app.services.full_pipeline_orchestrator import (
    FullPipelineError,
    run_full_reconstruction_pipeline,
)
from app.services.content_research import (
    InspirationAnalyzeError,
    InspirationNotFoundError,
    create_inspiration,
    fetch_url_preview,
    generate_week_plan_from_inspirations,
    get_inspiration_or_raise,
    list_inspirations,
)
from app.services.master_reconstruction_engine import (
    MasterReconstructionError,
    run_master_reconstruction,
)
from app.services.pattern_analyzer import (
    AssetAnalyzeError,
    PatternNotFoundError,
    generate_post_from_pattern,
    list_assets,
    list_patterns,
)
from app.services.post_manager import (
    PostNotFoundError,
    PostStatusError,
    approve_post,
    create_generated_post,
    edit_post_manually,
    generate_or_replace_image,
    get_post_or_raise,
    publish_post,
    reject_post,
    rewrite_post_with_ai,
)

router = APIRouter()


def _handle_error(exc: Exception):
    if isinstance(exc, PostNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, PostStatusError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, PlanItemNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, PlanItemStatusError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, InspirationNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, InspirationAnalyzeError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, AssetAnalyzeError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, PatternNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, MasterReconstructionError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, ImageTaskError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, ComponentGenerationError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, ComponentQAError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, RepairLoopError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, FinalLayoutError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, TechnicalRenderError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, DraftQAError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, LayoutRepairError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, DesignPolishError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, FinalQAError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, ReconstructionPostError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, FullPipelineError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/posts", response_model=list[PostResponse])
def get_posts(db: Session = Depends(get_db)):
    return db.query(ContentPost).order_by(ContentPost.id.desc()).all()


@router.get("/posts/{post_id}", response_model=PostResponse)
def get_post(post_id: int, db: Session = Depends(get_db)):
    try:
        return get_post_or_raise(db, post_id)
    except Exception as exc:
        _handle_error(exc)


@router.post("/posts", response_model=PostResponse)
def create_post(request: PostCreateRequest, db: Session = Depends(get_db)):
    post = ContentPost(
        title=request.title,
        platform=request.platform,
        text=request.text,
        status="draft",
    )
    db.add(post)
    db.commit()
    db.refresh(post)
    return post


@router.post("/generate-post", response_model=PostResponse)
def generate_post(request: GeneratePostRequest, db: Session = Depends(get_db)):
    try:
        return create_generated_post(
            db=db,
            topic=request.topic,
            platform=request.platform,
            service_offer=request.service_offer,
            with_image=request.with_image,
        )
    except Exception as exc:
        _handle_error(exc)


@router.post("/posts/{post_id}/image", response_model=PostResponse)
def generate_image(post_id: int, request: ImageGenerateRequest, db: Session = Depends(get_db)):
    try:
        return generate_or_replace_image(
            db=db,
            post_id=post_id,
            custom_instruction=request.instruction,
        )
    except Exception as exc:
        _handle_error(exc)


@router.post("/posts/{post_id}/approve", response_model=PostResponse)
def approve(post_id: int, db: Session = Depends(get_db)):
    try:
        return approve_post(db, post_id)
    except Exception as exc:
        _handle_error(exc)


@router.post("/posts/{post_id}/reject", response_model=PostResponse)
def reject(post_id: int, db: Session = Depends(get_db)):
    try:
        return reject_post(db, post_id)
    except Exception as exc:
        _handle_error(exc)


@router.patch("/posts/{post_id}/edit", response_model=PostResponse)
def manual_edit(post_id: int, request: ManualEditRequest, db: Session = Depends(get_db)):
    try:
        return edit_post_manually(db, post_id, request.text)
    except Exception as exc:
        _handle_error(exc)


@router.post("/posts/{post_id}/rewrite", response_model=PostResponse)
def ai_rewrite(post_id: int, request: AiRewriteRequest, db: Session = Depends(get_db)):
    try:
        return rewrite_post_with_ai(db, post_id, request.instruction)
    except Exception as exc:
        _handle_error(exc)


@router.post("/posts/{post_id}/publish", response_model=PostResponse)
def publish(post_id: int, db: Session = Depends(get_db)):
    try:
        return publish_post(db, post_id)
    except Exception as exc:
        _handle_error(exc)


@router.get("/plan", response_model=list[ContentPlanItemResponse])
def get_content_plan(db: Session = Depends(get_db)):
    return list_plan_items(db, limit=30)


@router.post("/plan/week", response_model=list[ContentPlanItemResponse])
def create_week_plan(request: GenerateWeekPlanRequest, db: Session = Depends(get_db)):
    try:
        return generate_week_plan(db=db, platform=request.platform)
    except Exception as exc:
        _handle_error(exc)


@router.get("/plan/{item_id}", response_model=ContentPlanItemResponse)
def get_content_plan_item(item_id: int, db: Session = Depends(get_db)):
    try:
        return get_plan_item_or_raise(db, item_id)
    except Exception as exc:
        _handle_error(exc)


@router.post("/plan/{item_id}/create-post", response_model=PostResponse)
def create_post_from_plan(item_id: int, request: CreateFromPlanRequest, db: Session = Depends(get_db)):
    try:
        _item, post = create_post_from_plan_item(
            db=db,
            item_id=item_id,
            with_image=request.with_image,
        )
        return post
    except Exception as exc:
        _handle_error(exc)


@router.get("/inspirations", response_model=list[ContentInspirationResponse])
def get_inspirations(db: Session = Depends(get_db)):
    return list_inspirations(db, limit=30)


@router.get("/inspirations/{inspiration_id}", response_model=ContentInspirationResponse)
def get_inspiration(inspiration_id: int, db: Session = Depends(get_db)):
    try:
        return get_inspiration_or_raise(db, inspiration_id)
    except Exception as exc:
        _handle_error(exc)


@router.post("/inspirations/analyze-url", response_model=ContentInspirationResponse)
def analyze_url(request: AnalyzeUrlRequest, db: Session = Depends(get_db)):
    try:
        data = fetch_url_preview(request.url)
        return create_inspiration(db, data)
    except Exception as exc:
        _handle_error(exc)


@router.post("/plan/week-from-inspirations", response_model=list[ContentPlanItemResponse])
def create_week_plan_from_inspirations(request: GenerateWeekPlanRequest, db: Session = Depends(get_db)):
    try:
        return generate_week_plan_from_inspirations(db=db, platform=request.platform)
    except Exception as exc:
        _handle_error(exc)


@router.get("/assets", response_model=list[ContentAssetResponse])
def get_assets(db: Session = Depends(get_db)):
    return list_assets(db, limit=30)


@router.get("/patterns", response_model=list[ContentPatternResponse])
def get_patterns(db: Session = Depends(get_db)):
    return list_patterns(db, limit=30)


@router.post("/patterns/{pattern_id}/generate-post", response_model=PostResponse)
def create_post_from_pattern_endpoint(pattern_id: int, request: GenerateFromPatternRequest, db: Session = Depends(get_db)):
    try:
        return generate_post_from_pattern(db=db, pattern_id=pattern_id, with_image=request.with_image)
    except Exception as exc:
        _handle_error(exc)


@router.post("/assets/{asset_id}/master-reconstruction", response_model=MasterReconstructionResponse)
def master_reconstruction(asset_id: int, request: MasterReconstructionRequest, db: Session = Depends(get_db)):
    try:
        state = run_master_reconstruction(db=db, asset_id=asset_id, instruction=request.instruction)
        issues = []
        try:
            import json
            payload = json.loads(state.payload_json or "{}")
            issues = payload.get("analysis_state", {}).get("master_validation_issues", []) or []
        except Exception:
            issues = []
        return {
            "project_state_id": state.id,
            "asset_id": state.asset_id,
            "pipeline_stage": state.pipeline_stage,
            "state_version": state.state_version,
            "master_validation_issues": issues,
        }
    except Exception as exc:
        _handle_error(exc)


@router.post("/assets/{asset_id}/run-full-reconstruction", response_model=FullPipelineResponse)
def run_asset_full_reconstruction(asset_id: int, request: FullPipelineRequest, db: Session = Depends(get_db)):
    try:
        return run_full_reconstruction_pipeline(
            db=db,
            asset_id=asset_id,
            instruction=request.instruction,
            platform=request.platform,
            max_component_repair_attempts=request.max_component_repair_attempts,
            max_layout_repair_attempts=request.max_layout_repair_attempts,
            run_polish=request.run_polish,
            generate_post=request.generate_post,
        )
    except Exception as exc:
        _handle_error(exc)


@router.post("/project-states/{state_id}/image-tasks/prepare", response_model=ImageTaskPrepareResponse)
def prepare_project_image_tasks(state_id: int, db: Session = Depends(get_db)):
    try:
        plan = prepare_image_tasks(db=db, state_id=state_id)
        state = db.query(ProjectState).filter_by(id=state_id).first()
        return {
            "project_state_id": state_id,
            "pipeline_stage": state.pipeline_stage if state else "image_tasks",
            "state_version": state.state_version if state else 0,
            "image_task_count": len(plan.tasks),
            "validation_issues": plan.validation_issues,
            "ready": plan.ready,
        }
    except Exception as exc:
        _handle_error(exc)

@router.post("/project-states/{state_id}/image-tasks/execute", response_model=ComponentGenerationResponse)
def execute_project_image_tasks(state_id: int, only_failed: bool = False, db: Session = Depends(get_db)):
    try:
        return execute_image_tasks(db=db, state_id=state_id, only_failed=only_failed)
    except Exception as exc:
        _handle_error(exc)



@router.post("/project-states/{state_id}/components/qa", response_model=ComponentQAResponse)
def qa_project_components(state_id: int, only_new_or_repaired: bool = True, db: Session = Depends(get_db)):
    try:
        return run_component_qa(db=db, state_id=state_id, only_new_or_repaired=only_new_or_repaired)
    except Exception as exc:
        _handle_error(exc)


@router.post("/project-states/{state_id}/components/repair", response_model=ComponentRepairResponse)
def repair_project_components(state_id: int, db: Session = Depends(get_db)):
    try:
        return run_component_repair_loop(db=db, state_id=state_id)
    except Exception as exc:
        _handle_error(exc)


@router.post("/project-states/{state_id}/layout/finalize", response_model=FinalLayoutEndpointResponse)
def finalize_project_layout(state_id: int, db: Session = Depends(get_db)):
    try:
        return finalize_layout(db=db, state_id=state_id)
    except Exception as exc:
        _handle_error(exc)


@router.post("/project-states/{state_id}/render/technical", response_model=TechnicalRenderEndpointResponse)
def render_project_technical_draft(state_id: int, db: Session = Depends(get_db)):
    try:
        return render_technical_draft(db=db, state_id=state_id)
    except Exception as exc:
        _handle_error(exc)


@router.post("/project-states/{state_id}/draft/qa", response_model=DraftQAEndpointResponse)
def qa_project_technical_draft(state_id: int, db: Session = Depends(get_db)):
    try:
        return run_draft_qa(db=db, state_id=state_id)
    except Exception as exc:
        _handle_error(exc)


@router.post("/project-states/{state_id}/draft/repair", response_model=DraftRepairEndpointResponse)
def repair_project_draft_layout(state_id: int, db: Session = Depends(get_db)):
    try:
        return run_layout_repair_loop(db=db, state_id=state_id)
    except Exception as exc:
        _handle_error(exc)


@router.post("/project-states/{state_id}/design/polish", response_model=DesignPolishEndpointResponse)
def polish_project_design(state_id: int, db: Session = Depends(get_db)):
    try:
        return run_design_polish(db=db, state_id=state_id)
    except Exception as exc:
        _handle_error(exc)


@router.post("/project-states/{state_id}/final/qa", response_model=FinalQAEndpointResponse)
def qa_project_final_image(state_id: int, db: Session = Depends(get_db)):
    try:
        return run_final_qa(db=db, state_id=state_id)
    except Exception as exc:
        _handle_error(exc)


@router.post("/project-states/{state_id}/post/generate", response_model=ReconstructionPostEndpointResponse)
def generate_project_reconstruction_post(state_id: int, platform: str = "telegram", db: Session = Depends(get_db)):
    try:
        return generate_post_from_reconstruction_state(db=db, state_id=state_id, platform=platform)
    except Exception as exc:
        _handle_error(exc)
