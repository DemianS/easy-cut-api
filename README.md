# Easy Cut API

FastAPI-компонент и Celery worker. API запускает индексирование и показывает
состояние фоновых задач, а worker выполняет `easy_cut.library_scan` и
`easy_cut.asset_probe` с помощью FFmpeg/ffprobe.

Репозиторий подключается к orchestration-проекту как Git submodule. Точка входа
`main.py`, SQL-слой `sql/` и каталог миграций `migrations/` расположены
непосредственно в корне репозитория.

Dockerfile содержит два target:

- `api` запускает `uvicorn main:app` и не содержит FFmpeg;
- `worker` запускает `celery -A worker:celery_app worker` и содержит FFmpeg.

`POST /media-roots/{media_root_id}/scan` создаёт `library_scan` либо возвращает
уже активную задачу для этого корня. `GET /jobs/{job_id}` возвращает её статус,
прогресс, результат и диагностическую ошибку.

`GET /assets` возвращает страницу проиндексированной медиабиблиотеки. Доступны
фильтры `status`, `media_kind`, `media_root_id`, `is_available`, поиск по пути
через `search`, сортировка `sort_by`/`sort_order` и пагинация `limit`/`offset`.
`GET /assets/{asset_id}` возвращает полные технические метаданные и все
известные расположения файла.

`library_scan` проверяет, что путь из связанного `media_root` находится внутри
`MEDIA_ROOT_PATH`, рекурсивно обнаруживает видео, изображения и аудио,
идемпотентно обновляет `assets` и `asset_locations`, а также отмечает пропавшие
файлы без удаления записей. Символические ссылки намеренно не индексируются.
Если часть дерева прочитать не удалось, задача сохраняет диагностику и не
помечает отсутствующие файлы до следующего полного прохода.

Для новых, изменённых и ранее не разобранных активов сканер создаёт отдельные
`asset_probe` jobs. Они вызывают `ffprobe` без shell, проверяют, что файл не
изменился во время анализа, и сохраняют контейнер, длительность, кодеки, FPS,
размер кадра и параметры аудио. Таймаут задаётся через
`FFPROBE_TIMEOUT_SECONDS`, по умолчанию — 120 секунд.

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

Для общей Compose-среды миграцию можно выполнить внутри API-контейнера:

```powershell
docker compose -f ../docker-compose.dev.yml --profile app run --rm api `
  uv run alembic upgrade head
```

Создание следующей миграции:

```powershell
uv run alembic revision --autogenerate -m "описание"
uv run black sql migrations
```

