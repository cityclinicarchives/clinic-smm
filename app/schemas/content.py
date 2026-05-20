from pydantic import BaseModel, Field


class PostCreateRequest(BaseModel):
    title: str = Field(..., examples=["Анализы на витамин D"])
    platform: str = Field(..., examples=["telegram"])
    text: str | None = Field(default=None, examples=["Текст черновика"])


class GeneratePostRequest(BaseModel):
    topic: str = Field(..., examples=["Как понять, что организму не хватает витамина D"])
    platform: str = Field(default="telegram", examples=["telegram"])
    service_offer: str | None = Field(
        default=None,
        examples=["В клинике можно сдать анализ на витамин D и получить консультацию врача."],
    )
    with_image: bool = Field(
        default=False,
        description="Если true — сразу сгенерировать изображение к посту.",
    )


class ManualEditRequest(BaseModel):
    text: str = Field(..., examples=["Полный исправленный текст поста"])


class AiRewriteRequest(BaseModel):
    instruction: str = Field(
        ...,
        examples=["Сделай текст короче, убери повторы и добавь мягкий призыв записаться."],
    )


class ImageGenerateRequest(BaseModel):
    instruction: str | None = Field(
        default=None,
        examples=["Сделай изображение без врача, только пациент на ресепшене."],
    )


class PostResponse(BaseModel):
    id: int
    title: str
    headline: str | None = None
    platform: str
    status: str
    text: str | None = None
    ai_model: str | None = None
    image_path: str | None = None
    image_prompt: str | None = None
    image_model: str | None = None

    class Config:
        from_attributes = True


class GenerateWeekPlanRequest(BaseModel):
    platform: str = Field(default="telegram", examples=["telegram"])


class CreateFromPlanRequest(BaseModel):
    with_image: bool = Field(default=True, description="Если true — сразу создать изображение к посту.")


class ContentPlanItemResponse(BaseModel):
    id: int
    planned_date: str | None = None
    topic: str
    platform: str
    status: str
    source: str | None = None
    created_post_id: int | None = None

    class Config:
        from_attributes = True


class AnalyzeUrlRequest(BaseModel):
    url: str = Field(..., examples=["https://example.com/post"])


class ContentInspirationResponse(BaseModel):
    id: int
    source_type: str
    source_url: str | None = None
    original_text: str | None = None
    media_type: str | None = None
    analysis: str | None = None
    idea: str | None = None
    format: str | None = None
    hook: str | None = None
    why_it_works: str | None = None
    clinic_service: str | None = None
    risks: str | None = None
    recommended_topic: str | None = None

    class Config:
        from_attributes = True
