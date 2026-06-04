from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import ContentPost
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
    GenerateWeekPlanRequest,
    SemanticReconstructionAnalysisRequest,
    SemanticReconstructionAnalysisResponse,
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
from app.services.content_research import (
    InspirationAnalyzeError,
    InspirationNotFoundError,
    create_inspiration,
    fetch_url_preview,
    generate_week_plan_from_inspirations,
    get_inspiration_or_raise,
    list_inspirations,
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

from app.services.semantic_reconstruction_engine import (
    SemanticReconstructionError,
    run_semantic_reconstruction_analysis,
)

router = APIRouter()


def _handle_error(exc: Exception):
    if isinstance(exc, PostNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, (PostStatusError, PlanItemStatusError, InspirationAnalyzeError, AssetAnalyzeError, SemanticReconstructionError)):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, (PlanItemNotFoundError, InspirationNotFoundError, PatternNotFoundError)):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
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
    post = ContentPost(title=request.title, platform=request.platform, text=request.text, status="draft")
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
        return generate_or_replace_image(db=db, post_id=post_id, custom_instruction=request.instruction)
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
        _item, post = create_post_from_plan_item(db=db, item_id=item_id, with_image=request.with_image)
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


@router.post("/assets/{asset_id}/semantic-reconstruction/analyze", response_model=SemanticReconstructionAnalysisResponse)
def analyze_asset_for_semantic_reconstruction(asset_id: int, request: SemanticReconstructionAnalysisRequest, db: Session = Depends(get_db)):
    try:
        state, issues = run_semantic_reconstruction_analysis(db=db, asset_id=asset_id)
        from app.services.project_state_manager import get_payload
        payload = get_payload(state)
        return SemanticReconstructionAnalysisResponse(
            project_state_id=state.id,
            asset_id=asset_id,
            pipeline_stage=state.pipeline_stage,
            topic=payload.analysis_state.get("topic"),
            visual_entities_count=len(payload.visual_entity_map),
            semantic_png_count=len(payload.semantic_png_plan),
            validation_issues=issues,
            analysis_json_path=payload.analysis_state.get("analysis_json_path") or payload.custom.get("analysis_json_path"),
        )
    except Exception as exc:
        _handle_error(exc)
