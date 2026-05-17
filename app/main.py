from fastapi import FastAPI

from app.config import settings
from app.database import Base, engine
from app.routers import content, health, telegram

Base.metadata.create_all(bind=engine)

app = FastAPI(
    title=settings.app_name,
    version="0.4.0",
)

app.include_router(health.router)
app.include_router(content.router)
app.include_router(telegram.router)


@app.get("/")
def root():
    return {
        "message": "Clinic SMM Manager is running",
        "docs": "/docs",
        "health": "/health",
        "posts": "/posts",
        "telegram_webhook": "/telegram/webhook",
    }
