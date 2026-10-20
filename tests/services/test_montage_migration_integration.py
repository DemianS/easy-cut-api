"""Миграция старого монтажа проверяется в собственной откатываемой схеме."""

import asyncio
from datetime import UTC, datetime
import importlib.util
import os
from pathlib import Path
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


@pytest.mark.skipif(
    os.getenv("RUN_DATABASE_INTEGRATION") != "1", reason="Требуется PostgreSQL"
)
def test_review_migration_preserves_existing_timeline_and_render():
    asyncio.run(_run())


async def _run():
    from sql.database import engine

    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.run_sync(_check_migration)
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


def _check_migration(connection):
    schema_name = f"montage_migration_test_{uuid4().hex}"
    connection.exec_driver_sql(f'CREATE SCHEMA "{schema_name}"')
    connection.exec_driver_sql(f'SET LOCAL search_path TO "{schema_name}"')
    metadata = sa.MetaData()
    jobs = sa.Table(
        "jobs",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("parent_job_id", sa.Uuid()),
        sa.Column("job_type", sa.Text()),
        sa.Column("status", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    runs = sa.Table(
        "montage_runs",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("job_id", sa.Uuid()),
        sa.Column("request", JSONB),
        sa.Column("selection", JSONB),
        sa.Column("candidates", JSONB),
        sa.Column("output_path", sa.Text()),
    )
    versions = sa.Table(
        "timeline_versions",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("montage_run_id", sa.Uuid()),
        sa.Column("timeline", JSONB),
        sa.UniqueConstraint(
            "montage_run_id", name="uq_timeline_versions_montage_run_id"
        ),
    )
    metadata.create_all(connection)
    parent_id, render_job_id, run_id, version_id = [uuid4() for _ in range(4)]
    now = datetime.now(UTC)
    connection.execute(
        jobs.insert(),
        [
            {
                "id": parent_id,
                "parent_job_id": None,
                "job_type": "montage",
                "status": "succeeded",
                "created_at": now,
                "updated_at": now,
            },
            {
                "id": render_job_id,
                "parent_job_id": parent_id,
                "job_type": "montage_render",
                "status": "succeeded",
                "created_at": now,
                "updated_at": now,
            },
        ],
    )
    selection = {"clips": [{"candidate_id": "legacy-candidate"}]}
    candidates = [{"candidate_id": "legacy-candidate"}]
    connection.execute(
        runs.insert(),
        {
            "id": run_id,
            "job_id": parent_id,
            "request": {},
            "selection": selection,
            "candidates": candidates,
            "output_path": "renders/legacy/final.mp4",
        },
    )
    connection.execute(
        versions.insert(), {"id": version_id, "montage_run_id": run_id, "timeline": {}}
    )
    path = (
        Path(__file__).parents[2]
        / "migrations/versions/f897faf954d7_add_timeline_review_and_version_bound_.py"
    )
    spec = importlib.util.spec_from_file_location("review_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with Operations.context(MigrationContext.configure(connection)):
        migration.upgrade()
        snapshot = connection.execute(
            sa.text("SELECT selection, candidates FROM timeline_versions")
        ).one()
        assert snapshot.selection == selection
        assert snapshot.candidates == candidates
        current = connection.execute(
            sa.text(
                "SELECT current_timeline_version_id, review_required FROM montage_runs"
            )
        ).one()
        assert current.current_timeline_version_id == version_id
        assert current.review_required is False
        render = connection.execute(
            sa.text("SELECT id, timeline_version_id, output_path FROM montage_renders")
        ).one()
        assert render.id == render_job_id
        assert render.timeline_version_id == version_id
        assert render.output_path == "renders/legacy/final.mp4"
        other_version = uuid4()
        connection.execute(
            versions.insert(),
            {"id": other_version, "montage_run_id": run_id, "timeline": {}},
        )
        with pytest.raises(RuntimeError, match="история версий"):
            migration.downgrade()
        assert connection.scalar(sa.text("SELECT count(*) FROM timeline_versions")) == 2
        connection.execute(versions.delete().where(versions.c.id == other_version))
        migration.downgrade()
        assert connection.scalar(sa.text("SELECT count(*) FROM timeline_versions")) == 1
