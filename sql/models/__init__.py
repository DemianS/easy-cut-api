"""Импорт всех моделей для регистрации в metadata Alembic."""

from sql.models.asset import Asset
from sql.models.asset_location import AssetLocation
from sql.models.job import Job
from sql.models.media_root import MediaRoot
from sql.models.shot import Shot
from sql.models.shot_description import ShotDescription
from sql.models.shot_embedding import ShotEmbedding
from sql.models.shot_frame import ShotFrame
from sql.models.user import User

__all__ = (
    "Asset",
    "AssetLocation",
    "Job",
    "MediaRoot",
    "Shot",
    "ShotDescription",
    "ShotEmbedding",
    "ShotFrame",
    "User",
)
