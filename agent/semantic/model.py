"""The subset of Cube's data model that Nova writes, as Pydantic models.

Nova only writes what's here, and reads models back through these same classes, so a hand-edited
file with anything else in it is refused rather than half understood. YAML keys are Cube's own
(snake_case), so a file written here is an ordinary Cube model.
"""

from __future__ import annotations

import os
import tempfile
from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

TENANT_DIMENSION = "nova_tenant"  # added by Nova, never by the profiler: see agent/semantic/access.py
CUBE_SEPARATOR = "__"  # cube names are <catalog>__<name>, so two sources can't collide


class Status(StrEnum):
    INFERRED = "inferred"
    REVIEWED = "reviewed"


class MemberMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Status = Status.INFERRED
    reviewed_by: str | None = None
    unit: str | None = None


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Dimension(Strict):
    name: str
    title: str | None = None
    description: str | None = Field(default=None, max_length=500)
    sql: str = Field(description="A column or row-level expression, such as {CUBE}.status")
    type: Literal["string", "number", "time", "boolean"]
    primary_key: bool = False
    public: bool | None = None
    meta: MemberMeta = Field(default_factory=MemberMeta)


class Filter(Strict):
    sql: str


class Measure(Strict):
    name: str
    title: str | None = None
    description: str | None = Field(default=None, max_length=500)
    type: Literal["count", "count_distinct", "sum", "avg", "min", "max", "number"]
    sql: str | None = Field(default=None, description="A row-level expression; for `number`, a calculation over "
                                                      "other measures, such as {revenue} / NULLIF({orders}, 0)")
    filters: list[Filter] = Field(default_factory=list)
    format: Literal["percent", "currency"] | None = None
    meta: MemberMeta = Field(default_factory=MemberMeta)

    @model_validator(mode="after")
    def _needs_sql(self) -> "Measure":
        if self.type != "count" and not self.sql:
            raise ValueError(f"measure {self.name} of type {self.type} needs sql")
        if self.type == "number" and self.filters:
            raise ValueError(f"measure {self.name}: a number measure can't have filters; filter the measures it uses")
        return self


class Segment(Strict):
    name: str
    title: str | None = None
    description: str | None = Field(default=None, max_length=500)
    sql: str


class Join(Strict):
    name: str = Field(description="The cube joined to")
    relationship: Literal["many_to_one", "one_to_many", "one_to_one"]
    sql: str


class Cube(Strict):
    name: str
    title: str | None = None
    description: str | None = Field(default=None, max_length=800)
    sql_table: str | None = Field(default=None, description="catalog.schema.table")
    sql: str | None = Field(default=None, description="A SELECT, when the rows need shaping first")
    joins: list[Join] = Field(default_factory=list)
    dimensions: list[Dimension] = Field(default_factory=list)
    measures: list[Measure] = Field(default_factory=list)
    segments: list[Segment] = Field(default_factory=list)

    @model_validator(mode="after")
    def _one_source(self) -> "Cube":
        if bool(self.sql_table) == bool(self.sql):
            raise ValueError(f"cube {self.name} needs exactly one of sql_table and sql")
        return self

    def member(self, name: str) -> Dimension | Measure | Segment | None:
        return next((m for m in [*self.dimensions, *self.measures, *self.segments] if m.name == name), None)


class DataModel(Strict):
    cubes: list[Cube] = Field(default_factory=list)

    def cube(self, name: str) -> Cube | None:
        return next((cube for cube in self.cubes if cube.name == name), None)

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.model_dump(mode="json", exclude_none=True, exclude_defaults=True),
                              sort_keys=False, allow_unicode=True, width=120)

    @classmethod
    def from_yaml(cls, text: str) -> "DataModel":
        return cls.model_validate(yaml.safe_load(text) or {})


class Proposal(Strict):
    """A change to a reviewed measure or dimension, waiting for a person."""

    cube: str
    member: Measure | Dimension
    reason: str


class Proposals(Strict):
    items: list[Proposal] = Field(default_factory=list)


def write_atomically(path: Path, text: str) -> None:
    """Cube reads these files while Nova writes them, so a reader must never see half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "w") as file:
            file.write(text)
        os.chmod(temporary, 0o644)  # mkstemp makes it 0600, and Cube reads it as another user
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
