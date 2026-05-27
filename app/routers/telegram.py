import html

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.services.content_research import (
    InspirationAnalyzeError,
    build_inspiration_input_from_telegram_message,
    create_inspiration,
    fetch_url_preview,
    format_inspiration_card,
    generate_week_plan_from_inspirations,
    list_inspirations,
)
from app.services.content_plan import (
    PlanItemNotFoundError,
    PlanItemStatusError,
    create_post_from_plan_item,
    generate_week_plan,
    list_plan_items,
)
from app.services.pattern_analyzer import (
    analyze_and_save_asset,
    build_asset_input_from_telegram_message,
    format_asset_card,
    format_pattern_card,
    generate_post_from_pattern,
    list_assets,
    list_patterns,
)
from app.services.reconstruction_engine import (
    create_post_from_reconstruction,
    create_crop_preview_for_reconstruction,
    format_reconstruction_card,
    list_reconstructions,
    reconstruct_asset_with_ai,
    reconstruction_needs_crop_preview,
)
from app.services.post_manager import (
    PostNotFoundError,
    PostStatusError,
    approve_post,
    create_generated_post,
    edit_post_manually,
    generate_or_replace_image,
    get_post_or_raise,
    list_recent_posts,
    publish_post,
    reject_post,
    rewrite_post_with_ai,
    select_post_image_version,
)
from app.services.telegram_bot import (
    TelegramBotError,
    answer_callback_query,
    get_webhook_info,
    send_message,
    send_photo,
    set_webhook,
)

router = APIRouter(prefix="/telegram", tags=["telegram"])

# Простое состояние диалога в памяти процесса.
# Для одного админа и одного Railway-инстанса этого достаточно на текущем этапе.
# Позже перенесем в PostgreSQL/Redis.
PENDING_ACTIONS: dict[str, dict] = {}


def _is_admin(chat_id: int | str) -> bool:
    if not settings.admin_telegram_id:
        return True
    return str(chat_id) == str(settings.admin_telegram_id)


def _safe(text: str | None) -> str:
    return html.escape(text or "")


def _shorten(text: str, limit: int = 3500) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n\n...текст обрезан, полный пост сохранен в базе."


def _post_keyboard(post_id: int) -> dict:
    """Кнопки под конкретным постом."""
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Одобрить", "callback_data": f"approve:{post_id}"},
                {"text": "🚀 Опубликовать", "callback_data": f"publish:{post_id}"},
            ],
            [
                {"text": "✏️ Редактировать вручную", "callback_data": f"edit_manual:{post_id}"},
            ],
            [
                {"text": "🤖 ИИ-редактирование", "callback_data": f"rewrite_ai:{post_id}"},
                {"text": "🖼 Картинка", "callback_data": f"image:{post_id}"},
            ],
            [
                {"text": "❌ Отклонить", "callback_data": f"reject:{post_id}"},
            ],
        ]
    }


def _plan_keyboard(item_id: int) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "📝 Создать пост", "callback_data": f"plan_create:{item_id}"},
                {"text": "🖼 Создать пост + картинку", "callback_data": f"plan_create_full:{item_id}"},
            ]
        ]
    }


def _pattern_keyboard(pattern_id: int) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "🧬 Создать пост", "callback_data": f"pattern_create:{pattern_id}"},
                {"text": "🧬🖼 Пост + картинка", "callback_data": f"pattern_create_full:{pattern_id}"},
            ]
        ]
    }


def _asset_keyboard(asset_id: int) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "🧠 Реконструировать", "callback_data": f"asset_reconstruct:{asset_id}"},
            ]
        ]
    }


def _reconstruction_keyboard(reconstruction_id: int) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "📝 Создать пост", "callback_data": f"reconstruction_create:{reconstruction_id}"},
                {"text": "🖼 Пост + картинка", "callback_data": f"reconstruction_create_full:{reconstruction_id}"},
            ]
        ]
    }


def _reconstruction_preview_keyboard(reconstruction_id: int) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Собрать инфографику", "callback_data": f"reconstruction_create_full_confirm:{reconstruction_id}"},
            ],
            [
                {"text": "🧠 Реконструировать заново", "callback_data": f"asset_reconstruct_again_from_reconstruction:{reconstruction_id}"},
                {"text": "📝 Только пост", "callback_data": f"reconstruction_create:{reconstruction_id}"},
            ],
        ]
    }


def _image_version_keyboard(post_id: int) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Взять дизайн-версию", "callback_data": f"select_image_polished:{post_id}"},
                {"text": "✅ Взять точный черновик", "callback_data": f"select_image_draft:{post_id}"},
            ],
            [
                {"text": "✅ Одобрить пост", "callback_data": f"approve:{post_id}"},
                {"text": "🖼 Перегенерировать картинку", "callback_data": f"image:{post_id}"},
            ],
        ]
    }


def _extract_v30_image_paths(image_prompt: str | None) -> dict[str, str]:
    paths: dict[str, str] = {}
    for line in (image_prompt or "").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key in {"technical_draft", "polished_image", "selected_image", "final_choice"}:
            paths[key] = value
    return paths


def _maybe_send_v30_image_choice(chat_id: int | str, post) -> None:
    paths = _extract_v30_image_paths(getattr(post, "image_prompt", None))
    draft = paths.get("technical_draft")
    polished = paths.get("polished_image")
    if not (draft and polished):
        return
    selected = paths.get("final_choice", "draft")
    send_message(
        chat_id,
        "🧪 V30 проверила две версии инфографики. "
        f"Сейчас выбрана: {'дизайн-версия' if selected == 'polished' else 'точный черновик'}.\n\n"
        "Ниже отправляю обе версии. Вы можете выбрать, какую оставить у поста.",
    )
    send_photo(chat_id, draft, caption="Точный технический черновик")
    send_photo(chat_id, polished, caption="Дизайн-версия после AI-polish", reply_markup=_image_version_keyboard(post.id))


def _plan_item_card(item) -> str:
    post_part = f"Пост: #{item.created_post_id}" if item.created_post_id else "Пост: еще не создан"
    return "\n".join([
        f"<b>Пункт плана #{item.id}</b>",
        f"Дата: {_safe(item.planned_date or '—')}",
        f"Платформа: {_safe(item.platform)}",
        f"Статус: {_safe(item.status)}",
        post_part,
        "",
        f"<b>Тема:</b> {_safe(item.topic)}",
    ])


def _post_card(post, include_text: bool = False) -> str:
    lines = [
        f"<b>Пост #{post.id}</b>",
        f"Тема: {_safe(post.title)}",
        f"Заголовок: {_safe(getattr(post, 'headline', None) or '—')}",
        f"Платформа: {_safe(post.platform)}",
        f"Статус: {_safe(post.status)}",
        f"Изображение: {'есть' if getattr(post, 'image_path', None) else 'нет'}",
    ]
    if include_text:
        lines.append("")
        lines.append(_safe(_shorten(post.text or "Текст пустой.")))
        lines.append("")
        lines.append("Для действий используйте кнопки под сообщением.")
    return "\n".join(lines)


def _parse_id_and_payload(text: str, command: str) -> tuple[int | None, str]:
    rest = text.replace(command, "", 1).strip()
    if not rest:
        return None, ""
    parts = rest.split(maxsplit=1)
    try:
        post_id = int(parts[0])
    except ValueError:
        return None, rest
    payload = parts[1].strip() if len(parts) > 1 else ""
    return post_id, payload


def _send_post(chat_id: int | str, post, include_text: bool = True) -> None:
    send_message(chat_id, _post_card(post, include_text=include_text), reply_markup=_post_keyboard(post.id))


def _set_pending(chat_id: int | str, mode: str, post_id: int) -> None:
    PENDING_ACTIONS[str(chat_id)] = {"mode": mode, "post_id": post_id}


def _set_pending_mode(chat_id: int | str, mode: str) -> None:
    PENDING_ACTIONS[str(chat_id)] = {"mode": mode}


def _clear_pending(chat_id: int | str) -> None:
    PENDING_ACTIONS.pop(str(chat_id), None)


def _get_pending(chat_id: int | str) -> dict | None:
    return PENDING_ACTIONS.get(str(chat_id))


def _set_crop_preview_confirmed(chat_id: int | str, reconstruction_id: int) -> None:
    PENDING_ACTIONS[str(chat_id)] = {"mode": "crop_preview_confirmed", "reconstruction_id": reconstruction_id}


def _is_crop_preview_confirmed(chat_id: int | str, reconstruction_id: int) -> bool:
    pending = PENDING_ACTIONS.get(str(chat_id)) or {}
    return pending.get("mode") == "crop_preview_confirmed" and int(pending.get("reconstruction_id") or 0) == int(reconstruction_id)


def _handle_pending_text(chat_id: int | str, text: str, db: Session) -> bool:
    """Обрабатывает ответ пользователя после кнопок редактирования.

    Возвращает True, если сообщение было обработано как продолжение диалога.
    """
    pending = _get_pending(chat_id)
    if not pending:
        return False

    # Команды не считаем ответом на редактирование, чтобы пользователь мог отменить/выполнить другую команду.
    if text.startswith("/"):
        if text == "/cancel":
            _clear_pending(chat_id)
            send_message(chat_id, "Редактирование отменено.")
            return True
        return False

    mode = pending.get("mode")
    post_id = int(pending.get("post_id"))

    try:
        if mode == "manual_edit":
            post = edit_post_manually(db, post_id, text)
            _clear_pending(chat_id)
            send_message(
                chat_id,
                "✅ Текст поста заменен вручную. Ранее созданная картинка сохранена.",
            )
            _send_post(chat_id, post, include_text=True)
            return True

        if mode == "ai_rewrite":
            send_message(chat_id, "🤖 Отправляю правки в ИИ.")
            post = rewrite_post_with_ai(db, post_id, text)
            _clear_pending(chat_id)
            send_message(
                chat_id,
                "✅ Пост отредактирован через ИИ. Ранее созданная картинка сохранена.",
            )
            _send_post(chat_id, post, include_text=True)
            return True

    except Exception as exc:
        _clear_pending(chat_id)
        send_message(chat_id, f"Ошибка редактирования:\n{_safe(str(exc))}")
        return True

    _clear_pending(chat_id)
    return False


def _handle_pending_asset(chat_id: int | str, message: dict, db: Session) -> bool:
    pending = _get_pending(chat_id)
    if not pending or pending.get("mode") != "asset":
        return False

    text = (message.get("text") or "").strip()
    if text.startswith("/"):
        if text == "/cancel":
            _clear_pending(chat_id)
            send_message(chat_id, "Анализ контент-исходника отменен.")
            return True
        return False

    try:
        send_message(chat_id, "Анализирую материал как: контент + паттерн + контекст. Если есть картинка, учту визуал.")
        data = build_asset_input_from_telegram_message(message)
        asset, pattern, _context = analyze_and_save_asset(db, data)
        _clear_pending(chat_id)
        send_message(chat_id, "✅ Контент-исходник сохранен. Следующий шаг — реконструкция по новой v23-архитектуре: router → specialized prompt → component blueprint → reference reconstruction.")
        send_message(chat_id, format_asset_card(asset), reply_markup=_asset_keyboard(asset.id))
        return True
    except Exception as exc:
        _clear_pending(chat_id)
        send_message(chat_id, f"Ошибка анализа контент-исходника:\n{_safe(str(exc))}")
        return True


def _handle_pending_inspiration(chat_id: int | str, message: dict, db: Session) -> bool:
    pending = _get_pending(chat_id)
    if not pending or pending.get("mode") != "inspire":
        return False

    text = (message.get("text") or "").strip()
    if text.startswith("/"):
        if text == "/cancel":
            _clear_pending(chat_id)
            send_message(chat_id, "Добавление вдохновения отменено.")
            return True
        return False

    try:
        send_message(chat_id, "Анализирую материал. Если в сообщении есть изображение, попробую учесть и визуал.")
        data = build_inspiration_input_from_telegram_message(message)
        if not (data.text or data.caption or data.media_file_id or data.source_url):
            send_message(chat_id, "Не вижу текста, caption, ссылки или медиа. Перешлите пост, фото с подписью или текст.")
            return True

        inspiration = create_inspiration(db, data)
        _clear_pending(chat_id)
        send_message(chat_id, "✅ Карточка вдохновения создана.")
        send_message(chat_id, format_inspiration_card(inspiration))
        return True
    except Exception as exc:
        _clear_pending(chat_id)
        send_message(chat_id, f"Ошибка анализа вдохновения:\n{_safe(str(exc))}")
        return True


def _handle_callback(callback_query: dict, db: Session) -> dict:
    callback_query_id = callback_query.get("id")
    message = callback_query.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    data = callback_query.get("data") or ""

    if callback_query_id:
        # Telegram requires callback queries to be answered very quickly.
        # If Railway/OpenAI work is slow or Telegram retries an old callback,
        # answerCallbackQuery may return: query is too old / invalid.
        # This must not abort the webhook handler or crop generation.
        try:
            answer_callback_query(callback_query_id)
        except TelegramBotError:
            pass

    if not chat_id:
        return {"ok": True}

    if not _is_admin(chat_id):
        send_message(chat_id, "Нет доступа к этому боту.")
        return {"ok": True}

    try:
        action, raw_post_id = data.split(":", 1)
        post_id = int(raw_post_id)
    except Exception:
        send_message(chat_id, "Не удалось распознать кнопку.")
        return {"ok": True}

    try:
        if action == "approve":
            _clear_pending(chat_id)
            post = approve_post(db, post_id)
            send_message(chat_id, f"✅ Пост #{post.id} одобрен.", reply_markup=_post_keyboard(post.id))

        elif action == "publish":
            _clear_pending(chat_id)
            post = publish_post(db, post_id)
            send_message(chat_id, f"🚀 Пост #{post.id} опубликован в тестовую группу. Статус: published")

        elif action == "reject":
            _clear_pending(chat_id)
            post = reject_post(db, post_id)
            send_message(chat_id, f"❌ Пост #{post.id} отклонен. Статус: rejected", reply_markup=_post_keyboard(post.id))

        elif action == "image":
            _clear_pending(chat_id)
            send_message(chat_id, f"Генерирую изображение для поста #{post_id}.")
            post = generate_or_replace_image(db, post_id, None)
            send_message(chat_id, f"🖼 Изображение для поста #{post.id} создано/обновлено.", reply_markup=_post_keyboard(post.id))
            if post.image_path:
                send_photo(chat_id, post.image_path, caption=f"Изображение к посту #{post.id}", reply_markup=_post_keyboard(post.id))

        elif action == "edit_manual":
            post = get_post_or_raise(db, post_id)
            _set_pending(chat_id, "manual_edit", post.id)
            send_message(
                chat_id,
                f"✏️ Ручное редактирование поста #{post.id}.\n\n"
                "Введите полную исправленную версию текста одним следующим сообщением.\n\n"
                "Картинка поста будет сохранена. Для отмены напишите /cancel.",
            )

        elif action == "rewrite_ai":
            post = get_post_or_raise(db, post_id)
            _set_pending(chat_id, "ai_rewrite", post.id)
            send_message(
                chat_id,
                f"🤖 ИИ-редактирование поста #{post.id}.\n\n"
                "Напишите, что нужно исправить. Например:\n"
                "<i>Сделай текст короче, убери повторы и добавь мягкий призыв записаться.</i>\n\n"
                "Картинка поста будет сохранена. Для отмены напишите /cancel.",
            )

        elif action == "plan_create":
            _clear_pending(chat_id)
            item, post = create_post_from_plan_item(db, post_id, with_image=False)
            send_message(chat_id, f"📝 По пункту плана #{item.id} создан пост #{post.id}.")
            _send_post(chat_id, post, include_text=True)

        elif action == "plan_create_full":
            _clear_pending(chat_id)
            send_message(chat_id, f"Создаю пост и изображение по пункту плана #{post_id}.")
            item, post = create_post_from_plan_item(db, post_id, with_image=True)
            send_message(chat_id, f"📝🖼 По пункту плана #{item.id} создан пост #{post.id} с изображением.")
            _send_post(chat_id, post, include_text=True)
            if post.image_path:
                send_photo(chat_id, post.image_path, caption=f"Изображение к посту #{post.id}", reply_markup=_post_keyboard(post.id))

        elif action == "pattern_create":
            _clear_pending(chat_id)
            post = generate_post_from_pattern(db, post_id, with_image=False)
            send_message(chat_id, f"🧬 По паттерну #{post_id} создан пост #{post.id}.")
            _send_post(chat_id, post, include_text=True)

        elif action == "pattern_create_full":
            _clear_pending(chat_id)
            send_message(chat_id, f"Создаю пост и изображение по паттерну #{post_id}.")
            post = generate_post_from_pattern(db, post_id, with_image=True)
            send_message(chat_id, f"🧬🖼 По паттерну #{post_id} создан пост #{post.id} с изображением.")
            _send_post(chat_id, post, include_text=True)
            if post.image_path:
                send_photo(chat_id, post.image_path, caption=f"Изображение к посту #{post.id}", reply_markup=_post_keyboard(post.id))

        elif action == "asset_reconstruct":
            _clear_pending(chat_id)
            send_message(chat_id, f"🧠 Реконструирую контент-исходник #{post_id}: создаю structured blueprint, медицинский аудит и план визуала.")
            reconstruction = reconstruct_asset_with_ai(db, post_id, instruction=None)
            send_message(chat_id, "✅ Реконструкция создана.")
            send_message(chat_id, format_reconstruction_card(reconstruction), reply_markup=_reconstruction_keyboard(reconstruction.id))

        elif action == "reconstruction_create":
            _clear_pending(chat_id)
            post = create_post_from_reconstruction(db, post_id, with_image=False)
            send_message(chat_id, f"📝 По реконструкции #{post_id} создан пост #{post.id}.")
            _send_post(chat_id, post, include_text=True)

        elif action == "reconstruction_create_full":
            _clear_pending(chat_id)
            if reconstruction_needs_crop_preview(db, post_id):
                send_message(chat_id, f"🔎 Сначала проверяю crop-блоки реконструкции #{post_id}. Финальная сборка начнется только после подтверждения.")
                preview_path, report, critical = create_crop_preview_for_reconstruction(db, post_id)
                caption = "Проверка блоков перед сборкой"
                if critical:
                    caption += "\n⚠️ Найдены критические проблемы — лучше реконструировать заново."
                send_photo(chat_id, preview_path, caption=caption, reply_markup=_reconstruction_preview_keyboard(post_id))
                send_message(chat_id, _safe(report))
                PENDING_ACTIONS[str(chat_id)] = {"mode": "awaiting_crop_preview_confirmation", "reconstruction_id": post_id}
                return {"ok": True}

            send_message(chat_id, f"Создаю пост и визуал по structured reconstruction #{post_id}.")
            post = create_post_from_reconstruction(db, post_id, with_image=True)
            send_message(chat_id, f"📝🖼 По реконструкции #{post_id} создан пост #{post.id} с изображением.")
            _maybe_send_v30_image_choice(chat_id, post)
            _send_post(chat_id, post, include_text=True)
            if post.image_path:
                send_photo(chat_id, post.image_path, caption=f"Выбранное изображение к посту #{post.id}", reply_markup=_post_keyboard(post.id))

        elif action == "reconstruction_create_full_confirm":
            pending = _get_pending(chat_id) or {}
            if not (pending.get("mode") == "awaiting_crop_preview_confirmation" and int(pending.get("reconstruction_id") or 0) == int(post_id)):
                answer_callback_query(callback_query_id, text="Сначала сформируйте актуальный crop-preview для этой реконструкции.")
                send_message(chat_id, f"⚠️ Для реконструкции #{post_id} нужно сначала нажать «Пост + картинка», получить crop-preview и только потом подтверждать сборку.")
                return {"ok": True}
            _clear_pending(chat_id)
            send_message(chat_id, f"✅ Подтверждено. Собираю пост и инфографику по реконструкции #{post_id}.")
            post = create_post_from_reconstruction(db, post_id, with_image=True)
            send_message(chat_id, f"📝🖼 По реконструкции #{post_id} создан пост #{post.id} с изображением.")
            _maybe_send_v30_image_choice(chat_id, post)
            _send_post(chat_id, post, include_text=True)
            if post.image_path:
                send_photo(chat_id, post.image_path, caption=f"Выбранное изображение к посту #{post.id}", reply_markup=_post_keyboard(post.id))

        elif action == "select_image_polished":
            post = select_post_image_version(db, post_id, "polished")
            send_message(chat_id, f"✅ Для поста #{post.id} выбрана дизайн-версия изображения.")
            _send_post(chat_id, post, include_text=True)

        elif action == "select_image_draft":
            post = select_post_image_version(db, post_id, "draft")
            send_message(chat_id, f"✅ Для поста #{post.id} выбран точный технический черновик.")
            _send_post(chat_id, post, include_text=True)

        elif action == "asset_reconstruct_again_from_reconstruction":
            _clear_pending(chat_id)
            # Здесь post_id — ID реконструкции. Получаем исходник через существующую карточку.
            from app.services.reconstruction_engine import get_reconstruction_or_raise
            rec = get_reconstruction_or_raise(db, post_id)
            if not rec.asset_id:
                send_message(chat_id, "У реконструкции нет связанного исходника.")
                return {"ok": True}
            send_message(chat_id, f"🧠 Запускаю повторную реконструкцию исходника #{rec.asset_id} с учетом ошибок crop-preview.")
            new_rec = reconstruct_asset_with_ai(db, rec.asset_id, instruction="Сделай atomic blueprint максимально подробно: каждый визуальный объект отдельным блоком с точным source_bbox. Не группируй сетку в один блок.")
            send_message(chat_id, "✅ Новая реконструкция создана.")
            send_message(chat_id, format_reconstruction_card(new_rec), reply_markup=_reconstruction_keyboard(new_rec.id))

        else:
            send_message(chat_id, "Неизвестное действие кнопки.")

    except (PostNotFoundError, PostStatusError, PlanItemNotFoundError, PlanItemStatusError, RuntimeError) as exc:
        send_message(chat_id, _safe(str(exc)))
    except Exception as exc:
        send_message(chat_id, f"Ошибка:\n{_safe(str(exc))}")

    return {"ok": True}


@router.post("/webhook")
async def telegram_webhook(request: Request, db: Session = Depends(get_db)):
    update = await request.json()

    callback_query = update.get("callback_query")
    if callback_query:
        return _handle_callback(callback_query, db)

    message = update.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text = (message.get("text") or "").strip()

    if not chat_id:
        return {"ok": True}

    if not _is_admin(chat_id):
        send_message(chat_id, "Нет доступа к этому боту.")
        return {"ok": True}

    if _handle_pending_asset(chat_id, message, db):
        return {"ok": True}

    if _handle_pending_inspiration(chat_id, message, db):
        return {"ok": True}

    if text and _handle_pending_text(chat_id, text, db):
        return {"ok": True}

    if text == "/start" or text == "/help":
        send_message(
            chat_id,
            "<b>Бот SMM-менеджера работает.</b>\n\n"
            "Команды:\n"
            "/generate тема — создать пост без изображения\n"
            "/generate_full тема — создать пост + изображение\n"
            "/posts — последние посты\n"
            "/post ID — посмотреть пост\n"
            "/plan_week — создать 7 тем на неделю\n"
            "/plan — показать контент-план\n"
            "/create_from_plan ID — создать пост по пункту плана\n"
            "/create_full_from_plan ID — создать пост + картинку по пункту плана\n"
            "/analyze_asset — загрузить материал для анализа и реконструкции\n"
            "/assets — показать сохраненные исходники\n"
            "/patterns — показать найденные паттерны\n"
            "/generate_from_pattern ID — создать пост по паттерну\n"
            "/inspire — старый режим карточек вдохновения\n"
            "/analyze_url ссылка — проанализировать пост/страницу по ссылке\n"
            "/inspirations — показать карточки вдохновения\n"
            "/plan_week_from_inspirations — создать план на основе карточек\n"
            "/cancel — отменить редактирование или добавление вдохновения\n\n"
            "Редактирование, одобрение, отклонение, генерация изображения и публикация доступны кнопками под постом.",
        )
        return {"ok": True}

    if text == "/analyze_asset":
        _set_pending_mode(chat_id, "asset")
        send_message(
            chat_id,
            "Пришлите следующим сообщением материал для глубокого анализа:\n"
            "• мем или скриншот;\n"
            "• пересланный Telegram-пост;\n"
            "• фото/картинку с подписью;\n"
            "• текст поста;\n"
            "• ссылку с пояснением.\n\n"
            "Я сохраню исходник и подготовлю его к реконструкции. После анализа нажмите кнопку 🧠 Реконструировать.\n"
            "Для отмены напишите /cancel.",
        )
        return {"ok": True}

    if text == "/assets":
        items = list_assets(db, limit=10)
        if not items:
            send_message(chat_id, "Контент-исходников пока нет. Добавьте первый через /analyze_asset")
            return {"ok": True}
        send_message(chat_id, "<b>Последние контент-исходники:</b>")
        for item in items:
            send_message(chat_id, format_asset_card(item), reply_markup=_asset_keyboard(item.id))
        return {"ok": True}

    if text == "/patterns":
        items = list_patterns(db, limit=10)
        if not items:
            send_message(chat_id, "Паттернов пока нет. Добавьте материал через /analyze_asset")
            return {"ok": True}
        send_message(chat_id, "<b>Последние паттерны:</b>")
        for item in items:
            send_message(chat_id, format_pattern_card(item), reply_markup=_pattern_keyboard(item.id))
        return {"ok": True}

    if text.startswith("/reconstruct_asset"):
        asset_id, extra_instruction = _parse_id_and_payload(text, "/reconstruct_asset")
        if not asset_id:
            send_message(chat_id, "Укажите ID исходника. Например: /reconstruct_asset 1")
            return {"ok": True}
        try:
            send_message(chat_id, f"🧠 Реконструирую контент-исходник #{asset_id}: заголовок, медицинскую точность, добавления и визуальную стратегию.")
            reconstruction = reconstruct_asset_with_ai(db, asset_id, instruction=extra_instruction or None)
            send_message(chat_id, "✅ Реконструкция создана.")
            send_message(chat_id, format_reconstruction_card(reconstruction), reply_markup=_reconstruction_keyboard(reconstruction.id))
        except Exception as exc:
            send_message(chat_id, _safe(str(exc)))
        return {"ok": True}

    if text == "/reconstructions":
        items = list_reconstructions(db, limit=10)
        if not items:
            send_message(chat_id, "Реконструкций пока нет. Сначала добавьте материал через /analyze_asset, затем /reconstruct_asset ID")
            return {"ok": True}
        send_message(chat_id, "<b>Последние реконструкции:</b>")
        for item in items:
            send_message(chat_id, format_reconstruction_card(item), reply_markup=_reconstruction_keyboard(item.id))
        return {"ok": True}

    if text.startswith("/create_full_from_reconstruction"):
        reconstruction_id, _ = _parse_id_and_payload(text, "/create_full_from_reconstruction")
        if not reconstruction_id:
            send_message(chat_id, "Укажите ID реконструкции. Например: /create_full_from_reconstruction 1")
            return {"ok": True}
        try:
            if reconstruction_needs_crop_preview(db, reconstruction_id):
                send_message(chat_id, f"🔎 Сначала проверяю crop-блоки реконструкции #{reconstruction_id}. Финальная сборка начнется только после подтверждения.")
                preview_path, report, critical = create_crop_preview_for_reconstruction(db, reconstruction_id)
                caption = "Проверка блоков перед сборкой"
                if critical:
                    caption += "\n⚠️ Найдены критические проблемы — лучше реконструировать заново."
                send_photo(chat_id, preview_path, caption=caption, reply_markup=_reconstruction_preview_keyboard(reconstruction_id))
                send_message(chat_id, _safe(report))
                return {"ok": True}

            send_message(chat_id, f"Создаю пост и изображение по реконструкции #{reconstruction_id}.")
            post = create_post_from_reconstruction(db, reconstruction_id, with_image=True)
            send_message(chat_id, f"По реконструкции #{reconstruction_id} создан пост #{post.id} с изображением.")
            _maybe_send_v30_image_choice(chat_id, post)
            _send_post(chat_id, post, include_text=True)
            if post.image_path:
                send_photo(chat_id, post.image_path, caption=f"Изображение к посту #{post.id}", reply_markup=_post_keyboard(post.id))
        except Exception as exc:
            send_message(chat_id, _safe(str(exc)))
        return {"ok": True}

    if text.startswith("/create_from_reconstruction"):
        reconstruction_id, _ = _parse_id_and_payload(text, "/create_from_reconstruction")
        if not reconstruction_id:
            send_message(chat_id, "Укажите ID реконструкции. Например: /create_from_reconstruction 1")
            return {"ok": True}
        try:
            post = create_post_from_reconstruction(db, reconstruction_id, with_image=False)
            send_message(chat_id, f"По реконструкции #{reconstruction_id} создан пост #{post.id}.")
            _send_post(chat_id, post, include_text=True)
        except Exception as exc:
            send_message(chat_id, _safe(str(exc)))
        return {"ok": True}

    if text.startswith("/generate_from_pattern"):
        pattern_id, _ = _parse_id_and_payload(text, "/generate_from_pattern")
        if not pattern_id:
            send_message(chat_id, "Укажите ID паттерна. Например: /generate_from_pattern 1")
            return {"ok": True}
        try:
            send_message(chat_id, f"Создаю пост и изображение по паттерну #{pattern_id}.")
            post = generate_post_from_pattern(db, pattern_id, with_image=True)
            send_message(chat_id, f"По паттерну #{pattern_id} создан пост #{post.id}.")
            _send_post(chat_id, post, include_text=True)
            if post.image_path:
                send_photo(chat_id, post.image_path, caption=f"Изображение к посту #{post.id}", reply_markup=_post_keyboard(post.id))
        except Exception as exc:
            send_message(chat_id, _safe(str(exc)))
        return {"ok": True}

    if text == "/inspire":
        _set_pending_mode(chat_id, "inspire")
        send_message(
            chat_id,
            "Пришлите следующим сообщением материал для анализа:\n"
            "• перешлите пост из Telegram;\n"
            "• отправьте фото/картинку с подписью;\n"
            "• отправьте текст успешного поста;\n"
            "• можно отправить сообщение со ссылкой.\n\n"
            "Я сохраню не копию, а карточку вдохновения: идею, формат, хук, причину успеха и новую тему для нашей клиники.\n"
            "Для отмены напишите /cancel.",
        )
        return {"ok": True}

    if text.startswith("/analyze_url"):
        url = text.replace("/analyze_url", "", 1).strip()
        if not url:
            send_message(chat_id, "Пришлите ссылку. Например:\n/analyze_url https://example.com/post")
            return {"ok": True}
        send_message(chat_id, "Пробую получить данные по ссылке и проанализировать материал.")
        try:
            data = fetch_url_preview(url)
            inspiration = create_inspiration(db, data)
            send_message(chat_id, "✅ Карточка вдохновения по ссылке создана.")
            send_message(chat_id, format_inspiration_card(inspiration))
        except Exception as exc:
            send_message(chat_id, f"Ошибка анализа ссылки:\n{_safe(str(exc))}")
        return {"ok": True}

    if text == "/inspirations":
        items = list_inspirations(db, limit=10)
        if not items:
            send_message(chat_id, "Карточек вдохновения пока нет. Добавьте первую через /inspire или /analyze_url")
            return {"ok": True}
        send_message(chat_id, "<b>Последние карточки вдохновения:</b>")
        for item in items:
            send_message(chat_id, format_inspiration_card(item))
        return {"ok": True}

    if text == "/plan_week_from_inspirations":
        send_message(chat_id, "Генерирую контент-план на основе карточек вдохновения.")
        try:
            items = generate_week_plan_from_inspirations(db, platform="telegram")
        except Exception as exc:
            send_message(chat_id, f"Ошибка генерации плана по вдохновениям:\n{_safe(str(exc))}")
            return {"ok": True}

        send_message(chat_id, "<b>Контент-план на основе вдохновений создан:</b>")
        for item in items:
            send_message(chat_id, _plan_item_card(item), reply_markup=_plan_keyboard(item.id))
        return {"ok": True}

    if text == "/plan_week":
        send_message(chat_id, "Генерирую контент-план на неделю.")
        try:
            items = generate_week_plan(db, platform="telegram")
        except Exception as exc:
            send_message(chat_id, f"Ошибка генерации плана:\n{_safe(str(exc))}")
            return {"ok": True}

        send_message(chat_id, "<b>Контент-план на неделю создан:</b>")
        for item in items:
            send_message(chat_id, _plan_item_card(item), reply_markup=_plan_keyboard(item.id))
        return {"ok": True}

    if text == "/plan":
        items = list_plan_items(db, limit=20)
        if not items:
            send_message(chat_id, "Контент-план пока пуст. Создайте его командой /plan_week")
            return {"ok": True}

        send_message(chat_id, "<b>Текущий контент-план:</b>")
        for item in items:
            send_message(chat_id, _plan_item_card(item), reply_markup=_plan_keyboard(item.id))
        return {"ok": True}

    if text.startswith("/create_full_from_plan"):
        item_id, _ = _parse_id_and_payload(text, "/create_full_from_plan")
        if not item_id:
            send_message(chat_id, "Укажите ID пункта плана. Например: /create_full_from_plan 1")
            return {"ok": True}
        try:
            send_message(chat_id, f"Создаю пост и изображение по пункту плана #{item_id}.")
            item, post = create_post_from_plan_item(db, item_id, with_image=True)
            send_message(chat_id, f"По пункту плана #{item.id} создан пост #{post.id} с изображением.")
            _send_post(chat_id, post, include_text=True)
            if post.image_path:
                send_photo(chat_id, post.image_path, caption=f"Изображение к посту #{post.id}", reply_markup=_post_keyboard(post.id))
        except Exception as exc:
            send_message(chat_id, _safe(str(exc)))
        return {"ok": True}

    if text.startswith("/create_from_plan"):
        item_id, _ = _parse_id_and_payload(text, "/create_from_plan")
        if not item_id:
            send_message(chat_id, "Укажите ID пункта плана. Например: /create_from_plan 1")
            return {"ok": True}
        try:
            item, post = create_post_from_plan_item(db, item_id, with_image=False)
            send_message(chat_id, f"По пункту плана #{item.id} создан пост #{post.id}.")
            _send_post(chat_id, post, include_text=True)
        except Exception as exc:
            send_message(chat_id, _safe(str(exc)))
        return {"ok": True}

    if text == "/posts":
        posts = list_recent_posts(db, limit=10)
        if not posts:
            send_message(chat_id, "Постов пока нет.")
            return {"ok": True}

        send_message(chat_id, "<b>Последние посты:</b>")
        for post in posts:
            send_message(
                chat_id,
                f"#{post.id} | {_safe(post.platform)} | {_safe(post.status)} | {_safe(getattr(post, 'headline', None) or post.title)}",
                reply_markup=_post_keyboard(post.id),
            )
        return {"ok": True}

    if text.startswith("/post"):
        post_id, _ = _parse_id_and_payload(text, "/post")
        if not post_id:
            send_message(chat_id, "Укажите ID поста. Например: /post 1")
            return {"ok": True}
        try:
            post = get_post_or_raise(db, post_id)
            _send_post(chat_id, post, include_text=True)
        except PostNotFoundError as exc:
            send_message(chat_id, str(exc))
        return {"ok": True}

    if text.startswith("/generate_full"):
        topic = text.replace("/generate_full", "", 1).strip()
        if not topic:
            send_message(chat_id, "Напишите тему после команды. Например:\n/generate_full Анализы на витамин D")
            return {"ok": True}

        send_message(chat_id, "Генерирую пост и изображение.")

        try:
            post = create_generated_post(
                db=db,
                topic=topic,
                platform="telegram",
                service_offer=None,
                with_image=True,
            )
        except Exception as exc:
            send_message(chat_id, f"Ошибка генерации:\n{_safe(str(exc))}")
            return {"ok": True}

        send_message(chat_id, "Пост и изображение созданы и сохранены в базе.")
        _send_post(chat_id, post, include_text=True)
        if post.image_path:
            try:
                send_photo(chat_id, post.image_path, caption=f"Изображение к посту #{post.id}", reply_markup=_post_keyboard(post.id))
            except Exception as exc:
                send_message(chat_id, f"Изображение создано, но не удалось отправить превью:\n{_safe(str(exc))}")
        return {"ok": True}

    if text.startswith("/generate"):
        topic = text.replace("/generate", "", 1).strip()
        if not topic:
            send_message(chat_id, "Напишите тему после команды. Например:\n/generate Анализы на витамин D")
            return {"ok": True}

        send_message(chat_id, "Генерирую пост.")

        try:
            post = create_generated_post(
                db=db,
                topic=topic,
                platform="telegram",
                service_offer=None,
                with_image=False,
            )
        except Exception as exc:
            send_message(chat_id, f"Ошибка генерации:\n{_safe(str(exc))}")
            return {"ok": True}

        send_message(chat_id, "Пост создан и сохранен в базе.")
        _send_post(chat_id, post, include_text=True)
        return {"ok": True}

    if text.startswith("/approve"):
        post_id, _ = _parse_id_and_payload(text, "/approve")
        if not post_id:
            send_message(chat_id, "Укажите ID поста. Например: /approve 1")
            return {"ok": True}
        try:
            post = approve_post(db, post_id)
            send_message(chat_id, f"Пост #{post.id} одобрен.", reply_markup=_post_keyboard(post.id))
        except Exception as exc:
            send_message(chat_id, _safe(str(exc)))
        return {"ok": True}

    if text.startswith("/publish"):
        post_id, _ = _parse_id_and_payload(text, "/publish")
        if not post_id:
            send_message(chat_id, "Укажите ID поста. Например: /publish 1")
            return {"ok": True}
        try:
            post = publish_post(db, post_id)
            send_message(chat_id, f"Пост #{post.id} опубликован в тестовую группу. Статус: published")
        except (PostNotFoundError, PostStatusError, RuntimeError) as exc:
            send_message(chat_id, _safe(str(exc)))
        except Exception as exc:
            send_message(chat_id, f"Ошибка публикации:\n{_safe(str(exc))}")
        return {"ok": True}

    if text.startswith("/image"):
        post_id, instruction = _parse_id_and_payload(text, "/image")
        if not post_id:
            send_message(chat_id, "Укажите ID поста. Например:\n/image 1")
            return {"ok": True}

        send_message(chat_id, "Генерирую изображение.")
        try:
            post = generate_or_replace_image(db, post_id, instruction or None)
            send_message(chat_id, f"Изображение для поста #{post.id} создано/обновлено.", reply_markup=_post_keyboard(post.id))
            if post.image_path:
                send_photo(chat_id, post.image_path, caption=f"Изображение к посту #{post.id}", reply_markup=_post_keyboard(post.id))
        except Exception as exc:
            send_message(chat_id, f"Ошибка генерации изображения:\n{_safe(str(exc))}")
        return {"ok": True}

    if text.startswith("/reject"):
        post_id, _ = _parse_id_and_payload(text, "/reject")
        if not post_id:
            send_message(chat_id, "Укажите ID поста. Например: /reject 1")
            return {"ok": True}
        try:
            post = reject_post(db, post_id)
            send_message(chat_id, f"Пост #{post.id} отклонен. Статус: rejected", reply_markup=_post_keyboard(post.id))
        except Exception as exc:
            send_message(chat_id, _safe(str(exc)))
        return {"ok": True}

    # Старые текстовые команды оставлены для совместимости.
    if text.startswith("/edit"):
        post_id, new_text = _parse_id_and_payload(text, "/edit")
        if not post_id or not new_text:
            send_message(chat_id, "Формат команды:\n/edit 1 Полный новый текст поста")
            return {"ok": True}
        try:
            post = edit_post_manually(db, post_id, new_text)
            send_message(chat_id, "Пост отредактирован вручную. Ранее созданная картинка сохранена.")
            _send_post(chat_id, post, include_text=True)
        except Exception as exc:
            send_message(chat_id, _safe(str(exc)))
        return {"ok": True}

    if text.startswith("/rewrite"):
        post_id, instruction = _parse_id_and_payload(text, "/rewrite")
        if not post_id or not instruction:
            send_message(chat_id, "Формат команды:\n/rewrite 1 Сделай текст короче и убери повторы")
            return {"ok": True}

        send_message(chat_id, "Редактирую пост через ИИ.")
        try:
            post = rewrite_post_with_ai(db, post_id, instruction)
            send_message(chat_id, "Пост отредактирован через ИИ. Ранее созданная картинка сохранена.")
            _send_post(chat_id, post, include_text=True)
        except Exception as exc:
            send_message(chat_id, _safe(str(exc)))
        return {"ok": True}

    send_message(chat_id, "Команда не распознана. Напишите /help")
    return {"ok": True}


@router.post("/set-webhook")
def setup_telegram_webhook():
    if not settings.public_base_url:
        raise HTTPException(
            status_code=400,
            detail="PUBLIC_BASE_URL не задан. Добавьте PUBLIC_BASE_URL в Railway Variables.",
        )

    webhook_url = settings.public_base_url.rstrip("/") + "/telegram/webhook"
    try:
        return set_webhook(webhook_url)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/webhook-info")
def telegram_webhook_info():
    try:
        return get_webhook_info()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
