from sqlalchemy import CheckConstraint, Enum

from sql.base import Base
import sql.models  # noqa: F401


def test_first_slice_tables_are_registered() -> None:
    assert set(Base.metadata.tables) == {
        "asset_locations",
        "assets",
        "jobs",
        "media_roots",
        "shot_descriptions",
        "shot_embeddings",
        "shot_frames",
        "shots",
        "users",
    }


def test_assets_support_all_required_media_kinds() -> None:
    assets = Base.metadata.tables["assets"]
    constraints = {
        constraint.name: str(constraint.sqltext)
        for constraint in assets.constraints
        if isinstance(constraint, CheckConstraint)
    }

    media_kind_constraint = constraints["ck_assets_media_kind"]
    assert "video" in media_kind_constraint
    assert "image" in media_kind_constraint
    assert "audio" in media_kind_constraint


def test_probe_job_can_reference_asset() -> None:
    jobs = Base.metadata.tables["jobs"]
    asset_foreign_keys = {
        foreign_key.target_fullname for foreign_key in jobs.c.asset_id.foreign_keys
    }

    assert asset_foreign_keys == {"assets.id"}


def test_status_columns_use_native_postgresql_enums() -> None:
    assets = Base.metadata.tables["assets"]
    jobs = Base.metadata.tables["jobs"]
    descriptions = Base.metadata.tables["shot_descriptions"]
    embeddings = Base.metadata.tables["shot_embeddings"]

    assert isinstance(assets.c.status.type, Enum)
    assert assets.c.status.type.native_enum is True
    assert assets.c.status.type.name == "asset_status"
    assert isinstance(jobs.c.status.type, Enum)
    assert jobs.c.status.type.native_enum is True
    assert jobs.c.status.type.name == "job_status"
    assert isinstance(descriptions.c.status.type, Enum)
    assert descriptions.c.status.type.native_enum is True
    assert descriptions.c.status.type.name == "description_status"
    assert isinstance(embeddings.c.status.type, Enum)
    assert embeddings.c.status.type.native_enum is True
    assert embeddings.c.status.type.name == "embedding_status"


def test_shots_use_half_open_positive_intervals() -> None:
    shots = Base.metadata.tables["shots"]
    constraints = {
        constraint.name: str(constraint.sqltext)
        for constraint in shots.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert str(constraints["ck_shots_start_ms"]) == "start_ms >= 0"
    assert str(constraints["ck_shots_time_range"]) == "end_ms > start_ms"
