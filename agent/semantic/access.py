"""Who may read which cube, written for Cube's `queryRewrite` (infra/cube/cube.js) to enforce.

This is the same rule the SQL guard applies to tables, applied to cubes, and it's written by code
from the source's tenancy settings, never by a model:

  * On a source with a tenant column, a cube whose rows come from a table carrying that column gets
    a hidden `nova_tenant` dimension, and someone limited to some tenants only sees rows where it
    matches. Every other table its SQL reads must carry the column too, or be shared.
  * A cube whose tables are all shared (such as exchange rates) is readable by everyone.
  * Any other cube on a multi-tenant source can't be limited, so it's refused to limited users.

The tenant dimension casts to varchar because Cube passes filter values as strings, and Trino
won't compare an integer with one.
"""

from __future__ import annotations

from typing import Any

import sqlglot
from pydantic import BaseModel, Field
from sqlglot import exp

from agent.harness.guardrails import IDENTIFIER, TRINO, SqlPolicy, guard
from agent.harness.errors import SqlRejected
from agent.semantic.model import TENANT_DIMENSION, Cube, DataModel, Dimension
from agent.tools.profiler import SourceProfile
from agent.tools.sources import Source


class Tenancy(BaseModel):
    """A source's tenancy settings, which an administrator confirms."""

    column: str | None = None
    shared_tables: list[str] = Field(default_factory=list, description="schema.table, readable by every tenant")


def tenancy_of(source: Source) -> Tenancy:
    return Tenancy(column=source.tenant_column, shared_tables=list(source.shared_tables))


def tables_of(cube: Cube, catalog: str) -> list[str]:
    """The tables a cube reads, as catalog.schema.table, its own row table first."""
    if cube.sql_table:
        return [cube.sql_table.lower()]
    guarded = guard(cube.sql or "", SqlPolicy(allowed_catalogs=frozenset({catalog})), add_limit=False)
    tree = sqlglot.parse_one(guarded.sql, read=TRINO)
    source = tree.args["from"].this if tree.args.get("from") else None
    first = (f"{source.catalog}.{source.db}.{source.name}".lower() if isinstance(source, exp.Table) else None)
    rest = [table for table in guarded.tables if table != first]
    return [first, *rest] if first else rest


def tenant_tables(profile: SourceProfile, tenant_column: str | None) -> set[str]:
    if not tenant_column:
        return set()
    return {f"{profile.catalog}.{table.qualified}".lower() for table in profile.tables
            if any(column.name == tenant_column for column in table.columns)}


def apply_tenancy(model: DataModel, *, catalog: str, profile: SourceProfile, tenancy: Tenancy
                  ) -> tuple[DataModel, dict[str, Any]]:
    """Add the tenant dimension where it belongs, and build the access manifest."""
    tenant_column, shared_tables = tenancy.column, tenancy.shared_tables
    if tenant_column and not IDENTIFIER.fullmatch(tenant_column):
        raise SqlRejected(f"{tenant_column!r} isn't a usable column name")
    scoped = tenant_tables(profile, tenant_column)
    shared = {f"{catalog}.{table}".lower() for table in shared_tables}
    model = model.model_copy(deep=True)
    rules: dict[str, dict[str, Any]] = {}
    for cube in model.cubes:
        cube.dimensions = [d for d in cube.dimensions if d.name != TENANT_DIMENSION]
        if not tenant_column:
            rules[cube.name] = {}
            continue
        tables = tables_of(cube, catalog)
        if tables and all(table in shared for table in tables):
            rules[cube.name] = {"shared": True}
        elif tables and tables[0] in scoped and all(table in scoped or table in shared for table in tables[1:]):
            cube.dimensions.append(Dimension(name=TENANT_DIMENSION, sql=f"CAST({{CUBE}}.{tenant_column} AS varchar)",
                                             type="string", public=False))
            rules[cube.name] = {"tenant_dimension": TENANT_DIMENSION}
        else:
            rules[cube.name] = {}  # can't be limited, so refused to anyone who is
    return model, {"catalog": catalog, "cubes": rules}


def unlimitable(manifest: dict[str, Any], *, multi_tenant: bool) -> list[str]:
    """Cubes someone limited to some tenants can't read."""
    if not multi_tenant:
        return []
    return [name for name, rule in manifest["cubes"].items() if not rule.get("tenant_dimension") and not rule.get("shared")]
