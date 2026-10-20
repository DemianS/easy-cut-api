"""Ограниченная подборка планов строго внутри области запроса."""

import asyncio
import os
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import func, select

from schemas.montage import Candidate, MontageCreate, SearchPlan
from services.embedding_provider import TextEmbeddingProvider, get_embedding_settings
from services.media_scanner import resolve_media_root
from services.montage_planner import InsufficientMaterialError, MontageError
from services.shot_search import shot_search_statement
from sql.database import session_scope
from sql.models import Asset, AssetLocation, MediaRoot, Shot
from sql.models import ShotFrame


def resolve_source(root_path: str, relative_path: str) -> Path:
    try:
        root = resolve_media_root(
            Path(root_path),
            Path(os.getenv("MEDIA_ROOT_PATH", "/media/library")),
        )
        path = (root / relative_path).resolve(strict=True)
        path.relative_to(root)
        if not path.is_file():
            raise ValueError("not a file")
        return path
    except (OSError, ValueError, RuntimeError) as error:
        raise MontageError("Исходник недоступен внутри разрешённого корня") from error


async def source_for_asset(asset_id: UUID, request: MontageCreate):
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(AssetLocation, MediaRoot, Asset)
                .join(MediaRoot, MediaRoot.id == AssetLocation.media_root_id)
                .join(Asset, Asset.id == AssetLocation.asset_id)
                .where(
                    Asset.id == asset_id,
                    AssetLocation.media_root_id == request.media_root_id,
                    AssetLocation.is_available.is_(True),
                    MediaRoot.is_enabled.is_(True),
                )
                .order_by(AssetLocation.normalized_path)
            )
        ).all()
    if request.asset_ids and asset_id not in request.asset_ids:
        raise MontageError("Актив вне выбранной области")
    for location, root, asset in rows:
        try:
            path = resolve_source(root.container_path, location.relative_path)
            stat = path.stat()
            if stat.st_size != location.size_bytes or (
                location.modified_at_ns is not None
                and stat.st_mtime_ns != location.modified_at_ns
            ):
                continue
            return path, stat, asset
        except (MontageError, OSError):
            continue
    raise MontageError("Источник изменён или недоступен; повторите индексацию")


async def retrieve_candidates(
    request: MontageCreate, plan: SearchPlan
) -> list[Candidate]:
    settings = get_embedding_settings()
    provider = TextEmbeddingProvider(settings)
    candidates = []
    shortages = []
    for section in plan.sections:
        vector = await asyncio.to_thread(provider.embed, section.query)
        preview = (
            select(ShotFrame.id)
            .where(
                ShotFrame.shot_id == Shot.id,
                ShotFrame.thumbnail_path.is_not(None),
            )
            .order_by(ShotFrame.sequence)
            .limit(1)
            .scalar_subquery()
        )
        statement = shot_search_statement(
            vector,
            provider,
            settings,
            limit=100,
            min_similarity=request.min_similarity,
            media_root_id=request.media_root_id,
            asset_ids=request.asset_ids,
            min_duration_ms=40,
            available_only=True,
        ).add_columns(
            preview.label("preview"),
            func.coalesce(Asset.duplicate_of_asset_id, Asset.id).label("canonical"),
        )
        async with session_scope() as session:
            rows = (await session.execute(statement)).all()
        seen = set()
        section_candidates = []
        for shot, description, _embedding, score, frame_id, canonical in rows:
            identity = (canonical, shot.start_ms, shot.end_ms)
            if identity in seen:
                continue
            try:
                _, stat, asset = await source_for_asset(shot.asset_id, request)
            except MontageError:
                continue
            if asset.duration_ms is None or shot.end_ms > asset.duration_ms:
                continue
            seen.add(identity)
            section_candidates.append(
                Candidate(
                    candidate_id=uuid4(),
                    section_id=section.section_id,
                    asset_id=shot.asset_id,
                    shot_id=shot.id,
                    start_ms=shot.start_ms,
                    end_ms=shot.end_ms,
                    summary=(description.summary or "")[:1500],
                    similarity=float(score),
                    preview_frame_id=frame_id,
                    size_bytes=stat.st_size,
                    modified_at_ns=stat.st_mtime_ns,
                    checksum=asset.checksum,
                    canonical_asset_id=canonical,
                )
            )
            if len(section_candidates) == 8:
                break
        available_ms = sum(
            (item.end_ms - item.start_ms) // 40 * 40 for item in section_candidates
        )
        if available_ms < section.duration_ms:
            shortages.append(section.section_id)
        candidates.extend(section_candidates)
    if shortages:
        error = InsufficientMaterialError(
            "Недостаточно материала для секций: " + ", ".join(shortages)
        )
        error.candidates = [item.model_dump(mode="json") for item in candidates]
        raise error
    return candidates
