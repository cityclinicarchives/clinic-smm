# clinic-smm-manager — v40 step14 Telegram v40 launch

Эта версия подключает Telegram-управление к новому автоматическому pipeline v40.

## Главное изменение

Старые кнопки реконструкции больше не запускают legacy crop/bbox pipeline.

Теперь при загрузке инфографики через Telegram:

```text
/analyze_asset
```

бот сохраняет исходник и, если материал похож на инфографику/визуальный материал, показывает кнопку:

```text
🧠 Реконструировать v40
```

При нажатии запускается новый endpoint-эквивалент:

```text
POST /assets/{asset_id}/run-full-reconstruction
```

То есть автоматически выполняется:

```text
master reconstruction
image task prepare
image task execute
component QA + repair loop
final layout refinement
technical render
draft QA + layout repair loop
optional design polish
final QA
post generation
```

## Новая команда Telegram

```text
/run_full_reconstruction ID
```

Например:

```text
/run_full_reconstruction 3
```

## На GitHub заменить

```text
README.md
app/routers/telegram.py
```

## После деплоя

Выполнить:

```text
POST /telegram/set-webhook
```
