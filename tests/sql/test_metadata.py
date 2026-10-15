from sqlalchemy import CheckConstraint

from sql.base import Base
import sql.models  # noqa: F401


def test_first_slice_tables_are_registered() -> None:
    assert set(Base.metadata.tables) == {
        "asset_locations",
        "assets",
        "jobs",
        "media_roots",
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
