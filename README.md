# Easy Cut API

FastAPI-компонент и Celery worker. API предоставляет проверку состояния
процесса, а worker содержит FFmpeg/ffprobe и задачу `easy_cut.library_scan` для
индексации медиатеки.

Репозиторий подключается к orchestration-проекту как Git submodule. Точка входа
`main.py`, SQL-слой `sql/` и каталог миграций `migrations/` расположены
непосредственно в корне репозитория.

Dockerfile содержит два target:

- `api` запускает `uvicorn main:app` и не содержит FFmpeg;
- `worker` запускает `celery -A worker:celery_app worker` и содержит FFmpeg.

`library_scan` принимает UUID заранее созданной записи `jobs` типа
`library_scan`. Задача проверяет, что путь из связанного `media_root` находится
внутри `MEDIA_ROOT_PATH`, рекурсивно обнаруживает видео, изображения и аудио,
идемпотентно обновляет `assets` и `asset_locations`, а также отмечает пропавшие
файлы без удаления записей. Состояние, прогресс и диагностическая ошибка
сохраняются в `jobs`. Символические ссылки намеренно не индексируются. Если
часть дерева прочитать не удалось, задача сохранит диагностику и не будет
помечать отсутствующие файлы до следующего полного прохода.

На этом этапе задача не вызывает `ffprobe`: технический анализ будет отдельной
фоновой задачей `asset_probe`, чтобы сканирование оставалось быстрым и могло
безопасно перезапускаться.

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

