from fastapi import FastAPI

from app.config import settings
from app.database import Base, engine
from app.routers.content import router as content_router
from app.routers.health import router as health_router

Base.metadata.create_all(bind=engine)

app = FastAPI(
    title=settings.app_name,
    version="0.2.0",
)


@app.get("/")
def root():
    return {
        "message": "Clinic SMM Manager is running",
        "docs": "/docs",
        "health": "/health",
        "posts": "/posts",
    }


app.include_router(health_router)
app.include_router(content_router)
