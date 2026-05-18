from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy.sql import func

from app.database import Base


class ContentPost(Base):
    __tablename__ = "content_posts"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(255), nullable=False)
    # Короткий заголовок для публикации и наложения на изображение.
    headline = Column(String(255), nullable=True)
    platform = Column(String(50), nullable=False)
    status = Column(String(50), default="draft", nullable=False)
    text = Column(Text, nullable=True)
    ai_model = Column(String(100), nullable=True)

    image_path = Column(Text, nullable=True)
    image_prompt = Column(Text, nullable=True)
    image_model = Column(String(100), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
