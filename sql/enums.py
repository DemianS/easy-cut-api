"""Допустимые значения строковых статусов БД."""

from enum import StrEnum


class AssetKind(StrEnum):
    UNKNOWN = "unknown"
    VIDEO = "video"
    IMAGE = "image"
    AUDIO = "audio"


class AssetStatus(StrEnum):
    DISCOVERED = "discovered"
    PROBING = "probing"
    INDEXED = "indexed"
    ANALYZING = "analyzing"
    READY = "ready"
    FAILED = "failed"
    MISSING = "missing"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobType(StrEnum):
    MONTAGE = "montage"
    MONTAGE_PLAN = "montage_plan"
    MONTAGE_SEARCH = "montage_search"
    MONTAGE_SELECT = "montage_select"
    MONTAGE_RENDER = "montage_render"
    LIBRARY_PREPARATION = "library_preparation"
    LIBRARY_SCAN = "library_scan"
    ASSET_PROBE = "asset_probe"
    SHOT_DETECTION = "shot_detection"
    ASSET_DESCRIPTION = "asset_description"
    ASSET_EMBEDDING = "asset_embedding"


class DescriptionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class EmbeddingStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
