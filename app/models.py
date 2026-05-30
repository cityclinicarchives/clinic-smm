from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
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


class ContentPlanItem(Base):
    __tablename__ = "content_plan_items"

    id = Column(Integer, primary_key=True, index=True)
    planned_date = Column(String(20), nullable=True)
    topic = Column(String(500), nullable=False)
    platform = Column(String(50), default="telegram", nullable=False)
    status = Column(String(50), default="planned", nullable=False)
    source = Column(String(100), default="ai_week_plan", nullable=True)
    created_post_id = Column(Integer, ForeignKey("content_posts.id"), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ContentInspiration(Base):
    __tablename__ = "content_inspirations"

    id = Column(Integer, primary_key=True, index=True)
    source_type = Column(String(100), nullable=False)
    source_url = Column(Text, nullable=True)
    original_text = Column(Text, nullable=True)
    media_type = Column(String(100), nullable=True)
    media_file_id = Column(Text, nullable=True)

    analysis = Column(Text, nullable=True)
    idea = Column(Text, nullable=True)
    format = Column(String(255), nullable=True)
    hook = Column(Text, nullable=True)
    why_it_works = Column(Text, nullable=True)
    clinic_service = Column(Text, nullable=True)
    risks = Column(Text, nullable=True)
    recommended_topic = Column(String(500), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ContentAsset(Base):
    __tablename__ = "content_assets"

    id = Column(Integer, primary_key=True, index=True)
    source_type = Column(String(100), nullable=False)
    source_url = Column(Text, nullable=True)
    text_content = Column(Text, nullable=True)
    caption = Column(Text, nullable=True)
    media_type = Column(String(100), nullable=True)
    media_file_id = Column(Text, nullable=True)
    raw_meta = Column(Text, nullable=True)
    content_summary = Column(Text, nullable=True)
    analysis = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ContentPattern(Base):
    __tablename__ = "content_patterns"

    id = Column(Integer, primary_key=True, index=True)
    asset_id = Column(Integer, ForeignKey("content_assets.id"), nullable=True)
    hook_type = Column(String(500), nullable=True)
    emotion = Column(String(500), nullable=True)
    pain_point = Column(Text, nullable=True)
    format = Column(String(500), nullable=True)
    visual_style = Column(Text, nullable=True)
    humor_mechanic = Column(Text, nullable=True)
    engagement_reason = Column(Text, nullable=True)
    cta_type = Column(Text, nullable=True)
    content_mechanic = Column(Text, nullable=True)
    analysis = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ContentContext(Base):
    __tablename__ = "content_contexts"

    id = Column(Integer, primary_key=True, index=True)
    asset_id = Column(Integer, ForeignKey("content_assets.id"), nullable=True)
    cultural_context = Column(Text, nullable=True)
    timing_reason = Column(Text, nullable=True)
    audience = Column(Text, nullable=True)
    medical_applicability = Column(Text, nullable=True)
    adaptation_risks = Column(Text, nullable=True)
    clinic_ideas = Column(Text, nullable=True)
    analysis = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())



class ProjectState(Base):
    __tablename__ = "project_states"

    id = Column(Integer, primary_key=True, index=True)
    asset_id = Column(Integer, ForeignKey("content_assets.id"), nullable=True)
    state_version = Column(Integer, default=1, nullable=False)
    pipeline_stage = Column(String(100), default="intake", nullable=False)

    # JSON stored as TEXT for simple Railway/PostgreSQL compatibility without migrations.
    payload_json = Column(Text, nullable=True)
    continuation_package_json = Column(Text, nullable=True)
    stage_history_json = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())



class SemanticAnalysis(Base):
    __tablename__ = "semantic_analyses"

    id = Column(Integer, primary_key=True, index=True)
    asset_id = Column(Integer, ForeignKey("content_assets.id"), nullable=False, index=True)
    project_state_id = Column(Integer, ForeignKey("project_states.id"), nullable=False, index=True)
    schema_version = Column(String(100), nullable=True)

    # Canonical persisted JSON of the expensive semantic analysis stage.
    analysis_json = Column(Text, nullable=False)
    file_path = Column(Text, nullable=True)

    estimated_cost_usd = Column(String(50), nullable=True)
    input_tokens = Column(Integer, nullable=True)
    output_tokens = Column(Integer, nullable=True)
    total_tokens = Column(Integer, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SemanticAssetFile(Base):
    __tablename__ = "semantic_asset_files"

    id = Column(Integer, primary_key=True, index=True)
    asset_id = Column(Integer, ForeignKey("content_assets.id"), nullable=False, index=True)
    project_state_id = Column(Integer, ForeignKey("project_states.id"), nullable=True, index=True)

    # semantic_png / reconstruction / blueprint / manifest / analysis_export
    kind = Column(String(100), nullable=False, index=True)
    file_name = Column(Text, nullable=False)
    local_path = Column(Text, nullable=True)
    storage_backend = Column(String(50), nullable=False, default="local")
    storage_key = Column(Text, nullable=True)
    public_url = Column(Text, nullable=True)
    mime_type = Column(String(100), nullable=True)
    size_bytes = Column(Integer, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
