# Clinic SMM Manager

Минимальный backend для автоматизированного SMM-менеджера клиники.

## Локальный запуск

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Проверка:

```text
http://127.0.0.1:8000/health
```

## Переменные окружения

В `.env` нужно указать:

```env
APP_NAME=clinic-smm-manager
ENVIRONMENT=local
DATABASE_URL=postgresql://...
```
