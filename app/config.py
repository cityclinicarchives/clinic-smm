from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "clinic-smm-manager"
    environment: str = "local"

    database_url: str

    openai_api_key: str = ""
    openai_model: str = "gpt-4.1-mini"
    openai_image_model: str = "gpt-image-1"

    telegram_bot_token: str = ""
    admin_telegram_id: str = ""
    public_base_url: str = ""

    # ID тестовой группы или будущего канала, куда бот публикует одобренные посты.
    # Для группы обычно выглядит как -1001234567890.
    telegram_publish_chat_id: str = ""

    # Разделитель между разными опубликованными постами в тестовой группе/канале.
    # По умолчанию это невидимый символ, который визуально создает промежуток.
    telegram_publish_separator_enabled: bool = True
    telegram_publish_separator: str = "ㅤ"

    # Папка для временного хранения сгенерированных изображений.
    generated_images_dir: str = "storage/generated_images"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
