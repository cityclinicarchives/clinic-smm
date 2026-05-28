import base64
import json
import re
from dataclasses import dataclass
from typing import Any

from openai import OpenAI

from app.config import settings
from app.models import ContentAsset, ContentContext, ContentPattern
from app.prompts.infographic import INFOGRAPHIC_RECONSTRUCTION_SYSTEM_PROMPT, INFOGRAPHIC_RECONSTRUCTION_USER_TEMPLATE
from app.prompts.meme import MEME_RECONSTRUCTION_SYSTEM_PROMPT, MEME_RECONSTRUCTION_USER_TEMPLATE
from app.prompts.post import POST_RECONSTRUCTION_SYSTEM_PROMPT, POST_RECONSTRUCTION_USER_TEMPLATE
from app.prompts.video import VIDEO_RECONSTRUCTION_SYSTEM_PROMPT, VIDEO_RECONSTRUCTION_USER_TEMPLATE
from app.prompts.visual import VISUAL_RECONSTRUCTION_SYSTEM_PROMPT, VISUAL_RECONSTRUCTION_USER_TEMPLATE
from app.prompts.router import ROUTER_SYSTEM_PROMPT, ROUTER_USER_TEMPLATE
from app.services.telegram_bot import download_file_bytes


@dataclass
class PromptChoice:
    asset_type: str
    recommended_prompt: str
    recommended_pipeline: str
    reason: str
    system_prompt: str
    user_template: str


def _cut(text: str | None, limit: int = 4000) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n...обрезано"


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = (text or "").strip()
    cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if match:
        cleaned = match.group(0)
    return json.loads(cleaned)


def _get_client() -> OpenAI:
    return OpenAI(api_key=settings.openai_api_key)


def _has_image(asset: ContentAsset) -> bool:
    return bool(asset.media_file_id and (asset.media_type in {"photo", "document"}))


def _heuristic_prompt_name(asset: ContentAsset, pattern: ContentPattern | None = None) -> str:
    text = "\n".join([
        asset.text_content or "",
        asset.caption or "",
        asset.content_summary or "",
        asset.analysis or "",
        pattern.format if pattern else "",
        pattern.humor_mechanic if pattern else "",
        pattern.visual_style if pattern else "",
    ]).lower()

    if any(x in text for x in ["инфограф", "чек-лист", "таблица", "памятка", "расшифров", "grid", "сетка"]):
        return "infographic"
    if any(x in text for x in ["мем", "прикол", "юмор", "смеш", "ирони", "сарказ", "абсурд"]):
        return "meme"
    if any(x in text for x in ["shorts", "reels", "tiktok", "видео", "ролик", "кадр", "монтаж"]):
        return "video"
    if _has_image(asset) and not (asset.text_content or asset.caption):
        return "visual"
    return "post"


def _prompts_for(name: str) -> tuple[str, str]:
    if name == "infographic":
        return INFOGRAPHIC_RECONSTRUCTION_SYSTEM_PROMPT, INFOGRAPHIC_RECONSTRUCTION_USER_TEMPLATE
    if name == "meme":
        return MEME_RECONSTRUCTION_SYSTEM_PROMPT, MEME_RECONSTRUCTION_USER_TEMPLATE
    if name == "video":
        return VIDEO_RECONSTRUCTION_SYSTEM_PROMPT, VIDEO_RECONSTRUCTION_USER_TEMPLATE
    if name == "visual":
        return VISUAL_RECONSTRUCTION_SYSTEM_PROMPT, VISUAL_RECONSTRUCTION_USER_TEMPLATE
    return POST_RECONSTRUCTION_SYSTEM_PROMPT, POST_RECONSTRUCTION_USER_TEMPLATE


def classify_asset_for_reconstruction(asset: ContentAsset, pattern: ContentPattern | None = None) -> dict[str, Any]:
    """Короткая AI-классификация. Если API/картинка недоступны — fallback на эвристику."""
    fallback = _heuristic_prompt_name(asset, pattern)
    default = {
        "asset_type": fallback,
        "content_mode": "mixed",
        "humor_level": "none",
        "needs_medical_audit": True,
        "needs_reference_image": _has_image(asset),
        "recommended_prompt": fallback,
        "recommended_pipeline": "reference_based_reconstruction" if _has_image(asset) else "text_reconstruction",
        "reason": "heuristic fallback",
    }
    try:
        client = _get_client()
        prompt = ROUTER_USER_TEMPLATE.format(
            source_type=asset.source_type or "—",
            media_type=asset.media_type or "—",
            text=_cut(asset.text_content, 1500),
            caption=_cut(asset.caption, 1500),
            analysis=_cut(asset.analysis, 3000),
        )
        content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
        if _has_image(asset):
            try:
                image_bytes = download_file_bytes(asset.media_file_id)  # type: ignore[arg-type]
                encoded = base64.b64encode(image_bytes).decode("utf-8")
                content.append({"type": "input_image", "image_url": f"data:image/jpeg;base64,{encoded}"})
            except Exception:
                pass
        response = client.responses.create(
            model=settings.openai_model,
            input=[
                {"role": "system", "content": ROUTER_SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
        )
        data = _extract_json(response.output_text)
        if not data.get("recommended_prompt"):
            data["recommended_prompt"] = fallback
        return {**default, **data}
    except Exception:
        return default


def select_reconstruction_prompt(asset: ContentAsset, pattern: ContentPattern | None = None) -> PromptChoice:
    data = classify_asset_for_reconstruction(asset, pattern)
    name = str(data.get("recommended_prompt") or "post").lower()
    if name not in {"infographic", "meme", "post", "video", "visual"}:
        name = _heuristic_prompt_name(asset, pattern)
    system_prompt, user_template = _prompts_for(name)
    return PromptChoice(
        asset_type=str(data.get("asset_type") or name),
        recommended_prompt=name,
        recommended_pipeline=str(data.get("recommended_pipeline") or "text_reconstruction"),
        reason=str(data.get("reason") or ""),
        system_prompt=system_prompt,
        user_template=user_template,
    )
