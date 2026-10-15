# Easy Cut API

Минимальный FastAPI-компонент. Сейчас предоставляет только проверку состояния
процесса; доменная логика будет добавляться по мере реализации вертикальных
срезов.

Каталог подготовлен так, чтобы его можно было перенести в отдельный репозиторий
и затем подключить в корневой проект как Git submodule.

## Локальная PostgreSQL

Создайте рабочий env-файл и запустите БД:

```powershell
Copy-Item .env.database.example .env.database
docker compose -f docker-compose.dev.yml up -d database
```

PostgreSQL доступен на `127.0.0.1:5432`. Состояние хранится в отдельном Docker
volume `easy-cut-api_postgres-data` и не пересекается с корневым Compose.

Миграции запускаются из каталога `api`:

```powershell
uv run alembic upgrade head
```

Создание следующей миграции:

```powershell
uv run alembic revision --autogenerate -m "описание"
uv run black sql migrations
```

