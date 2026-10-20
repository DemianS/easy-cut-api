# Первый монтаж по промпту

Сценарий работает через API: LLM планирует поисковые секции, backend ищет
готовые описания планов в pgvector, LLM выбирает клипы из подборки, backend
проверяет timeline и рендерит MP4. Можно остановиться после предложения модели,
изменить выбор через API и отдельным запросом запустить рендер. Все промежуточные
результаты доступны по ID запроса; графический редактор пока не реализован.

## Запуск из корневого orchestration-репозитория

В существующий `.env.api` добавьте:

```dotenv
PLANNER_PROVIDER=ollama
PLANNER_TIMEOUT_SECONDS=180
MODEL_QUEUE_TIMEOUT_SECONDS=120
MODEL_QUEUE_MAX_WAITING=32
```

В `.env.ollama` задайте `OLLAMA_PLANNER_MODEL`: локальную instruct-модель,
поддерживающую структурированный JSON. Можно использовать уже скачанную
`qwen3.5:9b`. Bootstrap скачивает новую planner-модель при необходимости.
Существующие настройки vision/embedding должны соответствовать моделям,
которыми подготовлена библиотека. Рабочие env-файлы автоматически не меняются.

```powershell
docker compose -f docker-compose.dev.yml up -d --build api worker model-worker render-worker scheduler
```

API применяет миграции при запуске. Миграции создают `projects`, `montage_runs`,
`timeline_versions`, `montage_renders` и расширяют типы jobs. Версии хранят
снимки кандидатов и выбора, render ссылается на конкретную версию. Код старых worker-ов
не поддерживает новые стадии, поэтому пересоберите и перезапустите все
перечисленные сервисы вместе.

Worker обрабатывает обычные IO/FFmpeg-задачи; model-worker — очереди
`model_interactive,model_background` с concurrency=1/prefetch=1; render-worker —
`user_render`. Redis gateway сериализует все generate/embed, включая поиск
через API. Compose задаёт Ollama один параллельный запрос и одну резидентную
модель; переключение больших моделей может заметно увеличить время монтажа.
Scheduler запускает control worker с отдельным beat-процессом и обслуживает
очередь `control`, поэтому продвижение стадий не ждёт окончания тяжёлого FFmpeg.
Запускайте один экземпляр scheduler.

Если медиатека ещё не подготовлена:

```powershell
$baseUrl = 'http://localhost:8080'
$rootId = '00000000-0000-4000-8000-000000000002'
$prepare = Invoke-RestMethod -Method Post -Uri "$baseUrl/media-roots/$rootId/prepare"
Invoke-RestMethod -Uri "$baseUrl/jobs/$($prepare.id)"
```

Дождитесь `succeeded`. Монтаж использует уже построенные описания и embeddings;
самостоятельный массовый анализ исходников при запросе монтажа не запускается.

## Запросить и получить MP4

```powershell
$body = @{
    prompt = 'Собери 30 секунд про работу в мастерской: общий вид, инструменты, процесс и готовая деталь'
    media_root_id = $rootId
    target_duration_ms = 30000
    min_similarity = 0.2
} | ConvertTo-Json
$bodyBytes = [System.Text.Encoding]::UTF8.GetBytes($body)
$run = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/v1/montages" `
    -ContentType 'application/json; charset=utf-8' -Body $bodyBytes `
    -Headers @{ 'Idempotency-Key' = [guid]::NewGuid().ToString() }
$state = Invoke-RestMethod -Uri "$baseUrl/api/v1/montages/$($run.id)"
$state.job
```

POST возвращает `202`, `id` и `job_id`. Сохраняйте Idempotency-Key для повторов
одного запроса; тот же ключ с другим телом даёт `409`. При заполненном backlog
API возвращает `429`. Статус сначала `pending`, затем стадии `montage_plan`,
`montage_search`, `montage_select`, `montage_render`, наконец `succeeded/failed`.
Диспетчер продвигает их с интервалом `JOB_DISPATCH_INTERVAL_SECONDS`.

GET возвращает `search_plan`, `candidates`, `selection`, `timeline`,
`timeline_version_id`, `provenance`, родительскую job и дочерние `stages`.
В candidates есть similarity, разрешённый интервал и `thumbnail_url`, если
WebP-превью уже создано. Превью доступно только для кадров этой подборки.

Проверяйте состояние повторным GET. Когда `download_url` заполнен:

```powershell
$state = Invoke-RestMethod -Uri "$baseUrl/api/v1/montages/$($run.id)"
Invoke-WebRequest -Uri "$baseUrl$($state.download_url)" -OutFile '.\montage.mp4'
```

Файлы находятся в `data/artifacts/renders/{timeline_version_id}/final.mp4`.
API читает артефакты через read-only volume. Для просмотра timeline достаточно
GET состояния. Ручной выбор и отдельный запуск рендера описаны ниже;
смена профиля экспорта пока не опубликована как отдельная операция.

## Проверить и изменить выбор перед рендером

В `POST /api/v1/montages` передайте `auto_render: false`. Без этого поля
сохраняется прежний автоматический режим. После подготовки `GET /api/v1/montages/{id}`
вернёт `state: awaiting_selection`, подборку `candidates`, первоначальные
`selection` и `timeline_version_id`. Родительская job при этом уже `succeeded`:
она закончила подготовку, а рендер ещё не запрашивался. Повторные проходы
диспетчера не запускают рендер и не расходуют попытки восстановления на ожидание.

В ответе `search_plan.sections` связываются с кандидатами через `section_id`.
Для каждого кандидата есть ID, исходный план, описание, similarity, разрешённые
границы и `thumbnail_url`, если превью существует. Можно принять выбор LLM
сразу: отправить его `timeline_version_id` в запрос рендера ниже.

Для замены кандидатов, изменения порядка или длительности:

```powershell
$state = Invoke-RestMethod -Uri "$baseUrl/api/v1/montages/$($run.id)"
$selection = @{
    base_version_id = $state.timeline_version_id
    clips = @($state.selection.clips | ForEach-Object {
        @{ candidate_id = $_.candidate_id; duration_ms = $_.duration_ms }
    })
}
# Здесь замените candidate_id значениями из candidates, измените порядок
# элементов clips и/или duration_ms. Затем сохраните новую версию:
$selectionJson = $selection | ConvertTo-Json -Depth 10
$updated = Invoke-RestMethod -Method Patch `
    -Uri "$baseUrl/api/v1/montages/$($run.id)/selection" `
    -ContentType 'application/json; charset=utf-8' `
    -Body ([System.Text.Encoding]::UTF8.GetBytes($selectionJson))
```

Каждая правка создаёт неизменяемую версию с `parent_version_id` и `source=user`.
Старый `base_version_id` возвращает `409`: перечитайте состояние и примените
правки к актуальной версии. Неизвестные/повторные кандидаты, выход за границы
и некратная 40 ms длительность дают `422`. Разрешён ручной выбор подмножества
секций и изменение общей длительности в пределах 1–120 секунд.
Точные source-интервалы пока размещает backend по политике MVP.

Рендер запускается отдельным запросом:

```powershell
$renderJson = @{ timeline_version_id = $updated.timeline_version_id } | ConvertTo-Json
$render = Invoke-RestMethod -Method Post `
    -Uri "$baseUrl/api/v1/montages/$($run.id)/renders" `
    -ContentType 'application/json' -Body $renderJson
$renderState = Invoke-RestMethod `
    -Uri "$baseUrl/api/v1/montages/$($run.id)/renders/$($render.id)"
# После job.status=succeeded:
Invoke-WebRequest -Uri "$baseUrl$($renderState.download_url)" -OutFile '.\montage.mp4'
```

Если выбор LLM принят без PATCH, используйте `$state.timeline_version_id`
вместо `$updated.timeline_version_id`. Ответ запуска — `202`, render ID,
job ID и привязанная версия. Повтор активной/успешной версии вернёт тот же ID;
после ошибки можно повторить запрос для новой попытки. Очередь ограничена
32 активными рендерами: заполнение даёт `429`. При сбое брокера job сохраняется,
а диспетчер повторяет доставку. Готовность не зависит от доступности Ollama.

`GET /{id}/versions?limit=20&offset=0` возвращает историю. `GET /{id}` содержит
список `renders` и состояние текущей версии: `preparing`, `awaiting_selection`,
`rendering`, `completed`, `failed`. Каждый render имеет собственную job.
Если изменить выбор во время сборки A, результат A останется доступным по
его render ID, а текущая версия B будет ждать отдельного запроса. Прежний
`GET /{id}/video` отдаёт только опубликованный результат текущей версии.

Изменение поисковых запросов/области пока требует нового MontageRun;
PATCH меняет выбор из уже сохранённой подборки.

## Контракт и ограничения

- Требуется явно выбранный активный `media_root_id`; необязательный
  `asset_ids` сужает область до пересечения с этим корнем. Область фильтруется
  в SQL до ранжирования. Исходники проверяются на диске перед выдачей и рендером.
- Используются только видео, успешно описанные и проиндексированные текущей
  embedding-моделью. Изменённые или пропавшие файлы требуют повторного анализа.
- Целевая длительность 1–120 секунд, до 8 поисковых секций, до 8 кандидатов
  на секцию и до 24 клипов. Длительность округляется к сетке 40 ms.
- Поиск ведётся по описанию целого плана. Одиночный отрезок выбирается по центру;
  при нескольких клипах одного источника заполняются свободные интервалы.
  Точность границ действия внутри плана пока не гарантируется. Similarity —
  близость текста, а не вероятность монтажной пригодности.
- Нехватка материала даёт `insufficient_material` и список непокрытых секций;
  доступная частичная подборка сохраняется. Невалидный JSON или выбор модели
  исправляется максимум один раз, затем задача завершается диагностикой.
- Рендер: MP4/H.264, 1080p/25 FPS/yuv420p, fit/pad, AAC stereo 48 kHz,
  исходный звук либо тишина, прямые склейки. Допуск проверки длительности —
  120 ms. Ограничение попытки — 30 минут и 4 GiB временных файлов.
- Рабочий каталог попытки удаляется после завершения; после аварийного
  завершения очищается при следующем запуске той же версии. Готовые MP4
  хранятся до явной очистки пользователем. Секреты и тексты prompts в логах
  не сохраняются; пользовательский текст и проверенные ответы хранятся в БД.

## Проверки

```powershell
cd api
uv run pytest
uv run flake8
uv run alembic check
```

Тест `tests/services/test_montage_integration.py` требует
`RUN_DATABASE_INTEGRATION=1`, применённых миграций и FFmpeg. Docker target
`test` содержит pytest, httpx и FFmpeg. Он проверяет полный API-сценарий с
управляемой моделью, реальным поиском pgvector и реальным рендером.
Опциональная проверка `test_prompt_to_downloadable_mp4_live` дополнительно
требует `RUN_LIVE_OLLAMA=1`, Redis, Ollama и настроенных planner/embedding моделей.
