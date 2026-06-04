import json
import re
from dataclasses import dataclass

from openai import OpenAI

from app.config import settings
from app.models import ContentPost


@dataclass
class VisualConcept:
    emotion: str
    visual_hook: str
    humor_level: str
    format_type: str
    scene: str
    composition: str
    style: str
    key_objects: str
    forbidden_generic: str
    title_placement: str
    safety_notes: str

    def to_prompt_block(self) -> str:
        return f"""
Визуальная концепция:
- Главная эмоция: {self.emotion}
- Visual hook: {self.visual_hook}
- Уровень юмора: {self.humor_level}
- Формат: {self.format_type}
- Сцена: {self.scene}
- Композиция: {self.composition}
- Стиль: {self.style}
- Ключевые объекты: {self.key_objects}
- Где разместить заголовок: {self.title_placement}
- Что НЕ делать: {self.forbidden_generic}
- Медицинская безопасность: {self.safety_notes}
""".strip()


SYSTEM_PROMPT = """
Ты — creative director медицинского SMM для частной клиники в Москве.
Твоя задача — придумать НЕ банальную медицинскую стоковую картинку, а сильную визуальную идею для поста.

Главное правило:
НЕ используй шаблон "врач и пациент сидят в кабинете", если это не является действительно лучшей идеей.

Ты должен искать:
- визуальную метафору;
- юмор, если он уместен и безопасен;
- контраст;
- предметную сцену;
- инфографику;
- чек-лист;
- символический сюжет;
- понятный visual hook, который остановит скролл.

Медицинская безопасность:
- без крови, операций, пугающих процедур;
- без демонстрации конкретного диагноза по внешности;
- без унижения пациента;
- без обещаний гарантированного результата;
- юмор допустим только мягкий, не обидный, не токсичный.

Верни строго JSON без markdown.
""".strip()


USER_TEMPLATE = """
Создай визуальную концепцию для SMM-картинки медицинской клиники.

Тема поста:
{topic}

Заголовок:
{headline}

Фрагмент текста поста:
{text}

Нужно выбрать один лучший визуальный подход.

Примеры хорошего мышления:
- "Основы правильного питания для спортсменов" → спортсмен смотрит на гигантский бургер как на спортивный снаряд; юмористический контраст, но без фастфуд-рекламы.
- "Подготовка к анализам" → аккуратный чек-лист, пробирки, часы, стакан воды, запретные продукты как перечеркнутые пиктограммы.
- "Прививка от клещевого энцефалита" → человек спокойно гуляет на природе, а стилизованные клещи отлетают от него с красными крестиками-защитой.
- "Дефицит витаминов" → батарейка организма почти разряжена, рядом мягкие медицинские символы.

Верни JSON со строковыми полями:
{{
  "emotion": "...",
  "visual_hook": "...",
  "humor_level": "none / light / medium",
  "format_type": "photo / infographic / poster / comic / collage / symbolic_scene / checklist",
  "scene": "...",
  "composition": "...",
  "style": "...",
  "key_objects": "...",
  "forbidden_generic": "...",
  "title_placement": "...",
  "safety_notes": "..."
}}
""".strip()


FALLBACK_CONCEPT = VisualConcept(
    emotion="польза + легкое узнавание себя",
    visual_hook="предметная визуальная метафора по теме поста",
    humor_level="light",
    format_type="poster",
    scene="яркая медицинская SMM-сцена с символами темы, без банального приема врача в кабинете",
    composition="главный объект или метафора в центре, крупный заголовок в отдельном дизайнерском блоке",
    style="современная медицинская рекламная карточка, clean design, мягкие синие и зеленые акценты",
    key_objects="объекты, напрямую связанные с темой поста",
    forbidden_generic="не делать одинаковую сцену врач+пациент за столом; не использовать скучное stock-photo",
    title_placement="крупный белый или светлый скругленный блок в нижней или центральной части изображения",
    safety_notes="без пугающих медицинских сцен и без обещаний результата",
)


def _get_client() -> OpenAI:
    if not settings.openai_api_key or settings.openai_api_key.startswith("sk-your"):
        raise RuntimeError("OPENAI_API_KEY не задан. Добавьте OPENAI_API_KEY в Railway Variables.")
    return OpenAI(api_key=settings.openai_api_key)


def _extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"```$", "", text).strip()
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if match:
        text = match.group(0)
    return json.loads(text)


def generate_visual_concept(post: ContentPost, custom_instruction: str | None = None) -> VisualConcept:
    """
    Создает креативную визуальную концепцию до генерации изображения.
    Это нужно, чтобы модель не уходила в однотипные "врач + пациент + кабинет".
    """
    headline = (post.headline or post.title or "").strip()
    prompt = USER_TEMPLATE.format(
        topic=(post.title or "")[:300],
        headline=headline[:160],
        text=(post.text or "")[:1200],
    )
    if custom_instruction:
        prompt += f"\n\nДополнительная инструкция пользователя:\n{custom_instruction.strip()}"

    try:
        client = _get_client()
        response = client.responses.create(
            model=settings.openai_model,
            input=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        data = _extract_json(response.output_text)
        return VisualConcept(
            emotion=str(data.get("emotion") or FALLBACK_CONCEPT.emotion),
            visual_hook=str(data.get("visual_hook") or FALLBACK_CONCEPT.visual_hook),
            humor_level=str(data.get("humor_level") or FALLBACK_CONCEPT.humor_level),
            format_type=str(data.get("format_type") or FALLBACK_CONCEPT.format_type),
            scene=str(data.get("scene") or FALLBACK_CONCEPT.scene),
            composition=str(data.get("composition") or FALLBACK_CONCEPT.composition),
            style=str(data.get("style") or FALLBACK_CONCEPT.style),
            key_objects=str(data.get("key_objects") or FALLBACK_CONCEPT.key_objects),
            forbidden_generic=str(data.get("forbidden_generic") or FALLBACK_CONCEPT.forbidden_generic),
            title_placement=str(data.get("title_placement") or FALLBACK_CONCEPT.title_placement),
            safety_notes=str(data.get("safety_notes") or FALLBACK_CONCEPT.safety_notes),
        )
    except Exception:
        return FALLBACK_CONCEPT
