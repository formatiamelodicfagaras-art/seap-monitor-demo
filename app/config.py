from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # Database
    database_url: str = "sqlite+aiosqlite:///./seap_monitor.db"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Auth
    admin_username: str = "admin"
    admin_password: str = "change_me"
    session_secret: str = "change_me_random_secret_key_here"
    session_max_age: int = 604800  # 7 days

    # Email — Microsoft 365 (Graph API OAuth2)
    ms_tenant_id: str = ""
    ms_client_id: str = ""
    ms_client_secret: str = ""
    mail_from: str = ""

    # Claude API
    anthropic_api_key: str = ""

    # App
    app_url: str = "http://localhost:8050"
    scan_interval_hours: int = 6
    storage_path: str = "./storage"

    # Rate limiting
    seap_request_delay: float = 2.0  # seconds between SEAP API requests
    seap_max_retries: int = 3

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
    }


@lru_cache
def get_settings() -> Settings:
    return Settings()
