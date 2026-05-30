from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "clinic-smm-manager"
    environment: str = "local"

    database_url: str

    openai_api_key: str = ""
    openai_model: str = "gpt-5.5"
    openai_image_model: str = "gpt-image-1"

    # Расчетная стоимость OpenAI-вызовов. Можно переопределить в Railway Variables.
    cost_tracking_enabled: bool = True
    cost_text_input_usd_per_1m: float = 0.0
    cost_text_output_usd_per_1m: float = 0.0
    cost_image_1024_usd: float = 0.0

    telegram_bot_token: str = ""
    admin_telegram_id: str = ""
    public_base_url: str = ""
    webhook_base_url: str = ""

    # ID тестовой группы или будущего канала, куда бот публикует одобренные посты.
    # Для группы обычно выглядит как -1001234567890.
    telegram_publish_chat_id: str = ""


    # Папка для временного хранения сгенерированных изображений.
    generated_images_dir: str = "storage/generated_images"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
