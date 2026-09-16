"""Build-selected INSERT identifier; values always remain typed bound parameters."""

from sqlalchemy import Boolean, DateTime, Float, String, bindparam, text

TIMESTAMP_COLUMN = "timestamp"
COLUMN_NAMES = ("id", "patient_id", "reading_type", "value", "unit", TIMESTAMP_COLUMN, "is_abnormal", "created_at")
SQL = (
    f"INSERT INTO sensor_readings ({', '.join(COLUMN_NAMES)}) "
    "VALUES (:id, :patient_id, :reading_type, :value, :unit, :timestamp, :is_abnormal, :created_at)"
)


def write_statement():
    """Keep identifier selection below the ORM while binding every patient value by type."""
    return text(SQL).bindparams(
        bindparam("id", type_=String(36)),
        bindparam("patient_id", type_=String(64)),
        bindparam("reading_type", type_=String(32)),
        bindparam("value", type_=Float),
        bindparam("unit", type_=String(16)),
        bindparam("timestamp", type_=DateTime(timezone=True)),
        bindparam("is_abnormal", type_=Boolean),
        bindparam("created_at", type_=DateTime(timezone=True)),
    )


def write_values(reading) -> dict:
    """Map domain fields to stable bind keys independently of the compiled column identifier."""
    return {
        name: getattr(reading, name)
        for name in ("id", "patient_id", "reading_type", "value", "unit", "timestamp", "is_abnormal", "created_at")
    }
