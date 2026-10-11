from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class CommerceSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[2] / ".env",
        extra="ignore",
        env_ignore_empty=True,
    )
    POSTGRES_HOST: str = "127.0.0.1"
    POSTGRES_PORT: int = 55432
    POSTGRES_USER: str = "commerce"
    POSTGRES_PASSWORD: SecretStr
    POSTGRES_DB: str = "agent_commerce_demo"
    SESSION_HOURS: int = 8
    COOKIE_SECURE: bool = False
    BUSINESS_ORIGINS: str = "http://127.0.0.1:8080,http://localhost:8080"
    ASYNC_TRANSPORT: Literal['postgres', 'kafka'] = "postgres"
    KAFKA_BOOTSTRAP_SERVERS: str = "127.0.0.1:59092"
    KAFKA_TOPIC: str = "commerce.tasks.v1"
    KAFKA_GROUP: str = "commerce-workers-v1"
    REDIS_URL: str = "redis://127.0.0.1:56379/0"
    OTEL_EXPORTER_OTLP_ENDPOINT: str = ""
    BUSINESS_BACKEND: Literal['database', 'http'] = 'database'
    SIM_BUSINESS_URL: str = 'http://127.0.0.1:8081'
    SIM_FAILURE_MODE: Literal['none', 'before', 'after', 'timeout'] = 'none'
    ADMIN_STAFF_IDS: str = 'STAFF-001'
    MAIL_BACKEND: Literal['file', 'smtp'] = 'file'
    MAIL_DIR: Path = Path(__file__).resolve().parents[2] / '.runtime' / 'mail'
    PUBLIC_APP_URL: str = 'http://127.0.0.1:8080/app/'
    SMTP_HOST: str = ''
    SMTP_PORT: int = 587
    SMTP_USER: str = ''
    SMTP_PASSWORD: SecretStr = SecretStr('')
    SMTP_FROM: str = 'service@qinghe.local'

    def connection_kwargs(self) -> dict:
        return {
            "host": self.POSTGRES_HOST,
            "port": self.POSTGRES_PORT,
            "user": self.POSTGRES_USER,
            "password": self.POSTGRES_PASSWORD.get_secret_value(),
            "dbname": self.POSTGRES_DB,
            "connect_timeout": 5,
        }
