"""Фоновая индексация описаний планов в pgvector."""

from dataclasses import dataclass
from datetime import UTC, datetime
import logging
from time import monotonic
from uuid import UUID

from sqlalchemy import select

from services.embedding_provider import EmbeddingProviderError
from services.embedding_provider import TextEmbeddingProvider
from services.logging_config import log_event, LogStatus
from services.embedding_provider import get_embedding_settings
from sql.database import session_scope
from sql.enums import AssetStatus, DescriptionStatus, EmbeddingStatus
from sql.enums import JobStatus, JobType
from sql.models import Asset, Job, Shot, ShotDescription, ShotEmbedding

logger = logging.getLogger(__name__)

TEXT_VERSION = "shot-temporal-description-text-v2"
MAX_ERROR_MESSAGE_LENGTH = 2000


class AssetEmbeddingError(RuntimeError):
    """Безопасная ошибка задачи индексации описаний."""


class InvalidAssetEmbeddingJobError(AssetEmbeddingError):
    """Задача или её входные данные недоступны."""


@dataclass(frozen=True, slots=True)
class EmbeddingContext:
    embedding_id: UUID
    indexed_text: str


def build_indexed_text(description: ShotDescription) -> str:
    """Собрать стабильный поисковый документ из проверенного описания."""
    sections: list[str] = []
    if description.summary:
        sections.append(description.summary.strip())
    if description.visible_objects:
        objects = ", ".join(item.strip() for item in description.visible_objects)
        sections.append(f"Объекты: {objects}")
    fields = (
        ("Действие", description.action),
        ("Обстановка", description.setting),
        ("Тип плана", description.shot_type),
        ("Движение камеры", description.camera_motion),
        ("Движение объектов", description.object_motion),
    )
    for label, value in fields:
        if value:
            sections.append(f"{label}: {value.strip()}")
    if description.visible_text:
        visible_text = ", ".join(item.strip() for item in description.visible_text)
        sections.append(f"Текст в кадре: {visible_text}")
    if description.stable_objects:
        stable_objects = ", ".join(item.strip() for item in description.stable_objects)
        sections.append(f"Устойчивые объекты: {stable_objects}")
    if description.temporal_events:
        for event in description.temporal_events:
            event_text = str(event.get("description", "")).strip()
            if event_text:
                sections.append(
                    "Событие "
                    f"[{event.get('start_ms')}, {event.get('end_ms')}): {event_text}"
                )
    return "\n".join(section for section in sections if section).strip()


async def _prepare_embeddings(
    job_id: UUID,
    celery_task_id: str | None,
) -> tuple[UUID | None, list[EmbeddingContext], int, dict | None]:
    settings = get_embedding_settings()
    async with session_scope() as session:
        job = await session.scalar(
            select(Job).where(Job.id == job_id).with_for_update()
        )
        if job is None or job.job_type != JobType.ASSET_EMBEDDING.value:
            raise InvalidAssetEmbeddingJobError("Задача индексации не найдена")
        if job.status == JobStatus.SUCCEEDED:
            return None, [], 0, job.result or {}
        if job.status == JobStatus.CANCELLED:
            return None, [], 0, {"status": JobStatus.CANCELLED.value}
        if job.asset_id is None:
            raise InvalidAssetEmbeddingJobError("В задаче отсутствует asset")

        asset = await session.get(Asset, job.asset_id)
        if asset is None:
            raise InvalidAssetEmbeddingJobError("Медиаактив не найден")
        descriptions = list(
            await session.scalars(
                select(ShotDescription)
                .join(Shot, Shot.id == ShotDescription.shot_id)
                .where(
                    Shot.asset_id == asset.id,
                    ShotDescription.status == DescriptionStatus.SUCCEEDED,
                )
                .order_by(Shot.sequence)
            )
        )
        if not descriptions:
            raise InvalidAssetEmbeddingJobError(
                "Нет успешно сформированных описаний планов"
            )

        contexts: list[EmbeddingContext] = []
        reused = 0
        for description in descriptions:
            indexed_text = build_indexed_text(description)
            if not indexed_text:
                raise InvalidAssetEmbeddingJobError(
                    "Описание плана не содержит индексируемого текста"
                )
            embedding = await session.scalar(
                select(ShotEmbedding).where(
                    ShotEmbedding.shot_description_id == description.id,
                    ShotEmbedding.provider == TextEmbeddingProvider.provider_name,
                    ShotEmbedding.model == settings.model,
                    ShotEmbedding.text_version == TEXT_VERSION,
                )
            )
            if embedding is None:
                embedding = ShotEmbedding(
                    shot_description_id=description.id,
                    provider=TextEmbeddingProvider.provider_name,
                    model=settings.model,
                    text_version=TEXT_VERSION,
                    dimension=settings.dimensions,
                    indexed_text=indexed_text,
                )
                session.add(embedding)
                await session.flush()
            elif (
                embedding.status == EmbeddingStatus.SUCCEEDED
                and embedding.indexed_text == indexed_text
                and embedding.dimension == settings.dimensions
                and embedding.embedding is not None
            ):
                reused += 1
                continue
            else:
                embedding.status = EmbeddingStatus.PENDING
                embedding.dimension = settings.dimensions
                embedding.indexed_text = indexed_text
                embedding.embedding = None
                embedding.error_code = None
                embedding.error_message = None
            contexts.append(EmbeddingContext(embedding.id, indexed_text))

        now = datetime.now(UTC)
        job.status = JobStatus.RUNNING
        job.celery_task_id = celery_task_id or job.celery_task_id
        job.progress_current = reused
        job.progress_total = len(descriptions)
        job.result = None
        job.error_code = None
        job.error_message = None
        job.started_at = now
        job.heartbeat_at = now
        job.finished_at = None
        asset.status = AssetStatus.ANALYZING
        asset.last_attempt_at = now
        return asset.id, contexts, reused, None


async def _mark_embedding_running(embedding_id: UUID) -> None:
    async with session_scope() as session:
        embedding = await session.get(ShotEmbedding, embedding_id)
        if embedding is not None:
            embedding.status = EmbeddingStatus.RUNNING
            embedding.last_attempt_at = datetime.now(UTC)


async def _store_embedding_success(
    job_id: UUID,
    embedding_id: UUID,
    vector: list[float],
) -> None:
    async with session_scope() as session:
        embedding = await session.get(ShotEmbedding, embedding_id)
        job = await session.get(Job, job_id)
        if embedding is None or job is None:
            raise InvalidAssetEmbeddingJobError("Embedding или задача исчезли")
        embedding.status = EmbeddingStatus.SUCCEEDED
        embedding.embedding = vector
        embedding.error_code = None
        embedding.error_message = None
        job.progress_current += 1
        job.heartbeat_at = datetime.now(UTC)


async def _store_embedding_failure(
    job_id: UUID,
    embedding_id: UUID,
    error: Exception,
) -> None:
    async with session_scope() as session:
        log_event(
            logger,
            logging.WARNING,
            "shot_embedding",
            LogStatus.FAILED,
            "Построение embedding завершилось ошибкой",
            stage="embed",
            job_id=str(job_id),
            embedding_id=str(embedding_id),
            error_type=type(error).__name__,
        )
        embedding = await session.get(ShotEmbedding, embedding_id)
        job = await session.get(Job, job_id)
        if embedding is not None:
            embedding.status = EmbeddingStatus.FAILED
            embedding.error_code = "embedding_provider_failed"
            embedding.error_message = str(error)[:MAX_ERROR_MESSAGE_LENGTH]
        if job is not None:
            job.progress_current += 1
            job.heartbeat_at = datetime.now(UTC)


async def _complete_embeddings(
    job_id: UUID,
    asset_id: UUID,
    reused: int,
    failed_embedding_ids: list[str],
) -> dict:
    async with session_scope() as session:
        job = await session.get(Job, job_id)
        asset = await session.get(Asset, asset_id)
        if job is None or asset is None:
            raise InvalidAssetEmbeddingJobError("Задача или актив исчезли")
        failed = len(failed_embedding_ids)
        result = {
            "asset_id": str(asset_id),
            "indexed": job.progress_current - reused - failed,
            "reused": reused,
            "failed": failed,
            "failed_embedding_ids": failed_embedding_ids,
        }
        now = datetime.now(UTC)
        job.result = result
        job.heartbeat_at = now
        job.finished_at = now
        if failed:
            job.status = JobStatus.FAILED
            job.error_code = "partial_embedding_failure"
            job.error_message = "Не удалось построить часть embeddings"
            asset.status = AssetStatus.FAILED
            asset.last_error_code = job.error_code
            asset.last_error_message = job.error_message
        else:
            job.status = JobStatus.SUCCEEDED
            asset.status = AssetStatus.READY
            asset.last_error_code = None
            asset.last_error_message = None
        return result


async def _mark_job_failed(job_id: UUID, error: Exception) -> None:
    async with session_scope() as session:
        job = await session.get(Job, job_id)
        if job is None or job.status in {JobStatus.SUCCEEDED, JobStatus.CANCELLED}:
            return
        now = datetime.now(UTC)
        job.status = JobStatus.FAILED
        job.error_code = "asset_embedding_failed"
        job.error_message = str(error)[:MAX_ERROR_MESSAGE_LENGTH]
        job.heartbeat_at = now
        job.finished_at = now
        if job.asset_id is not None:
            asset = await session.get(Asset, job.asset_id)
            if asset is not None:
                asset.status = AssetStatus.FAILED
                asset.last_error_code = job.error_code
                asset.last_error_message = job.error_message


async def execute_asset_embedding(
    job_id: UUID,
    celery_task_id: str | None = None,
) -> dict:
    """Идемпотентно построить embeddings описаний планов актива."""
    started_at = monotonic()
    log_event(
        logger,
        logging.INFO,
        "asset_embedding",
        LogStatus.STARTED,
        "Построение embeddings актива начато",
        stage="prepare",
        job_id=str(job_id),
    )
    try:
        settings = get_embedding_settings()
        provider = TextEmbeddingProvider(settings)
        asset_id, contexts, reused, completed_result = await _prepare_embeddings(
            job_id,
            celery_task_id,
        )
        if completed_result is not None:
            log_event(
                logger,
                logging.INFO,
                "asset_embedding",
                LogStatus.REUSED,
                "Готовые embeddings актива переиспользованы",
                stage="prepare",
                job_id=str(job_id),
            )
            return completed_result
        if asset_id is None:
            raise InvalidAssetEmbeddingJobError("Не удалось подготовить задачу")

        failed_embedding_ids: list[str] = []
        for context in contexts:
            await _mark_embedding_running(context.embedding_id)
            try:
                vector = provider.embed(context.indexed_text)
                await _store_embedding_success(job_id, context.embedding_id, vector)
                log_event(
                    logger,
                    logging.DEBUG,
                    "shot_embedding",
                    LogStatus.SUCCEEDED,
                    "Embedding описания успешно сохранён",
                    stage="embed",
                    job_id=str(job_id),
                    embedding_id=str(context.embedding_id),
                )
            except EmbeddingProviderError as error:
                failed_embedding_ids.append(str(context.embedding_id))
                await _store_embedding_failure(job_id, context.embedding_id, error)
        result = await _complete_embeddings(
            job_id,
            asset_id,
            reused,
            failed_embedding_ids,
        )
        final_status = LogStatus.FAILED if result["failed"] else LogStatus.SUCCEEDED
        log_event(
            logger,
            logging.WARNING if result["failed"] else logging.INFO,
            "asset_embedding",
            final_status,
            "Построение embeddings актива завершено",
            stage="store",
            job_id=str(job_id),
            asset_id=str(asset_id),
            completed=result["indexed"],
            failed=result["failed"],
            elapsed_ms=round((monotonic() - started_at) * 1000),
        )
        return result
    except Exception as error:
        await _mark_job_failed(job_id, error)
        log_event(
            logger,
            logging.ERROR,
            "asset_embedding",
            LogStatus.FAILED,
            "Построение embeddings актива завершилось ошибкой",
            stage="execution",
            exc_info=True,
            job_id=str(job_id),
            elapsed_ms=round((monotonic() - started_at) * 1000),
            error_type=type(error).__name__,
            error_code=getattr(error, "error_code", "asset_embedding_failed"),
        )
        raise
