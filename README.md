# Easy Cut API

FastAPI-компонент, Celery worker и периодический диспетчер. API запускает
индексирование и показывает состояние фоновых задач, worker выполняет этапы
анализа, а Celery beat продвигает составные задачи и восстанавливает потерянные
доставки.

API и worker пишут безопасные структурированные JSON-логи в stdout. Если задан
`LOG_FILE`, запись дублируется в ротируемый JSONL-файл. Контекст обработки
связывается полями `request_id`, `task_id`, `job_id`, `asset_id` и `shot_id`;
тела запросов, query string, промпты и содержимое медиа не логируются.

Каждое прикладное событие имеет `operation`, машинное имя `event` в формате
`operation.status`, текущий `stage` и унифицированный `status`. Основной цикл:
`started → progress → succeeded/failed`; постановка и восстановление задач
дополнительно используют `dispatched`, `reused` и `retrying`. Поэтому поиск
проблемы не зависит от текста русскоязычного `message`.

Репозиторий подключается к orchestration-проекту как Git submodule. Точка входа
`main.py`, SQL-слой `sql/` и каталог миграций `migrations/` расположены
непосредственно в корне репозитория.

Dockerfile содержит два target:

- `api` запускает `uvicorn main:app` и не содержит FFmpeg;
- `worker` запускает `celery -A worker:celery_app worker` и содержит FFmpeg.

`POST /media-roots/{media_root_id}/scan` создаёт `library_scan` либо возвращает
уже активную задачу для этого корня. `GET /jobs/{job_id}` возвращает её статус,
прогресс, результат и диагностическую ошибку.

`POST /media-roots/{media_root_id}/prepare` создаёт родительский
`library_preparation` и одной командой запускает `scan → probe → shot_detection
→ asset_description → asset_embedding`. Изображения и аудио проходят
технический probe, но пока явно пропускаются на визуальных этапах.

Контейнер API после миграций запускает `python -m sql.bootstrap`. Команда
идемпотентно создаёт локального пользователя и корень `MEDIA_ROOT_PATH`. Для
свежей базы стандартный ID корня —
`00000000-0000-4000-8000-000000000002`. Существующая запись с тем же путём
переиспользуется без изменения имени, владельца и состояния. Bootstrap можно
отключить через `BOOTSTRAP_MEDIA_ROOT=false`.

`GET /assets` возвращает страницу проиндексированной медиабиблиотеки. Доступны
фильтры `status`, `media_kind`, `media_root_id`, `is_available`, поиск по пути
через `search`, сортировка `sort_by`/`sort_order` и пагинация `limit`/`offset`.
`GET /assets/{asset_id}` возвращает полные технические метаданные и все
известные расположения файла.

`POST /assets/{asset_id}/shots/detect` запускает PySceneDetect и извлечение
упорядоченного набора кадров через FFmpeg. Короткий план получает один средний
кадр, обычный — `start/middle/end`, длинный — дополнительные равномерные точки
в пределах `FRAME_SAMPLING_MAX_FRAMES`. Минимальный шаг, порог короткого плана
и интервал настраиваются через `FRAME_SAMPLING_*`. Артефакты именуются
`{timestamp_ms}_{role}.jpg` и повторно не декодируются, если уже существуют.
`GET /assets/{asset_id}/shots` возвращает интервалы `[start_ms, end_ms)`,
порядок, роль, причину и версию выборки кадров. Исходное видео не изменяется.

`POST /assets/{asset_id}/describe` описывает все кадры плана одним временным
VLM-вызовом через явно включённый
локальный Ollama provider. По умолчанию `VISION_PROVIDER=disabled`, поэтому
никакие кадры не отправляются модели. Для включения задайте
`VISION_PROVIDER=ollama`, `OLLAMA_VISION_MODEL` и локальный `OLLAMA_BASE_URL`.
Общие `wait_for_ollama`, `pull_model`, `generate` и `embed` реализованы в одном
`OllamaProvider` через `requests`. `generate` получает единый ответ без
streaming для последующей строгой валидации. Текст prompt хранится отдельно в
`prompts/shot_description.ru.txt`, а его версия вычисляется по содержимому.
Ответ содержит `FrameObservation`, переходы соседних кадров, временные события
и evidence. Backend проверяет порядок и известность frame ID, исходные
timestamp и попадание всех интервалов в границы плана. Вместе с результатом
хранятся версии prompt, схемы и sampling-политики.
До появления алгоритмического motion-анализатора поля движения обязаны быть
`null`: backend отклоняет попытку VLM самостоятельно утверждать движение
камеры или объектов.

`POST /assets/{asset_id}/embeddings` запускает идемпотентное построение
embeddings успешно описанных планов. По умолчанию provider отключён; для
включения задайте `EMBEDDING_PROVIDER=ollama`, `OLLAMA_EMBEDDING_MODEL` и
локальный `OLLAMA_BASE_URL`. Первая схема зафиксирована на 768 измерениях.

`POST /search/shots` преобразует текстовый запрос в embedding и выполняет
точный cosine search через pgvector. Ответ содержит `asset_id`, `shot_id` и
полуоткрытый интервал `[start_ms, end_ms)`. HNSW пока намеренно не создаётся:
его следует добавлять после измерений на реальной библиотеке.

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

После `ffprobe` задача потоково рассчитывает полный SHA-256 блоками по 8 MiB.
Во время долгого чтения обновляется heartbeat, а версия файла повторно
проверяется перед сохранением. Совпавшие digest связываются через
`duplicate_of_asset_id`; исходные файлы при этом не удаляются и не
перемещаются.

Все Celery-задачи с `job_id` используют общий `DatabaseJobTask`. Если
необработанное исключение возникло до предметного обработчика, защитный слой
переводит job в `failed`, сохраняет безопасный `unhandled_task_error` и
завершает heartbeat. Уже сохранённая предметная ошибка не перезаписывается.

Периодический `easy_cut.reconcile_jobs` не ждёт дочерние задачи внутри worker:
каждый запуск делает один короткий идемпотентный проход. Интервал, grace period,
таймаут heartbeat и предел восстановлений задаются переменными
`JOB_DISPATCH_INTERVAL_SECONDS`, `JOB_PENDING_GRACE_SECONDS`,
`JOB_HEARTBEAT_TIMEOUT_SECONDS` и `JOB_MAX_RECOVERY_ATTEMPTS`.

## Локальная PostgreSQL + pgvector

Создайте рабочий env-файл и запустите БД:

```powershell
Copy-Item .env.database.example .env.database
docker compose -f docker-compose.dev.yml up -d database
```

PostgreSQL доступен на `127.0.0.1:5432`. Образ уже содержит pgvector, а
расширение `vector` включается миграцией. Состояние хранится в отдельном Docker
volume `easy-cut-api_postgres-data` и не пересекается с корневым Compose.

Миграции запускаются из каталога `api`:

```powershell
uv run alembic upgrade head
```

Для общей Compose-среды миграцию можно выполнить внутри API-контейнера:

```powershell
docker compose -f ../docker-compose.dev.yml run --rm api `
  uv run alembic upgrade head
```

Создание следующей миграции:

```powershell
uv run alembic revision --autogenerate -m "описание"
uv run black sql migrations
```

