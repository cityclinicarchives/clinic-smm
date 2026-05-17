# Clinic SMM Manager

Минимальный стартовый каркас FastAPI для автоматизированного SMM-менеджера клиники.

## Локальный запуск

```bash
pip install -r requirements.txt
copy .env.example .env
uvicorn app.main:app --reload
```

Проверка:

```text
http://127.0.0.1:8000/health
```

Ожидаемый ответ:

```json
{
  "status": "ok",
  "service": "clinic-smm-manager",
  "environment": "local"
}
```

## Railway

Railway запускает проект командой из `railway.json`:

```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```
