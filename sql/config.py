"""Настройки подключения к PostgreSQL."""

from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import URL

DEFAULT_ENV_FILE = Path(__file__).resolve().parents[1] / ".env.database"


@dataclass(frozen=True, slots=True)
class DatabaseSettings:
    """Параметры PostgreSQL, получаемые из переменных окружения."""

    database: str
    username: str
    password: str
    host: str
    port: int

    @classmethod
    def from_environment(cls) -> "DatabaseSettings":
        """Загрузить локальный env-файл и прочитать настройки PostgreSQL."""
        load_dotenv(DEFAULT_ENV_FILE, override=False)

        password = os.getenv("POSTGRES_PASSWORD")
        if not password:
            raise RuntimeError("Переменная POSTGRES_PASSWORD не задана")

        return cls(
            database=os.getenv("POSTGRES_DB", "easy_cut"),
            username=os.getenv("POSTGRES_USER", "easy_cut"),
            password=password,
            host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
            port=int(os.getenv("POSTGRES_PORT", "5432")),
        )

    @property
    def url(self) -> URL:
        """Построить URL для асинхронного драйвера без ручного escaping."""
        return URL.create(
            drivername="postgresql+asyncpg",
            username=self.username,
            password=self.password,
            host=self.host,
            port=self.port,
            database=self.database,
        )
