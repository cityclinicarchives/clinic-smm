# clinic-smm-manager v23

Версия v23 добавляет новую архитектуру для сложных инфографик:

```text
/analyze_asset
→ router определяет тип контента
→ specialized prompt строит structured blueprint
→ component-based infographic reconstruction
→ reference image используется как источник сильных визуальных элементов
→ новая картинка + пост создаются из одного blueprint
```

## Главное изменение v23

Для инфографик теперь используется не простой режим «создай новую картинку», а компонентная реконструкция:

```text
исходная инфографика
↓
разбор на блоки
↓
для каждого блока: сохранить / очистить / заменить / создать новый
↓
сборка новой инфографики
```

Пример с укусами насекомых:

```text
header — новый аккуратный заголовок
блоки комар/муравей/клещ/... — сохранить хорошие визуальные элементы из исходника
скорпион — заменить на слепня/мошку
warning block — добавить признаки, когда нужен врач
action block — добавить безопасные действия после укуса
footer — профилактика
```

## Команды

```text
/analyze_asset
```
Загрузить исходный материал: инфографику, мем, скриншот, пост, картинку с подписью.

```text
/reconstruct_asset ID
```
Создать structured reconstruction blueprint.

```text
/create_from_reconstruction ID
```
Создать текстовый пост из реконструкции.

```text
/create_full_from_reconstruction ID
```
Создать пост + картинку из реконструкции.

## Что заменить на GitHub

```text
README.md
app/prompts/infographic.py
app/routers/telegram.py
app/services/reconstruction_engine.py
```

## Что добавить на GitHub

```text
app/services/component_infographic_engine.py
```

После деплоя снова выполните в Swagger:

```text
POST /telegram/set-webhook
```
