"""Переиспользуемые нативные типы PostgreSQL."""

from enum import Enum as PythonEnum

from sqlalchemy import Enum as SqlEnum

from sql.enums import AssetStatus, DescriptionStatus, JobStatus


def _enum_values(enum_class: type[PythonEnum]) -> list[str]:
    """Использовать значения enum, а не имена Python-констант."""
    return [str(member.value) for member in enum_class]


asset_status_type = SqlEnum(
    AssetStatus,
    name="asset_status",
    values_callable=_enum_values,
    validate_strings=True,
)

job_status_type = SqlEnum(
    JobStatus,
    name="job_status",
    values_callable=_enum_values,
    validate_strings=True,
)

description_status_type = SqlEnum(
    DescriptionStatus,
    name="description_status",
    values_callable=_enum_values,
    validate_strings=True,
)
