from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    telegram_token: str = Field(..., env="TELEGRAM_TOKEN")
    telegram_webhook_path: str = Field("/telegram/korus-feedback", env="TELEGRAM_WEBHOOK_PATH")
    telegram_webhook_secret: str = Field("change-me", env="TELEGRAM_WEBHOOK_SECRET")
    admin_chat_id: int | None = Field(default=None, env="ADMIN_CHAT_ID")
    admin_contact: str = Field(default="@your_admin", env="ADMIN_CONTACT")

    sheets_webhook_url: str = Field(
        "https://script.google.com/macros/s/AKfycbyJeu_IX_xr8QooEu6ipdmcTqiWMNbGTpoBc6M23txqA5HYVrv7thmA7fLxFnzrH2Ycrw/exec",
        env="SHEETS_WEBHOOK_URL",
    )
    sheets_webhook_key: str = Field("", env="SHEETS_WEBHOOK_KEY")

    friendwork_secret: str = Field("fw_korus_feedback_2025_secret", env="FRIENDWORK_SECRET")

    speech_language_code: str = Field("en-US", env="SPEECH_LANGUAGE_CODE")

    reminder_minutes: int = Field(180, env="REMINDER_MINUTES")


def load_settings() -> Settings:
    return Settings()
