from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import ContentPost
from app.schemas.content import ContentPostCreate, ContentPostOut, ContentPostUpdate

router = APIRouter(prefix="/posts", tags=["posts"])


@router.post("", response_model=ContentPostOut)
def create_post(payload: ContentPostCreate, db: Session = Depends(get_db)):
    post = ContentPost(
        title=payload.title,
        platform=payload.platform,
        text=payload.text,
        status="draft",
    )
    db.add(post)
    db.commit()
    db.refresh(post)
    return post


@router.get("", response_model=list[ContentPostOut])
def get_posts(db: Session = Depends(get_db)):
    return db.query(ContentPost).order_by(ContentPost.id.desc()).all()


@router.get("/{post_id}", response_model=ContentPostOut)
def get_post(post_id: int, db: Session = Depends(get_db)):
    post = db.query(ContentPost).filter(ContentPost.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    return post


@router.patch("/{post_id}", response_model=ContentPostOut)
def update_post(post_id: int, payload: ContentPostUpdate, db: Session = Depends(get_db)):
    post = db.query(ContentPost).filter(ContentPost.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    update_data = payload.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(post, key, value)

    db.commit()
    db.refresh(post)
    return post


@router.delete("/{post_id}")
def delete_post(post_id: int, db: Session = Depends(get_db)):
    post = db.query(ContentPost).filter(ContentPost.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    db.delete(post)
    db.commit()
    return {"status": "deleted", "id": post_id}
