import html
import threading
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal, get_db
from app.services.content_plan import create_post_from_plan_item, generate_week_plan, list_plan_items
from app.services.pattern_analyzer import (
    analyze_and_save_asset,
    build_asset_input_from_telegram_message,
    format_asset_card,
    list_assets,
)
from app.services.semantic_reconstruction_engine import run_semantic_reconstruction_analysis
from app.services.semantic_assets import generate_semantic_pngs, compose_reconstruction
from app.services.project_state_manager import get_payload
from app.services.cost_tracker import format_cost_summary
from app.services.semantic_analysis_store import (
    export_analysis_row_to_file,
    get_latest_analysis_from_db,
    list_analyses_from_db,
)

from app.services.post_manager import (
    approve_post,
    edit_post_manually,
    generate_or_replace_image,
    get_post_or_raise,
    list_recent_posts,
    publish_post,
    reject_post,
    rewrite_post_with_ai,
)
from app.services.telegram_bot import (
    TelegramBotError,
    answer_callback_query,
    get_webhook_info,
    send_document,
    send_message,
    send_photo,
    set_webhook,
)

router = APIRouter(prefix="/telegram", tags=["telegram"])

PENDING_ACTIONS: dict[str, dict] = {}
RUNNING_SEMANTIC_ANALYSES: set[int] = set()


def _is_admin(chat_id: int | str) -> bool:
    if not settings.admin_telegram_id:
        return True
    return str(chat_id) == str(settings.admin_telegram_id)


def _safe(text: str | None) -> str:
    return html.escape(text or "")


def _shorten(text: str, limit: int = 3500) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n\n...текст обрезан."


def _analysis_dir() -> Path:
    return Path("storage/analysis")


def _find_analysis_files(asset_id: int | None = None) -> list[Path]:
    analysis_dir = _analysis_dir()
    if not analysis_dir.exists():
        return []
    pattern = f"asset-{asset_id}-state-*-semantic-analysis.json" if asset_id is not None else "asset-*-state-*-semantic-analysis.json"
    return sorted(analysis_dir.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)


def _send_analysis_file(chat_id: int | str, asset_id: int) -> None:
    # Prefer PostgreSQL: this is the persistent canonical copy.
    db = SessionLocal()
    try:
        row = get_latest_analysis_from_db(db, asset_id)
        if row is not None:
            path = export_analysis_row_to_file(row)
            send_document(
                chat_id,
                str(path),
                caption=(
                    f"JSON v41-анализа исходника #{asset_id}: "
                    f"<code>{_safe(path.name)}</code>\n"
                    f"Источник: <b>PostgreSQL</b>, ProjectState: <code>{row.project_state_id}</code>"
                ),
            )
            return
    finally:
        db.close()

    # Backward-compatible fallback for old local files.
    files = _find_analysis_files(asset_id)
    if not files:
        send_message(
            chat_id,
            f"❌ JSON анализа для исходника #{asset_id} не найден ни в PostgreSQL, ни в <code>storage/analysis</code>.\n\n"
            "Сначала запустите v41-анализ через кнопку 🧠 Анализировать инфографику (v41).",
        )
        return
    path = files[0]
    send_document(
        chat_id,
        str(path),
        caption=f"JSON v41-анализа исходника #{asset_id}: <code>{_safe(path.name)}</code>\nИсточник: <b>storage/analysis</b>",
    )


def _send_analysis_list(chat_id: int | str, limit: int = 20) -> None:
    db = SessionLocal()
    try:
        rows = list_analyses_from_db(db, limit=limit)
        if rows:
            lines = ["🗄 <b>v41-анализы в PostgreSQL:</b>"]
            for row in rows:
                size_kb = max(1, round(len(row.analysis_json or "") / 1024))
                cost = f", ${row.estimated_cost_usd}" if row.estimated_cost_usd else ""
                lines.append(
                    f"• asset <code>#{row.asset_id}</code>, state <code>{row.project_state_id}</code> — {size_kb} KB{cost}"
                )
            lines.append("\nЧтобы скачать файл: <code>/get_analysis 22</code>")
            send_message(chat_id, "\n".join(lines))
            return
    finally:
        db.close()

    files = _find_analysis_files()
    if not files:
        send_message(chat_id, "JSON-анализов пока нет ни в PostgreSQL, ни в <code>storage/analysis</code>.")
        return
    lines = ["📁 <b>JSON-файлы v41-анализа в storage/analysis:</b>"]
    for path in files[:limit]:
        size_kb = max(1, round(path.stat().st_size / 1024))
        lines.append(f"• <code>{_safe(path.name)}</code> — {size_kb} KB")
    lines.append("\nЧтобы скачать файл: <code>/get_analysis 22</code>")
    send_message(chat_id, "\n".join(lines))


def _post_keyboard(post_id: int) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Одобрить", "callback_data": f"approve:{post_id}"},
                {"text": "🚀 Опубликовать", "callback_data": f"publish:{post_id}"},
            ],
            [{"text": "✏️ Редактировать вручную", "callback_data": f"edit_manual:{post_id}"}],
            [
                {"text": "🤖 ИИ-редактирование", "callback_data": f"rewrite_ai:{post_id}"},
                {"text": "🖼 Картинка", "callback_data": f"image:{post_id}"},
            ],
            [{"text": "❌ Отклонить", "callback_data": f"reject:{post_id}"}],
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




def _asset_keyboard(asset_id: int) -> dict:
    return {
        "inline_keyboard": [
            [{"text": "🧠 Анализировать инфографику (v41)", "callback_data": f"semantic_analyze:{asset_id}"}],
            [{"text": "🧩 Сгенерировать Semantic PNG", "callback_data": f"semantic_png:{asset_id}"}],
            [{"text": "🖼 Собрать финальную инфографику", "callback_data": f"compose_reconstruction:{asset_id}"}],
        ]
    }


def _format_semantic_analysis_summary(state, issues: list[str]) -> str:
    payload = get_payload(state)
    analysis = payload.analysis_state or {}
    topic = analysis.get("topic") or "не определена"
    blueprint = payload.design_blueprint if isinstance(payload.design_blueprint, dict) else {}
    layout = blueprint.get("layout") or blueprint.get("structure") or "не указан"
    path = analysis.get("analysis_json_path") or payload.custom.get("analysis_json_path") or ""
    cost_summary = analysis.get("cost_estimate") or payload.custom.get("cost_estimate")
    lines = [
        f"✅ <b>v41 анализ готов</b>",
        f"ProjectState: <code>{state.id}</code>",
        f"Тема: {_safe(str(topic))}",
        f"Visual entities: <b>{len(payload.visual_entity_map)}</b>",
        f"Semantic PNG: <b>{len(payload.semantic_png_plan)}</b>",
        f"Design blueprint: {_safe(str(layout))}",
    ]
    if path:
        lines.append(f"JSON сохранен: <code>{_safe(path)}</code>")
    cost_text = format_cost_summary(cost_summary)
    if cost_text:
        lines.append(cost_text)
    if issues:
        shown = "\n".join(f"• {_safe(str(x))}" for x in issues[:12])
        lines.append("\n<b>Validation issues:</b>\n" + shown)
        if len(issues) > 12:
            lines.append(f"...и еще {len(issues)-12}")
    return "\n".join(lines)


def _run_semantic_analysis_background(chat_id: int | str, asset_id: int) -> None:
    db = SessionLocal()
    try:
        state, issues = run_semantic_reconstruction_analysis(db=db, asset_id=asset_id)
        send_message(chat_id, _format_semantic_analysis_summary(state, issues))
    except Exception as exc:
        send_message(chat_id, f"❌ Ошибка v41 анализа исходника #{asset_id}: {_safe(str(exc))}")
    finally:
        RUNNING_SEMANTIC_ANALYSES.discard(asset_id)
        db.close()


def _start_semantic_analysis(chat_id: int | str, asset_id: int) -> None:
    if asset_id in RUNNING_SEMANTIC_ANALYSES:
        send_message(chat_id, f"⏳ Анализ исходника #{asset_id} уже выполняется.")
        return
    RUNNING_SEMANTIC_ANALYSES.add(asset_id)
    send_message(chat_id, f"🧠 Запускаю v41 semantic-анализ исходника #{asset_id}. Это только этап 1: visual entities → semantic PNG plan → design blueprint.")
    thread = threading.Thread(target=_run_semantic_analysis_background, args=(chat_id, asset_id), daemon=True)
    thread.start()




def _generate_semantic_png_background(chat_id: int | str, asset_id: int) -> None:
    try:
        done, skipped, cost_summary = generate_semantic_pngs(asset_id)
        text = (
            "✅ <b>Semantic PNG готовы</b>\n"
            f"Исходник: <code>#{asset_id}</code>\n"
            f"Создано: <b>{len(done)}</b>\n"
            f"Уже было: <b>{len(skipped)}</b>"
            f"{format_cost_summary(cost_summary)}\n\n"
            f"Теперь можно выполнить: <code>/compose_reconstruction {asset_id}</code>"
        )
        send_message(chat_id, text)
    except Exception as exc:
        send_message(chat_id, f"❌ Ошибка генерации Semantic PNG для исходника #{asset_id}: {_safe(str(exc))}")


def _start_semantic_png_generation(chat_id: int | str, asset_id: int) -> None:
    send_message(chat_id, f"🧩 Запускаю генерацию Semantic PNG для исходника #{asset_id}. Это может занять несколько минут.")
    thread = threading.Thread(target=_generate_semantic_png_background, args=(chat_id, asset_id), daemon=True)
    thread.start()


def _compose_reconstruction_and_send(chat_id: int | str, asset_id: int) -> None:
    try:
        path = compose_reconstruction(asset_id)
        send_photo(chat_id, path, caption=f"✅ Финальная инфографика собрана для исходника #{asset_id}")
    except Exception as exc:
        send_message(chat_id, f"❌ Ошибка сборки финальной инфографики #{asset_id}: {_safe(str(exc))}")


def _set_pending_mode(chat_id: int | str, mode: str, **kwargs) -> None:
    PENDING_ACTIONS[str(chat_id)] = {"mode": mode, **kwargs}


def _clear_pending(chat_id: int | str) -> None:
    PENDING_ACTIONS.pop(str(chat_id), None)


def _parse_id_and_payload(text: str, command: str) -> tuple[int | None, str | None]:
    rest = text.replace(command, "", 1).strip()
    if not rest:
        return None, None
    parts = rest.split(maxsplit=1)
    try:
        item_id = int(parts[0])
    except ValueError:
        return None, None
    return item_id, parts[1] if len(parts) > 1 else None


def _send_post(chat_id: int | str, post) -> None:
    text = f"<b>Пост #{post.id}</b> — {_safe(post.status)}\n<b>{_safe(post.headline or post.title)}</b>\n\n{_safe(_shorten(post.text or ''))}"
    if post.image_path:
        try:
            send_photo(chat_id, post.image_path, caption=text, reply_markup=_post_keyboard(post.id))
            return
        except Exception:
            pass
    send_message(chat_id, text, reply_markup=_post_keyboard(post.id))


def _handle_pending_text(chat_id: int | str, text: str, db: Session) -> bool:
    pending = PENDING_ACTIONS.get(str(chat_id))
    if not pending:
        return False
    mode = pending.get("mode")
    try:
        if mode == "edit_manual":
            post = edit_post_manually(db, int(pending["post_id"]), text)
            _clear_pending(chat_id)
            send_message(chat_id, "✅ Текст заменен.")
            _send_post(chat_id, post)
            return True
        if mode == "rewrite_ai":
            post = rewrite_post_with_ai(db, int(pending["post_id"]), text)
            _clear_pending(chat_id)
            send_message(chat_id, "✅ ИИ-редактирование выполнено.")
            _send_post(chat_id, post)
            return True
    except Exception as exc:
        _clear_pending(chat_id)
        send_message(chat_id, f"❌ Ошибка: {_safe(str(exc))}")
        return True
    return False


def _handle_pending_asset(chat_id: int | str, message: dict, db: Session) -> bool:
    pending = PENDING_ACTIONS.get(str(chat_id))
    if not pending or pending.get("mode") != "asset":
        return False
    try:
        data = build_asset_input_from_telegram_message(message)
        asset, _pattern, _context = analyze_and_save_asset(db, data)
        _clear_pending(chat_id)
        send_message(
            chat_id,
            format_asset_card(asset)
            + "\n\nМатериал сохранен. Для проверки новой архитектуры запустите только этап 1 — semantic-анализ.",
            reply_markup=_asset_keyboard(asset.id),
        )
    except Exception as exc:
        _clear_pending(chat_id)
        send_message(chat_id, f"❌ Не удалось сохранить материал: {_safe(str(exc))}")
    return True


@router.post("/set-webhook")
def set_telegram_webhook():
    if not settings.webhook_base_url:
        raise HTTPException(status_code=400, detail="WEBHOOK_BASE_URL is not set")
    try:
        return set_webhook(f"{settings.webhook_base_url.rstrip('/')}/telegram/webhook")
    except TelegramBotError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/webhook-info")
def telegram_webhook_info():
    try:
        return get_webhook_info()
    except TelegramBotError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/webhook")
async def telegram_webhook(request: Request, db: Session = Depends(get_db)):
    update = await request.json()
    callback_query = update.get("callback_query")
    if callback_query:
        return _handle_callback(callback_query, db)
    message = update.get("message") or update.get("edited_message")
    if not message:
        return {"ok": True}
    chat_id = message.get("chat", {}).get("id")
    if not chat_id or not _is_admin(chat_id):
        return {"ok": True}
    text = (message.get("text") or "").strip()
    if _handle_pending_asset(chat_id, message, db):
        return {"ok": True}
    if text and _handle_pending_text(chat_id, text, db):
        return {"ok": True}
    _handle_command(chat_id, text, db)
    return {"ok": True}


def _handle_callback(callback_query: dict, db: Session):
    callback_query_id = callback_query.get("id")
    try:
        answer_callback_query(callback_query_id)
    except Exception:
        pass
    chat_id = callback_query.get("message", {}).get("chat", {}).get("id")
    if not chat_id or not _is_admin(chat_id):
        return {"ok": True}
    data = callback_query.get("data") or ""
    if ":" not in data:
        send_message(chat_id, "Неизвестное действие кнопки.")
        return {"ok": True}
    action, raw_id = data.split(":", 1)
    try:
        item_id = int(raw_id)
    except ValueError:
        send_message(chat_id, "Некорректный ID.")
        return {"ok": True}
    try:
        if action == "approve":
            _send_post(chat_id, approve_post(db, item_id))
        elif action == "publish":
            _send_post(chat_id, publish_post(db, item_id))
        elif action == "reject":
            _send_post(chat_id, reject_post(db, item_id))
        elif action == "edit_manual":
            _set_pending_mode(chat_id, "edit_manual", post_id=item_id)
            send_message(chat_id, "Введите полную новую версию текста поста.")
        elif action == "rewrite_ai":
            _set_pending_mode(chat_id, "rewrite_ai", post_id=item_id)
            send_message(chat_id, "Напишите, что нужно исправить в посте.")
        elif action == "image":
            _send_post(chat_id, generate_or_replace_image(db, item_id))
        elif action == "plan_create":
            _item, post = create_post_from_plan_item(db=db, item_id=item_id, with_image=False)
            _send_post(chat_id, post)
        elif action == "plan_create_full":
            _item, post = create_post_from_plan_item(db=db, item_id=item_id, with_image=True)
            _send_post(chat_id, post)
        elif action == "semantic_analyze":
            _start_semantic_analysis(chat_id, item_id)
        elif action == "semantic_png":
            _start_semantic_png_generation(chat_id, item_id)
        elif action == "compose_reconstruction":
            _compose_reconstruction_and_send(chat_id, item_id)
        else:
            send_message(chat_id, "Это действие отключено в очищенной версии. Используйте кнопку v41 semantic-анализ для инфографик.")
    except Exception as exc:
        send_message(chat_id, f"❌ Ошибка: {_safe(str(exc))}")
    return {"ok": True}


def _handle_command(chat_id: int | str, text: str, db: Session) -> None:
    if not text or text in {"/start", "/help"}:
        send_message(
            chat_id,
            "Команды:\n"
            "/plan — план контента\n"
            "/generate_week_plan — создать недельный план\n"
            "/posts — последние посты\n"
            "/analyze_asset — загрузить материал/инфографику\n"
            "/assets — последние исходники\n"
            "/list_analysis — список JSON анализа\n"
            "/get_analysis 23 — скачать JSON анализа\n"
            "/generate_semantic_png 23 — сгенерировать Semantic PNG\n"
            "/compose_reconstruction 23 — собрать финальную инфографику\n\n"
            "Для инфографик доступна цепочка v41.1: анализ → Semantic PNG → Layout Composer.",
        )
        return
    if text == "/generate_week_plan":
        try:
            items = generate_week_plan(db=db, platform="telegram")
            for item in items[:10]:
                send_message(chat_id, f"<b>Пункт #{item.id}</b>\n{_safe(item.topic)}", reply_markup=_plan_keyboard(item.id))
        except Exception as exc:
            send_message(chat_id, f"❌ Ошибка: {_safe(str(exc))}")
        return
    if text == "/plan":
        items = list_plan_items(db, limit=10)
        if not items:
            send_message(chat_id, "План пока пуст. Используйте /generate_week_plan")
            return
        for item in items:
            send_message(chat_id, f"<b>Пункт #{item.id}</b> — {_safe(item.status)}\n{_safe(item.topic)}", reply_markup=_plan_keyboard(item.id))
        return
    if text == "/posts":
        posts = list_recent_posts(db, limit=10)
        if not posts:
            send_message(chat_id, "Постов пока нет.")
            return
        for post in posts:
            _send_post(chat_id, post)
        return
    if text == "/analyze_asset":
        _set_pending_mode(chat_id, "asset")
        send_message(chat_id, "Отправьте картинку, скриншот, пост, ссылку или текст для анализа.")
        return
    if text == "/assets":
        items = list_assets(db, limit=10)
        if not items:
            send_message(chat_id, "Контент-исходников пока нет. Добавьте первый через /analyze_asset")
            return
        for item in items:
            send_message(chat_id, format_asset_card(item), reply_markup=_asset_keyboard(item.id))
        return

    if text == "/list_analysis":
        _send_analysis_list(chat_id)
        return
    if text.startswith("/get_analysis"):
        asset_id, _ = _parse_id_and_payload(text, "/get_analysis")
        if not asset_id:
            send_message(chat_id, "Укажите ID исходника. Например: <code>/get_analysis 22</code>")
            return
        try:
            _send_analysis_file(chat_id, asset_id)
        except Exception as exc:
            send_message(chat_id, f"❌ Не удалось отправить JSON анализа: {_safe(str(exc))}")
        return
    if text.startswith("/generate_semantic_png"):
        asset_id, _ = _parse_id_and_payload(text, "/generate_semantic_png")
        if not asset_id:
            send_message(chat_id, "Укажите ID исходника. Например: <code>/generate_semantic_png 23</code>")
            return
        _start_semantic_png_generation(chat_id, asset_id)
        return
    if text.startswith("/compose_reconstruction"):
        asset_id, _ = _parse_id_and_payload(text, "/compose_reconstruction")
        if not asset_id:
            send_message(chat_id, "Укажите ID исходника. Например: <code>/compose_reconstruction 23</code>")
            return
        _compose_reconstruction_and_send(chat_id, asset_id)
        return
    if text.startswith("/create_from_plan") or text.startswith("/create_full_from_plan"):
        full = text.startswith("/create_full_from_plan")
        command = "/create_full_from_plan" if full else "/create_from_plan"
        item_id, _ = _parse_id_and_payload(text, command)
        if not item_id:
            send_message(chat_id, f"Укажите ID. Например: {command} 1")
            return
        try:
            _item, post = create_post_from_plan_item(db=db, item_id=item_id, with_image=full)
            _send_post(chat_id, post)
        except Exception as exc:
            send_message(chat_id, f"❌ Ошибка: {_safe(str(exc))}")
        return
    send_message(chat_id, "Команда не распознана. Используйте /help")
