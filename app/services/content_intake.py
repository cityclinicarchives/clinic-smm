import json
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.models import IntakeContentFile, IntakeContentItem
from app.services.telegram_bot import download_file_bytes


INTAKE_DIR = Path("storage/content_intake")
EXPORT_DIR = Path("storage/content_exports")


@dataclass
class IntakeInput:
    text: str | None = None
    caption: str | None = None
    media_type: str | None = None
    telegram_file_id: str | None = None
    mime_type: str | None = None
    file_name: str | None = None
    media_bytes: bytes | None = None
    raw_meta: dict[str, Any] | None = None
    chat_id: str | None = None
    message_id: str | None = None


def _safe_name(name: str) -> str:
    name = re.sub(r"[^A-Za-zА-Яа-я0-9_.-]+", "_", name or "file")
    return name[:120] or "file"


def _guess_ext(media_type: str | None, mime_type: str | None, file_name: str | None) -> str:
    if file_name and "." in file_name:
        return "." + file_name.rsplit(".", 1)[-1].lower()[:8]
    mt = (mime_type or "").lower()
    if "jpeg" in mt or "jpg" in mt:
        return ".jpg"
    if "png" in mt:
        return ".png"
    if "webp" in mt:
        return ".webp"
    if "mp4" in mt:
        return ".mp4"
    if "quicktime" in mt:
        return ".mov"
    if media_type == "photo":
        return ".jpg"
    if media_type == "video":
        return ".mp4"
    return ".bin"


def build_intake_input_from_telegram_message(message: dict) -> IntakeInput:
    text = message.get("text") or ""
    caption = message.get("caption") or ""
    chat_id = str(message.get("chat", {}).get("id") or "")
    message_id = str(message.get("message_id") or "")
    media_type = None
    file_id = None
    mime_type = None
    file_name = None
    media_bytes = None

    if message.get("photo"):
        media_type = "photo"
        file_id = message["photo"][-1].get("file_id")
        mime_type = "image/jpeg"
        file_name = f"telegram_photo_{message_id}.jpg"
    elif message.get("video"):
        media_type = "video"
        file_id = message["video"].get("file_id")
        mime_type = message["video"].get("mime_type") or "video/mp4"
        file_name = message["video"].get("file_name") or f"telegram_video_{message_id}.mp4"
    elif message.get("document"):
        media_type = "document"
        file_id = message["document"].get("file_id")
        mime_type = message["document"].get("mime_type")
        file_name = message["document"].get("file_name") or f"telegram_document_{message_id}"
    elif message.get("animation"):
        media_type = "animation"
        file_id = message["animation"].get("file_id")
        mime_type = message["animation"].get("mime_type") or "video/mp4"
        file_name = message["animation"].get("file_name") or f"telegram_animation_{message_id}.mp4"

    if file_id:
        try:
            media_bytes = download_file_bytes(file_id)
        except Exception:
            media_bytes = None

    raw_meta = {
        "telegram_date": message.get("date"),
        "forward_origin": message.get("forward_origin"),
        "forward_from_chat": message.get("forward_from_chat"),
        "media_group_id": message.get("media_group_id"),
    }
    return IntakeInput(
        text=text,
        caption=caption,
        media_type=media_type,
        telegram_file_id=file_id,
        mime_type=mime_type,
        file_name=file_name,
        media_bytes=media_bytes,
        raw_meta=raw_meta,
        chat_id=chat_id,
        message_id=message_id,
    )


def _detect_content_type(data: IntakeInput) -> str:
    if data.media_type == "video" or data.media_type == "animation":
        return "video"
    if data.media_type in {"photo", "document"} and (data.text or data.caption):
        return "text_plus_media"
    if data.media_type:
        return "media"
    return "text"


def _extract_title(text: str | None, caption: str | None) -> str:
    source = (text or caption or "").strip()
    for line in source.splitlines():
        line = line.strip(" #*•—-\t")
        if line:
            return line[:120]
    return "Новый материал"


def create_intake_item(db: Session, data: IntakeInput, rubric: str = "expert") -> IntakeContentItem:
    INTAKE_DIR.mkdir(parents=True, exist_ok=True)
    item = IntakeContentItem(
        title=_extract_title(data.text, data.caption),
        rubric=rubric,
        content_type=_detect_content_type(data),
        status="draft",
        source="telegram",
        source_chat_id=data.chat_id,
        source_message_id=data.message_id,
        text_content=data.text or None,
        caption=data.caption or None,
        raw_meta=json.dumps(data.raw_meta or {}, ensure_ascii=False),
    )
    db.add(item)
    db.commit()
    db.refresh(item)

    if data.telegram_file_id:
        item_dir = INTAKE_DIR / f"content-{item.id}"
        item_dir.mkdir(parents=True, exist_ok=True)
        ext = _guess_ext(data.media_type, data.mime_type, data.file_name)
        fname = _safe_name(data.file_name or f"asset_{item.id}{ext}")
        if "." not in fname:
            fname += ext
        path = item_dir / fname
        if data.media_bytes:
            path.write_bytes(data.media_bytes)
        f = IntakeContentFile(
            content_item_id=item.id,
            media_type=data.media_type,
            telegram_file_id=data.telegram_file_id,
            file_name=fname,
            local_path=str(path) if data.media_bytes else None,
            mime_type=data.mime_type,
            size_bytes=len(data.media_bytes) if data.media_bytes else None,
            raw_meta=json.dumps(data.raw_meta or {}, ensure_ascii=False),
        )
        db.add(f)
        db.commit()
    return item


def list_intake_items(db: Session, limit: int = 10) -> list[IntakeContentItem]:
    return db.query(IntakeContentItem).order_by(IntakeContentItem.id.desc()).limit(limit).all()


def get_intake_item(db: Session, item_id: int) -> IntakeContentItem | None:
    return db.query(IntakeContentItem).filter(IntakeContentItem.id == item_id).first()


def list_intake_files(db: Session, item_id: int) -> list[IntakeContentFile]:
    return db.query(IntakeContentFile).filter(IntakeContentFile.content_item_id == item_id).order_by(IntakeContentFile.id.asc()).all()


def format_intake_card(item: IntakeContentItem, files: list[IntakeContentFile] | None = None) -> str:
    files = files or []
    text = (item.text_content or item.caption or "").strip()
    if len(text) > 800:
        text = text[:800].rstrip() + "…"
    return (
        f"📥 <b>Контент #{item.id}</b> — <code>{item.status}</code>\n"
        f"Рубрика: <b>{item.rubric}</b>\n"
        f"Тип: <b>{item.content_type}</b>\n"
        f"Заголовок: <b>{item.title or 'без названия'}</b>\n"
        f"Файлов: <b>{len(files)}</b>\n\n"
        f"{text or 'Текст не добавлен.'}"
    )


def export_intake_item_zip(db: Session, item_id: int) -> Path:
    item = get_intake_item(db, item_id)
    if not item:
        raise ValueError(f"Контент #{item_id} не найден")
    files = list_intake_files(db, item_id)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    zip_path = EXPORT_DIR / f"content-{item.id}-source-{ts}.zip"
    readme = [
        f"Контент #{item.id}",
        f"Статус: {item.status}",
        f"Рубрика: {item.rubric}",
        f"Тип: {item.content_type}",
        f"Заголовок: {item.title or ''}",
        "",
        "Текст:",
        item.text_content or item.caption or "",
        "",
        "Файлы:",
    ]
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README_что_куда_копировать.txt", "\n".join(readme))
        zf.writestr("source/text.txt", item.text_content or item.caption or "")
        zf.writestr("source/meta.json", json.dumps({
            "id": item.id,
            "title": item.title,
            "rubric": item.rubric,
            "content_type": item.content_type,
            "status": item.status,
            "created_at": str(item.created_at),
        }, ensure_ascii=False, indent=2))
        for f in files:
            if f.local_path and Path(f.local_path).is_file():
                zf.write(f.local_path, arcname=f"source/files/{Path(f.local_path).name}")
    return zip_path
