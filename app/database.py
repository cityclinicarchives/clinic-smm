from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import settings

engine = create_engine(settings.database_url)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_database_schema() -> None:
    """
    Мини-миграция для ранней стадии проекта.
    Нужна потому, что SQLAlchemy create_all() создает новые таблицы,
    но не добавляет новые колонки в уже существующие таблицы.
    Позже заменим это на Alembic migrations.
    """
    statements = [
        "ALTER TABLE content_posts ADD COLUMN IF NOT EXISTS headline VARCHAR(255)",
        "ALTER TABLE content_posts ADD COLUMN IF NOT EXISTS ai_model VARCHAR(100)",
        "ALTER TABLE content_posts ADD COLUMN IF NOT EXISTS image_path TEXT",
        "ALTER TABLE content_posts ADD COLUMN IF NOT EXISTS image_prompt TEXT",
        "ALTER TABLE content_posts ADD COLUMN IF NOT EXISTS image_model VARCHAR(100)",
        "ALTER TABLE content_posts ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE content_plan_items ADD COLUMN IF NOT EXISTS planned_date VARCHAR(20)",
        "ALTER TABLE content_plan_items ADD COLUMN IF NOT EXISTS topic VARCHAR(500)",
        "ALTER TABLE content_plan_items ADD COLUMN IF NOT EXISTS platform VARCHAR(50)",
        "ALTER TABLE content_plan_items ADD COLUMN IF NOT EXISTS status VARCHAR(50)",
        "ALTER TABLE content_plan_items ADD COLUMN IF NOT EXISTS source VARCHAR(100)",
        "ALTER TABLE content_plan_items ADD COLUMN IF NOT EXISTS created_post_id INTEGER",
        "ALTER TABLE content_plan_items ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE content_inspirations ADD COLUMN IF NOT EXISTS source_type VARCHAR(100)",
        "ALTER TABLE content_inspirations ADD COLUMN IF NOT EXISTS source_url TEXT",
        "ALTER TABLE content_inspirations ADD COLUMN IF NOT EXISTS original_text TEXT",
        "ALTER TABLE content_inspirations ADD COLUMN IF NOT EXISTS media_type VARCHAR(100)",
        "ALTER TABLE content_inspirations ADD COLUMN IF NOT EXISTS media_file_id TEXT",
        "ALTER TABLE content_inspirations ADD COLUMN IF NOT EXISTS analysis TEXT",
        "ALTER TABLE content_inspirations ADD COLUMN IF NOT EXISTS idea TEXT",
        "ALTER TABLE content_inspirations ADD COLUMN IF NOT EXISTS format VARCHAR(255)",
        "ALTER TABLE content_inspirations ADD COLUMN IF NOT EXISTS hook TEXT",
        "ALTER TABLE content_inspirations ADD COLUMN IF NOT EXISTS why_it_works TEXT",
        "ALTER TABLE content_inspirations ADD COLUMN IF NOT EXISTS clinic_service TEXT",
        "ALTER TABLE content_inspirations ADD COLUMN IF NOT EXISTS risks TEXT",
        "ALTER TABLE content_inspirations ADD COLUMN IF NOT EXISTS recommended_topic VARCHAR(500)",
        "ALTER TABLE content_inspirations ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE content_assets ADD COLUMN IF NOT EXISTS source_type VARCHAR(100)",
        "ALTER TABLE content_assets ADD COLUMN IF NOT EXISTS source_url TEXT",
        "ALTER TABLE content_assets ADD COLUMN IF NOT EXISTS text_content TEXT",
        "ALTER TABLE content_assets ADD COLUMN IF NOT EXISTS caption TEXT",
        "ALTER TABLE content_assets ADD COLUMN IF NOT EXISTS media_type VARCHAR(100)",
        "ALTER TABLE content_assets ADD COLUMN IF NOT EXISTS media_file_id TEXT",
        "ALTER TABLE content_assets ADD COLUMN IF NOT EXISTS raw_meta TEXT",
        "ALTER TABLE content_assets ADD COLUMN IF NOT EXISTS content_summary TEXT",
        "ALTER TABLE content_assets ADD COLUMN IF NOT EXISTS analysis TEXT",
        "ALTER TABLE content_assets ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE content_patterns ADD COLUMN IF NOT EXISTS asset_id INTEGER",
        "ALTER TABLE content_patterns ADD COLUMN IF NOT EXISTS hook_type VARCHAR(500)",
        "ALTER TABLE content_patterns ADD COLUMN IF NOT EXISTS emotion VARCHAR(500)",
        "ALTER TABLE content_patterns ADD COLUMN IF NOT EXISTS pain_point TEXT",
        "ALTER TABLE content_patterns ADD COLUMN IF NOT EXISTS format VARCHAR(500)",
        "ALTER TABLE content_patterns ADD COLUMN IF NOT EXISTS visual_style TEXT",
        "ALTER TABLE content_patterns ADD COLUMN IF NOT EXISTS humor_mechanic TEXT",
        "ALTER TABLE content_patterns ADD COLUMN IF NOT EXISTS engagement_reason TEXT",
        "ALTER TABLE content_patterns ADD COLUMN IF NOT EXISTS cta_type TEXT",
        "ALTER TABLE content_patterns ADD COLUMN IF NOT EXISTS content_mechanic TEXT",
        "ALTER TABLE content_patterns ADD COLUMN IF NOT EXISTS analysis TEXT",
        "ALTER TABLE content_patterns ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE content_contexts ADD COLUMN IF NOT EXISTS asset_id INTEGER",
        "ALTER TABLE content_contexts ADD COLUMN IF NOT EXISTS cultural_context TEXT",
        "ALTER TABLE content_contexts ADD COLUMN IF NOT EXISTS timing_reason TEXT",
        "ALTER TABLE content_contexts ADD COLUMN IF NOT EXISTS audience TEXT",
        "ALTER TABLE content_contexts ADD COLUMN IF NOT EXISTS medical_applicability TEXT",
        "ALTER TABLE content_contexts ADD COLUMN IF NOT EXISTS adaptation_risks TEXT",
        "ALTER TABLE content_contexts ADD COLUMN IF NOT EXISTS clinic_ideas TEXT",
        "ALTER TABLE content_contexts ADD COLUMN IF NOT EXISTS analysis TEXT",
        "ALTER TABLE content_contexts ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE",


        "ALTER TABLE project_states ADD COLUMN IF NOT EXISTS asset_id INTEGER",
        "ALTER TABLE project_states ADD COLUMN IF NOT EXISTS state_version INTEGER DEFAULT 1",
        "ALTER TABLE project_states ADD COLUMN IF NOT EXISTS pipeline_stage VARCHAR(100) DEFAULT 'intake'",
        "ALTER TABLE project_states ADD COLUMN IF NOT EXISTS payload_json TEXT",
        "ALTER TABLE project_states ADD COLUMN IF NOT EXISTS continuation_package_json TEXT",
        "ALTER TABLE project_states ADD COLUMN IF NOT EXISTS stage_history_json TEXT",
        "ALTER TABLE project_states ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE",
    ]

    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
