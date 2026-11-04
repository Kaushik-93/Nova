"""Collects the facts about a source. No LLM here: everything in a SourceProfile was
measured, so whatever the profiler subagent concludes from it rests on real numbers.

Sampling uses TABLESAMPLE BERNOULLI, because `LIMIT n` takes the oldest rows: on the
demo orders table a LIMIT sample reports data ending in January 2025 when it really
runs to the present. BERNOULLI reads the whole table, so on a table too big for the
timeout it falls back to LIMIT, and the profile records that the sample is biased.

Column comments and values come from the source and are untrusted. They are stored
as plain data here, and must only reach the model through `wrap_untrusted`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Iterable

from pydantic import BaseModel, Field

from agent.harness.audit import RunContext
from agent.harness.budgets import RunBudget
from agent.harness.errors import NovaError, QueryTimedOut
from agent.harness.executor import SqlExecutor
from agent.harness.guardrails import SqlPolicy

logger = logging.getLogger(__name__)

SAMPLE_ROWS = 20_000
TOP_VALUE_LIMIT = 10
MAX_DISTINCT_FOR_TOP_VALUES = 30
MIN_FK_OVERLAP = 0.98
NEAR_UNIQUE = 0.9
KEY_LIKE = re.compile(r"(email|sku|code|number|slug|username)$", re.I)

# Only engine internals. `public` stays: it's where most Postgres tables live.
SKIPPED_SCHEMAS = {"information_schema", "pg_catalog", "pg_toast", "performance_schema", "mysql", "sys"}
NUMERIC = re.compile(r"^(tinyint|smallint|integer|bigint|real|double|decimal)")
TEMPORAL = re.compile(r"^(date|timestamp|time)")
TEXT = re.compile(r"^(varchar|char)")
# Never shown as top values, even when a column has few distinct values.
PERSONAL = re.compile(r"email|phone|mobile|first_?name|last_?name|full_?name|address|ssn|password|token|secret|plate|"
                      r"licen[cs]e", re.I)


class ColumnRole(StrEnum):
    IDENTIFIER = "identifier"
    TIME = "time"
    MEASURE = "measure"
    DIMENSION = "dimension"
    TEXT = "text"
    FLAG = "flag"


class GotchaKind(StrEnum):
    SOFT_DELETE = "soft_delete"
    TEST_ROWS = "test_rows"
    MONEY_UNIT = "money_unit"
    DUPLICATED_KEY = "duplicated_key"
    REVISIONED_ROWS = "revisioned_rows"
    TIME_ZONE = "time_zone"
    STATUS_FILTER = "status_filter"
    MIXED_CURRENCY = "mixed_currency"


class Gotcha(BaseModel):
    kind: GotchaKind
    table: str
    column: str
    detail: str


class ColumnProfile(BaseModel):
    name: str
    type: str
    nullable: bool
    comment: str | None = None
    role: ColumnRole = ColumnRole.TEXT
    null_pct: float | None = None
    distinct: int | None = Field(default=None, description="Approximate, within about 2%")
    duplicate_values: int | None = Field(default=None, description="Exact, for columns that look like keys")
    min: Any = None
    max: Any = None
    top_values: list[list[Any]] = Field(default_factory=list)


class ForeignKey(BaseModel):
    column: str
    references_table: str
    references_column: str
    declared: bool
    overlap: float | None = None


class TableProfile(BaseModel):
    schema_name: str
    name: str
    comment: str | None = None
    row_count: int | None = None
    sample_rows: int = 0
    sample_method: str = "none"
    sample_is_biased: bool = False
    columns: list[ColumnProfile] = Field(default_factory=list)
    primary_key: list[str] = Field(default_factory=list)
    primary_key_declared: bool = False
    foreign_keys: list[ForeignKey] = Field(default_factory=list)

    @property
    def qualified(self) -> str:
        return f"{self.schema_name}.{self.name}"


class SourceProfile(BaseModel):
    source_slug: str
    catalog: str
    dialect: str
    fingerprint: str
    profiled_at: datetime
    tables: list[TableProfile]
    gotchas: list[Gotcha] = Field(default_factory=list)
    # Listed by the source but not readable with these credentials. Kept so they count
    # towards the fingerprint: otherwise one locked table would make a skill look
    # stale forever, because the live listing includes it.
    unreadable: dict[str, dict[str, str]] = Field(default_factory=dict)


class SchemaDiff(BaseModel):
    added_tables: list[str] = Field(default_factory=list)
    removed_tables: list[str] = Field(default_factory=list)
    added_columns: list[str] = Field(default_factory=list)
    removed_columns: list[str] = Field(default_factory=list)
    changed_types: list[str] = Field(default_factory=list)
    changed_tables: list[str] = Field(default_factory=list, description="Tables needing to be measured again")

    @property
    def is_empty(self) -> bool:
        return not any(self.model_dump().values())


# --- Fingerprints ------------------------------------------------------------

Shape = dict[str, dict[str, str]]  # {schema.table: {column: type}}


def fingerprint(shape: Shape) -> str:
    """A hash of tables, columns and types. Order-independent, so a re-listed schema
    hashes the same; values aren't included, so new rows don't make a skill stale."""
    canonical = json.dumps({table: dict(sorted(cols.items())) for table, cols in sorted(shape.items())},
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def diff_shapes(old: Shape, new: Shape) -> SchemaDiff:
    diff = SchemaDiff(added_tables=sorted(set(new) - set(old)), removed_tables=sorted(set(old) - set(new)))
    changed = set(diff.added_tables)
    for table in sorted(set(old) & set(new)):
        before, after = old[table], new[table]
        if before == after:
            continue
        changed.add(table)
        diff.added_columns += [f"{table}.{c}" for c in sorted(set(after) - set(before))]
        diff.removed_columns += [f"{table}.{c}" for c in sorted(set(before) - set(after))]
        diff.changed_types += [f"{table}.{c} {before[c]} -> {after[c]}" for c in sorted(set(before) & set(after))
                               if before[c] != after[c]]
    diff.changed_tables = sorted(changed)
    return diff


def shape_of(profile: SourceProfile) -> Shape:
    return {t.qualified: {c.name: c.type for c in t.columns} for t in profile.tables} | profile.unreadable


# --- Heuristics --------------------------------------------------------------

def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def classify_column(column: ColumnProfile, table: TableProfile) -> ColumnRole:
    name, kind = column.name.lower(), column.type.lower()
    if name in {c.lower() for c in table.primary_key} or any(fk.column == column.name for fk in table.foreign_keys):
        return ColumnRole.IDENTIFIER
    if re.search(r"(^|_)(id|uuid|key|guid)$", name):
        return ColumnRole.IDENTIFIER
    if kind.startswith("boolean") or re.match(r"^(is|has)_", name):
        return ColumnRole.FLAG
    if TEMPORAL.match(kind):
        return ColumnRole.TIME
    if NUMERIC.match(kind):
        return ColumnRole.MEASURE
    if TEXT.match(kind) and column.distinct is not None and column.distinct <= MAX_DISTINCT_FOR_TOP_VALUES:
        return ColumnRole.DIMENSION
    return ColumnRole.TEXT


def find_gotchas(tables: Iterable[TableProfile]) -> list[Gotcha]:
    """Traps worth a human's attention. Each is a measured signal, not a conclusion:
    the subagent, then a reviewer, decide what it means. Findings that repeat across
    a table's columns become one line, because every line here costs the model tokens
    and competes for its attention with the warnings that matter."""
    found: list[Gotcha] = []
    for table in tables:
        found += _column_gotchas(table)
        found += _grouped_gotchas(table)
    return found


def _column_gotchas(table: TableProfile) -> list[Gotcha]:
    found: list[Gotcha] = []
    for column in table.columns:
        name = column.name.lower()
        where = dict(table=table.qualified, column=column.name)

        if re.fullmatch(r"deleted(_at|_on)?|is_deleted|archived(_at)?|is_archived|removed_at", name):
            share = 100 - (column.null_pct if column.null_pct is not None else 100)
            found.append(Gotcha(kind=GotchaKind.SOFT_DELETE, **where,
                                detail=f"About {share:.1f}% of sampled rows are marked; they probably shouldn't count."))
        if re.fullmatch(r"(is_)?(test|internal|sandbox|demo)(_account|_user|_order)?", name):
            found.append(Gotcha(kind=GotchaKind.TEST_ROWS, **where,
                                detail="Flags non-production rows, which probably shouldn't count."))
        if name in {"is_latest", "is_current", "revision"}:
            found.append(Gotcha(kind=GotchaKind.REVISIONED_ROWS, **where,
                                detail="A key can have several versions: filter to the current one or sums double count."))
        if re.fullmatch(r"status|state|order_status|payment_status", name) and column.top_values:
            values = ", ".join(str(v[0]) for v in column.top_values)
            found.append(Gotcha(kind=GotchaKind.STATUS_FILTER, **where,
                                detail=f"Values include: {values}. Some likely don't count towards totals."))
        if re.fullmatch(r"(\w+_)?currency(_code)?", name) and len(column.top_values) > 1:
            values = ", ".join(str(v[0]) for v in column.top_values)
            found.append(Gotcha(kind=GotchaKind.MIXED_CURRENCY, **where,
                                detail=f"Amounts are in several currencies ({values}): they can't be added together "
                                       "without converting them."))
        if column.duplicate_values:
            found.append(Gotcha(kind=GotchaKind.DUPLICATED_KEY, **where,
                                detail=f"Looks like a key, but {column.duplicate_values:,} sampled values are "
                                       "duplicates: don't count on it being unique."))
    return found


def _grouped_gotchas(table: TableProfile) -> list[Gotcha]:
    groups: dict[tuple[GotchaKind, str], list[str]] = {}
    for column in table.columns:
        name, kind = column.name.lower(), column.type.lower()
        if re.search(r"(_cents|_pence|_minor)$", name):
            key = (GotchaKind.MONEY_UNIT, "Stored in minor units: divide by 100 for currency.")
        elif re.search(r"(_usd|_eur|_gbp|_dollars)$", name):
            key = (GotchaKind.MONEY_UNIT, "Stored in major currency units.")
        elif kind.startswith("timestamp") and "with time zone" in kind:
            key = (GotchaKind.TIME_ZONE, "Time-zone aware: day and month boundaries depend on the reporting zone.")
        elif kind.startswith(("date", "timestamp")):
            key = (GotchaKind.TIME_ZONE, "No time zone recorded: confirm which zone before joining to other sources.")
        else:
            continue
        groups.setdefault(key, []).append(column.name)
    return [Gotcha(kind=kind, table=table.qualified, column=columns[0],
                   detail=f"{detail} Applies to: {', '.join(columns)}." if len(columns) > 1 else detail)
            for (kind, detail), columns in groups.items()]


# --- Declared keys and comments ----------------------------------------------
# Trino's information_schema has no constraints, so these use the passthrough.
# They are fixed text; the only inserted value is the validated schema list.

def _sql_list(values: Iterable[str]) -> str:
    return ", ".join("''" + v.replace("'", "''''") + "''" for v in values)


def _postgres_keys_sql(catalog: str, schemas: list[str]) -> str:
    return f"""SELECT * FROM TABLE({_quote(catalog)}.system.query(query => '
      SELECT n.nspname AS schema_name, t.relname AS table_name, c.contype::text AS kind, a.attname AS column_name,
             ft.relname AS ref_table, fn.nspname AS ref_schema, fa.attname AS ref_column
      FROM pg_constraint c
      JOIN pg_class t ON t.oid = c.conrelid
      JOIN pg_namespace n ON n.oid = t.relnamespace
      JOIN LATERAL unnest(c.conkey) WITH ORDINALITY k(att, ord) ON true
      JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.att
      LEFT JOIN pg_class ft ON ft.oid = c.confrelid
      LEFT JOIN pg_namespace fn ON fn.oid = ft.relnamespace
      LEFT JOIN LATERAL unnest(c.confkey) WITH ORDINALITY fk(att, ord) ON fk.ord = k.ord
      LEFT JOIN pg_attribute fa ON fa.attrelid = ft.oid AND fa.attnum = fk.att
      WHERE c.contype IN (''p'', ''f'') AND n.nspname IN ({_sql_list(schemas)})'))"""


def _mysql_keys_sql(catalog: str, schemas: list[str]) -> str:
    return f"""SELECT * FROM TABLE({_quote(catalog)}.system.query(query => '
      SELECT table_schema AS schema_name, table_name AS table_name,
             IF(constraint_name = ''PRIMARY'', ''p'', ''f'') AS kind, column_name AS column_name,
             referenced_table_name AS ref_table, referenced_table_schema AS ref_schema,
             referenced_column_name AS ref_column
      FROM information_schema.key_column_usage
      WHERE table_schema IN ({_sql_list(schemas)})
        AND (constraint_name = ''PRIMARY'' OR referenced_table_name IS NOT NULL)'))"""


KEY_QUERIES = {"postgres": _postgres_keys_sql, "mysql": _mysql_keys_sql}


def _postgres_comments_sql(catalog: str, schemas: list[str]) -> str:
    return f"""SELECT * FROM TABLE({_quote(catalog)}.system.query(query => '
      SELECT n.nspname AS schema_name, c.relname AS table_name, a.attname AS column_name, d.description AS comment
      FROM pg_description d
      JOIN pg_class c ON c.oid = d.objoid
      JOIN pg_namespace n ON n.oid = c.relnamespace
      LEFT JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = d.objsubid AND d.objsubid > 0
      WHERE n.nspname IN ({_sql_list(schemas)})'))"""


COMMENT_QUERIES = {"postgres": _postgres_comments_sql}


# --- The profiler ------------------------------------------------------------

class Profiler:
    def __init__(self, executor: SqlExecutor, *, context: RunContext, budget: RunBudget,
                 sample_rows: int = SAMPLE_ROWS) -> None:
        self._executor = executor
        self._context = context
        self._budget = budget
        self._sample_rows = sample_rows

    def _policy(self, catalog: str) -> SqlPolicy:
        return SqlPolicy(
            allowed_catalogs=frozenset({catalog}),
            allowed_system_tables=frozenset({f"{catalog}.information_schema.columns"}),
            max_rows=self._sample_rows,
        )

    def _select(self, catalog: str, sql: str) -> list[list[Any]]:
        return self._executor.run(sql, policy=self._policy(catalog), context=self._context, budget=self._budget).rows

    def read_shape(self, catalog: str) -> Shape:
        """Just the tables, columns and types: all a freshness check needs."""
        rows = self._select(catalog, f"""
            SELECT table_schema, table_name, column_name, data_type
            FROM {catalog}.information_schema.columns
            ORDER BY table_schema, table_name, ordinal_position""")
        shape: Shape = {}
        for schema, table, column, kind in rows:
            if schema not in SKIPPED_SCHEMAS:
                shape.setdefault(f"{schema}.{table}", {})[column] = kind
        return shape

    def profile(self, source_slug: str, catalog: str, dialect: str, *, previous: SourceProfile | None = None,
                only_tables: set[str] | None = None) -> SourceProfile:
        """Profile a source. With `previous` and `only_tables`, only those tables are
        measured again and the rest are carried over: an incremental re-profile."""
        shape = self.read_shape(catalog)
        columns_info = self._nullability(catalog)
        schemas = sorted({table.split(".")[0] for table in shape})
        keys = self._declared_keys(catalog, dialect, schemas)
        comments = self._comments(catalog, dialect, schemas)
        carried = {t.qualified: t for t in previous.tables} if previous else {}

        tables: list[TableProfile] = []
        unreadable: Shape = {}
        for qualified, columns in shape.items():
            if only_tables is not None and qualified not in only_tables and qualified in carried:
                tables.append(carried[qualified])
                continue
            try:
                tables.append(self._profile_table(catalog, qualified, columns, columns_info, keys, comments))
            except NovaError as error:
                logger.warning("skipping table", extra={"table": qualified, "reason": error.message})
                unreadable[qualified] = columns

        self._infer_keys(catalog, tables, only_tables)
        for table in tables:
            for column in table.columns:
                column.role = classify_column(column, table)

        return SourceProfile(source_slug=source_slug, catalog=catalog, dialect=dialect, tables=tables,
                             fingerprint=fingerprint(shape), profiled_at=datetime.now(timezone.utc),
                             gotchas=find_gotchas(tables), unreadable=unreadable)

    def _nullability(self, catalog: str) -> dict[tuple[str, str], bool]:
        rows = self._select(catalog, f"""
            SELECT table_schema, table_name, column_name, is_nullable
            FROM {catalog}.information_schema.columns""")
        return {(f"{s}.{t}", c): nullable == "YES" for s, t, c, nullable in rows}

    def _declared_keys(self, catalog: str, dialect: str, schemas: list[str]) -> list[list[Any]]:
        builder = KEY_QUERIES.get(dialect)
        if builder is None or not schemas:
            return []
        return self._executor.run_internal(builder(catalog, schemas), context=self._context,
                                           purpose=f"{catalog}: declared keys")

    def _comments(self, catalog: str, dialect: str, schemas: list[str]) -> dict[tuple[str, str | None], str]:
        builder = COMMENT_QUERIES.get(dialect)
        if builder is None or not schemas:
            return {}
        rows = self._executor.run_internal(builder(catalog, schemas), context=self._context,
                                           purpose=f"{catalog}: comments")
        return {(f"{schema}.{table}", column): comment for schema, table, column, comment in rows}

    def _sample_clause(self, table_sql: str, row_count: int | None) -> tuple[str, str]:
        if row_count is not None and row_count <= self._sample_rows:
            return table_sql, "full"
        if row_count:
            percent = max(self._sample_rows / row_count * 100, 0.01)
            return f"{table_sql} TABLESAMPLE BERNOULLI ({percent:.4f})", "bernoulli"
        return table_sql, "limit"

    def _profile_table(self, catalog: str, qualified: str, columns: dict[str, str],
                       nullable: dict[tuple[str, str], bool], keys: list[list[Any]],
                       comments: dict[tuple[str, str | None], str]) -> TableProfile:
        schema, name = qualified.split(".", 1)
        table_sql = f"{_quote(catalog)}.{_quote(schema)}.{_quote(name)}"
        row_count = self._select(catalog, f"SELECT COUNT(*) FROM {table_sql}")[0][0]
        profile = TableProfile(schema_name=schema, name=name, row_count=row_count,
                               comment=comments.get((qualified, None)))
        profile.columns = [ColumnProfile(name=c, type=t, nullable=nullable.get((qualified, c), True),
                                         comment=comments.get((qualified, c))) for c, t in columns.items()]
        self._apply_declared_keys(profile, keys)
        if row_count:
            self._measure(catalog, profile, table_sql, row_count)
        return profile

    @staticmethod
    def _apply_declared_keys(profile: TableProfile, keys: list[list[Any]]) -> None:
        for schema, table, kind, column, ref_table, ref_schema, ref_column in keys:
            if schema != profile.schema_name or table != profile.name:
                continue
            if kind == "p":
                profile.primary_key.append(column)
                profile.primary_key_declared = True
            elif ref_table:
                profile.foreign_keys.append(ForeignKey(column=column, references_table=f"{ref_schema}.{ref_table}",
                                                       references_column=ref_column, declared=True))

    def _measure(self, catalog: str, profile: TableProfile, table_sql: str, row_count: int) -> None:
        source, method = self._sample_clause(table_sql, row_count)
        try:
            self._measure_sample(catalog, profile, source, method)
        except QueryTimedOut:
            logger.warning("sample timed out, falling back", extra={"table": profile.qualified})
            self._measure_sample(catalog, profile, table_sql, "limit")

    def _measure_sample(self, catalog: str, profile: TableProfile, source: str, method: str) -> None:
        sample = f"SELECT * FROM {source}"
        if method == "limit":
            sample += f" LIMIT {self._sample_rows}"
        selections = ["COUNT(*) AS sample_rows"]
        for index, column in enumerate(profile.columns):
            quoted = _quote(column.name)
            selections += [f"COUNT({quoted}) AS c{index}", f"APPROX_DISTINCT({quoted}) AS d{index}"]
            if NUMERIC.match(column.type) or TEMPORAL.match(column.type):
                selections += [f"MIN({quoted}) AS lo{index}", f"MAX({quoted}) AS hi{index}"]
        result = self._executor.run(f"WITH s AS ({sample}) SELECT {', '.join(selections)} FROM s",
                                    policy=self._policy(catalog), context=self._context, budget=self._budget)
        row = dict(zip(result.columns, result.rows[0]))
        sampled = row["sample_rows"]
        profile.sample_rows, profile.sample_method = sampled, method
        profile.sample_is_biased = method == "limit"

        for index, column in enumerate(profile.columns):
            if sampled:
                column.null_pct = round(100 * (1 - row[f"c{index}"] / sampled), 2)
            column.distinct = row[f"d{index}"]
            column.min, column.max = _plain(row.get(f"lo{index}")), _plain(row.get(f"hi{index}"))
            quoted = _quote(column.name)
            if (column.distinct and column.distinct <= MAX_DISTINCT_FOR_TOP_VALUES and not PERSONAL.search(column.name)
                    and TEXT.match(column.type)):
                column.top_values = [[_plain(value), count] for value, count in self._select(catalog, f"""
                    WITH s AS ({sample})
                    SELECT {quoted}, COUNT(*) FROM s WHERE {quoted} IS NOT NULL
                    GROUP BY 1 ORDER BY 2 DESC LIMIT {TOP_VALUE_LIMIT}""")]
            present = sampled * (1 - (column.null_pct or 0) / 100)
            if (KEY_LIKE.search(column.name) and TEXT.match(column.type) and column.name not in profile.primary_key
                    and column.distinct and column.distinct >= present * NEAR_UNIQUE):
                # An exact count, because approx_distinct is off by about 2%. Both
                # counts must come from this one query: BERNOULLI draws a new sample
                # every time it runs, so comparing across queries invents duplicates.
                total, unique = self._select(catalog, f"""
                    WITH s AS ({sample}) SELECT COUNT({quoted}), COUNT(DISTINCT {quoted}) FROM s""")[0]
                column.duplicate_values = total - unique

        if not profile.primary_key:
            for column in profile.columns:
                unique = sampled and column.null_pct == 0 and column.distinct and column.distinct >= sampled * 0.97
                if unique and re.search(r"(^|_)id$", column.name.lower()):
                    profile.primary_key = [column.name]
                    break

    def _infer_keys(self, catalog: str, tables: list[TableProfile], only_tables: set[str] | None) -> None:
        """Find undeclared foreign keys: same column name as another table's single
        primary key, confirmed by measuring how many values actually match."""
        primary = {t.primary_key[0].lower(): t for t in tables if len(t.primary_key) == 1}
        for table in tables:
            if only_tables is not None and table.qualified not in only_tables:
                continue
            known = {fk.column.lower() for fk in table.foreign_keys}
            for column in table.columns:
                name = column.name.lower()
                target = primary.get(name)
                if target is None or target is table or name in known:
                    continue
                if table.primary_key == [column.name]:
                    continue
                overlap = self._measure_overlap(catalog, table, column.name, target, target.primary_key[0])
                if overlap is not None and overlap >= MIN_FK_OVERLAP:
                    table.foreign_keys.append(ForeignKey(column=column.name, references_table=target.qualified,
                                                         references_column=target.primary_key[0], declared=False,
                                                         overlap=round(overlap, 4)))

    def _measure_overlap(self, catalog: str, child: TableProfile, child_column: str, parent: TableProfile,
                         parent_column: str) -> float | None:
        name = lambda t: f"{_quote(catalog)}.{_quote(t.schema_name)}.{_quote(t.name)}"  # noqa: E731
        source, _ = self._sample_clause(name(child), child.row_count)
        column = _quote(child_column)
        rows = self._select(catalog, f"""
            WITH s AS (SELECT DISTINCT {column} AS v FROM {source} WHERE {column} IS NOT NULL LIMIT {self._sample_rows})
            SELECT COUNT(*), COUNT(p.k)
            FROM s LEFT JOIN (SELECT DISTINCT {_quote(parent_column)} AS k FROM {name(parent)}) p ON p.k = s.v""")
        total, matched = rows[0]
        return matched / total if total else None


def _plain(value: Any) -> Any:
    """Make a value safe to write as JSON or YAML."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)
