"""A query against the semantic layer, as agents, examples and the UI write it.

It's Cube's query format with snake_case fields and only what Nova uses: measures, dimensions, one
or more time dimensions, AND-ed filters, segments, order and limit. `to_cube` turns it into what
Cube's REST API takes.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Granularity = Literal["day", "week", "month", "quarter", "year"]
Operator = Literal["equals", "notEquals", "contains", "notContains", "startsWith", "notStartsWith", "endsWith",
                   "notEndsWith", "gt", "gte", "lt", "lte", "set", "notSet", "inDateRange", "notInDateRange",
                   "beforeDate", "beforeOrOnDate", "afterDate", "afterOrOnDate"]
MAX_LIMIT = 50_000


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TimeDimension(Strict):
    dimension: str = Field(description="cube.member of type time")
    granularity: Granularity | None = Field(default=None, description="Group by this period. Leave empty to only "
                                                                      "limit the range")
    date_range: list[str] | None = Field(default=None, min_length=2, max_length=2,
                                         description="[from, to] as YYYY-MM-DD, both included")


class QueryFilter(Strict):
    member: str
    operator: Operator
    values: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _values(self) -> "QueryFilter":
        if self.operator not in ("set", "notSet") and not self.values:
            raise ValueError(f"filter on {self.member}: {self.operator} needs values")
        return self


class Order(Strict):
    member: str
    direction: Literal["asc", "desc"] = "desc"


class SemanticQuery(Strict):
    measures: list[str] = Field(default_factory=list, description="cube.member names")
    dimensions: list[str] = Field(default_factory=list)
    time_dimensions: list[TimeDimension] = Field(default_factory=list)
    filters: list[QueryFilter] = Field(default_factory=list)
    segments: list[str] = Field(default_factory=list)
    order: list[Order] = Field(default_factory=list)
    limit: int | None = Field(default=None, ge=1, le=MAX_LIMIT)

    @model_validator(mode="after")
    def _asks_for_something(self) -> "SemanticQuery":
        if not (self.measures or self.dimensions or any(t.granularity for t in self.time_dimensions)):
            raise ValueError("a query needs at least one measure or dimension")
        return self

    def members(self) -> list[str]:
        return [*self.measures, *self.dimensions, *(t.dimension for t in self.time_dimensions),
                *(f.member for f in self.filters), *self.segments, *(o.member for o in self.order)]

    def to_cube(self) -> dict[str, Any]:
        query: dict[str, Any] = {"measures": self.measures, "dimensions": self.dimensions}
        if self.time_dimensions:
            query["timeDimensions"] = [
                {"dimension": t.dimension, **({"granularity": t.granularity} if t.granularity else {}),
                 **({"dateRange": t.date_range} if t.date_range else {})} for t in self.time_dimensions]
        if self.filters:
            query["filters"] = [{"member": f.member, "operator": f.operator, **({"values": f.values} if f.values else {})}
                                for f in self.filters]
        if self.segments:
            query["segments"] = self.segments
        if self.order:
            query["order"] = [[o.member, o.direction] for o in self.order]
        if self.limit:
            query["limit"] = self.limit
        return query


class Example(Strict):
    """A business question and the query that answers it, run and checked when the skill was made."""

    question: str = Field(max_length=300)
    query: SemanticQuery


def column_of(member: str, granularity: str | None = None) -> str:
    """The key Cube gives a member's values in a result row."""
    return f"{member}.{granularity}" if granularity else member
