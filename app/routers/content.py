from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import ContentPost
from app.schemas.content import (
    AiRewriteRequest,
    GeneratePostRequest,
    ImageGenerateRequest,
    ManualEditRequest,
    PostCreateRequest,
    PostResponse,
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
