# ADR 0008: Admission control локального Ollama

- Статус: принято
- Дата: 2026-10-20

Все generate/embed проходят ModelExecutionGateway в общем OllamaProvider.
Redis хранит ограниченную очередь ожидания и token lease на endpoint.
Capacity=1, TTL lease превышает HTTP read timeout и продлевается во время
запроса. При недоступности Redis новые inference-запросы отклоняются.
Интерактивные запросы получают преимущество 30 секунд; возраст фоновых
постепенно перекрывает это преимущество. Ожидание ограничено deadline.
Отмена ожидающего вызова удаляет билет; истёкшие билеты очищаются при admission.
Release/renew допускаются только владельцу token. При потере lease результат
не принимается. Для одного сервера Ollama дополнительно OLLAMA_NUM_PARALLEL=1
и OLLAMA_MAX_LOADED_MODELS=1: lease не может остановить уже принятый сервером
HTTP-запрос после сетевого разделения.

Отдельные очереди model_interactive/model_background обслуживает model worker
concurrency=1/prefetch=1. FFmpeg/IO и пользовательский render обслуживают
другие worker-ы. API-поиск тоже проходит gateway; интерфейс поиска сохраняется.
Scheduler-контейнер содержит control worker и отдельный beat-процесс (`-B`);
очередь `control` обслуживает только диспетчер. Поэтому долгий FFmpeg не
задерживает продвижение родительских сценариев. Запускается один scheduler.
Не строим отдельный inference-кластер и автоматический model affinity/batching
до измерений. Реестр endpoint-ов расширяется через ключ, зависящий от URL.
