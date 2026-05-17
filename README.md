# Clinic SMM Manager

Автоматизированный SMM-менеджер для медицинской клиники.

## Что умеет версия 0.3.0

- FastAPI backend
- PostgreSQL через SQLAlchemy
- хранение постов в базе данных
- генерация текста поста через OpenAI API
- Swagger-документация `/docs`
- проверка сервиса `/health`

## Переменные окружения

Для Railway добавьте в Variables:

```env
APP_NAME=clinic-smm-manager
ENVIRONMENT=production
DATABASE_URL=postgresql://...
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4.1-mini
```

`OPENAI_MODEL` можно позже заменить на другую доступную модель.

## Локальный запуск

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Проверка:

```text
http://127.0.0.1:8000/health
http://127.0.0.1:8000/docs
```

## Тест генерации

Откройте `/docs`, затем `POST /generate-post` и отправьте:

```json
{
  "topic": "Как понять, что организму не хватает витамина D",
  "platform": "telegram",
  "service_offer": "В клинике можно сдать анализ на витамин D и получить консультацию врача."
}
```
