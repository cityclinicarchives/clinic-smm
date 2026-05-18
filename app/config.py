from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "clinic-smm-manager"
    environment: str = "local"

    database_url: str

    openai_api_key: str = ""
    openai_model: str = "gpt-4.1-mini"

    telegram_bot_token: str = ""
    admin_telegram_id: str = ""
    public_base_url: str = ""

    # ID тестовой группы или будущего канала, куда бот публикует одобренные посты.
    # Для группы обычно выглядит как -1001234567890.
    telegram_publish_chat_id: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
