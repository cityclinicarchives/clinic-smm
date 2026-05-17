from pydantic import BaseModel, Field


class ContentPostCreate(BaseModel):
    title: str = Field(..., min_length=3, max_length=255)
    platform: str = Field(..., min_length=2, max_length=50)
    text: str | None = None


class ContentPostUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=3, max_length=255)
    platform: str | None = Field(default=None, min_length=2, max_length=50)
    status: str | None = Field(default=None, min_length=2, max_length=50)
    text: str | None = None


class ContentPostOut(BaseModel):
    id: int
    title: str
    platform: str
    status: str
    text: str | None = None

    class Config:
        from_attributes = True
