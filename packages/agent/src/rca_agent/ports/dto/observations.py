from __future__ import annotations

from pydantic import BaseModel, Field


class CriticalFact(BaseModel):
    """Keep independently sourced facts outside prose truncation and never infer absent fields."""

    source: str
    source_ref: str
    observed_at: str
    account_id: str = ""
    region: str = ""
    service: str = ""
    run_id: str = ""
    sqlstate: str | None = None
    relation: str | None = None
    schema_name: str | None = None
    driver_relation: str | None = None
    driver_column: str | None = None
    columns: list[str] = Field(default_factory=list)
    sql_fingerprint: str | None = None
    actual_schema: dict[str, list[str]] = Field(default_factory=dict)
    task_definition: str | None = None
    image_digest: str | None = None
    source_revision: str | None = None
    write_accounting: dict[str, str] = Field(default_factory=dict)


class IncidentObservations(BaseModel):
    """Verified refers to source/scope checks, never a confirmed diagnosis."""

    critical_facts: list[CriticalFact] = Field(default_factory=list)
    baseline_verified: bool = False
    baseline: dict = Field(default_factory=dict)
    current: dict = Field(default_factory=dict)
    diagnostics: list[str] = Field(default_factory=list)
