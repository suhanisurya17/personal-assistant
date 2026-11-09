from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"
    google_credentials_file: str = "credentials.json"
    google_token_file: str = "token.json"
    google_timezone: str = "America/New_York"
    database_path: str = "assistant.db"
    # Gmail "from:" values for the news widget. Override in .env as JSON, e.g.
    # NEWS_SENDERS='["tldrnewsletter.com", "\"TLDR by Wealthsimple\""]'
    news_senders: list[str] = ['"TLDR by Wealthsimple"', "tldrnewsletter.com"]
    day_start_hour: int = 8
    day_end_hour: int = 22
    # Google Photos widget: separate sign-in so Gmail/Calendar are never affected.
    photos_token_file: str = "photos_token.json"
    photos_dir: str = "data/photos"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
