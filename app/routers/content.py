from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import ContentPost
from app.schemas.content import GeneratePostRequest, PostCreateRequest, PostResponse
from app.services.copywriter import generate_post_text

router = APIRouter()


@router.get("/posts", response_model=list[PostResponse])
def get_posts(db: Session = Depends(get_db)):
    return db.query(ContentPost).order_by(ContentPost.id.desc()).all()


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
        generated_text = generate_post_text(
            topic=request.topic,
            platform=request.platform,
            service_offer=request.service_offer,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    post = ContentPost(
        title=request.topic,
        platform=request.platform,
        text=generated_text,
        status="generated",
        ai_model=settings.openai_model,
    )
    db.add(post)
    db.commit()
    db.refresh(post)
    return post
