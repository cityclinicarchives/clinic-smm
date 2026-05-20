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
    ]

    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
