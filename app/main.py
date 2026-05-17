from fastapi import FastAPI

from app.config import settings
from app.database import Base, engine
from app.routers import content, health

Base.metadata.create_all(bind=engine)

app = FastAPI(
    title=settings.app_name,
    version="0.3.0",
)

app.include_router(health.router)
app.include_router(content.router)


@app.get("/")
def root():
    return {
        "message": "Clinic SMM Manager is running",
        "docs": "/docs",
        "health": "/health",
        "posts": "/posts",
    }
