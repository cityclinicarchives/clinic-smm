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


class ManualEditRequest(BaseModel):
    text: str = Field(..., examples=["Полный исправленный текст поста"])


class AiRewriteRequest(BaseModel):
    instruction: str = Field(
        ...,
        examples=["Сделай текст короче, убери повторы и добавь мягкий призыв записаться."],
    )


class PostResponse(BaseModel):
    id: int
    title: str
    platform: str
    status: str
    text: str | None = None
    ai_model: str | None = None

    class Config:
        from_attributes = True
