"""SQL guardrails. Everything the agent runs passes through `guard` first.

Per-tenant access is enforced here too. For a scoped user every table is replaced by a
subquery holding only their rows, so nothing the agent writes can reach anyone else's, and a
table with no way to scope it is refused rather than shown.

This is an allowlist, and it fails closed: a query is accepted only if it is fully
understood to be one read-only SELECT over fully qualified tables in connected
catalogs. Anything else is refused, including anything the parser can't make sense
of. The read-only database users are the next layer down; this layer exists so the
agent gets a clear refusal, and so that bad SQL never reaches a source at all.

Two parser behaviours matter here, both checked against sqlglot 30.29:
  * `TABLE(cat.system.query(query => '...'))` parses as a table with an empty name.
    It is Trino's raw SQL passthrough, so a check that only reads table names would
    let it through. Hence every table's `this` must be a plain identifier.
  * `a.b.c.d` parses with `c` tucked inside a Dot, so `table.name` reports `d`. The
    same check rejects it, so we never allowlist a different table from the one
    that would run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import sqlglot
from pydantic import BaseModel, ConfigDict
from sqlglot import exp
from sqlglot.errors import ErrorLevel, SqlglotError

from agent.harness.errors import SqlRejected

TRINO = "trino"
DEFAULT_MAX_ROWS = 5000
MAX_SQL_LENGTH = 100_000

# Trino lower-cases identifiers, even quoted ones, so all of these compare lower-case.
SYSTEM_SCHEMAS = frozenset({"information_schema", "pg_catalog", "pg_toast", "mysql", "performance_schema", "sys",
                            "system", "metadata", "runtime", "jdbc"})
SYSTEM_CATALOGS = frozenset({"system", "jmx", "memory"})

IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
ALLOWED_ROOTS = (exp.Select, exp.SetOperation)
FORBIDDEN_NODES = (exp.DML, exp.DDL, exp.Command, exp.Into, exp.Lock, exp.Set, exp.Use, exp.Transaction, exp.Commit,
                   exp.Rollback, exp.Grant, exp.Copy, exp.Drop, exp.Alter, exp.TruncateTable)

# Trino has none of these, so they would fail anyway. Refusing them here gives the
# agent a clear reason, and covers engines added later.
FORBIDDEN_FUNCTIONS = frozenset({"pg_sleep", "pg_sleep_for", "pg_sleep_until", "pg_read_file", "pg_read_binary_file",
                                 "pg_ls_dir", "pg_terminate_backend", "pg_cancel_backend", "lo_import", "lo_export",
                                 "dblink", "dblink_exec", "load_file", "sleep", "benchmark", "get_lock",
                                 "sys_exec", "sys_eval", "xp_cmdshell", "query_to_xml"})


class RowScope(BaseModel):
    """Which rows of one catalog a user may read."""

    model_config = ConfigDict(frozen=True)

    column: str
    values: tuple[str, ...]
    numeric: bool = False
    scoped_tables: frozenset[str] = frozenset()  # "catalog.schema.table" holding the column
    shared_tables: frozenset[str] = frozenset()  # readable by everyone, such as exchange rates


class SqlPolicy(BaseModel):
    """What one run may touch. Built server-side from the connected sources."""

    model_config = ConfigDict(frozen=True)

    allowed_catalogs: frozenset[str]
    allowed_system_tables: frozenset[str] = frozenset()  # "catalog.schema.table"
    max_rows: int = DEFAULT_MAX_ROWS
    row_scopes: dict[str, RowScope] = {}  # by catalog


@dataclass(frozen=True)
class GuardedQuery:
    sql: str
    tables: tuple[str, ...]
    limit: int


def scope_clause(column: str, values: tuple[str, ...] | list[str], *, numeric: bool) -> str:
    """A WHERE condition for a row scope, shared by the SQL rewrite and Superset guest tokens.

    The column comes from an admin and the values from the database, but this text goes into
    SQL, so neither is trusted: the identifier must be plain, numbers must be numbers, and
    strings are quoted. An empty scope matches nothing."""
    if not IDENTIFIER.fullmatch(column):
        raise SqlRejected(f"{column!r} isn't a usable column name")
    if not values:
        return "1 = 0"
    if numeric:
        literals = [str(int(value)) for value in values]
    else:
        literals = ["'" + str(value).replace("'", "''") + "'" for value in values]
    return f"{column} IN ({', '.join(literals)})"


def guard(sql: str, policy: SqlPolicy, *, dialect: str = TRINO, add_limit: bool = True) -> GuardedQuery:
    """Validate `sql` and return a safe, LIMITed, comment-free version of it.

    `add_limit=False` is for SQL stored as a virtual dataset: Superset adds the limit
    itself when charts query it, and a LIMIT in the dataset would silently cut totals.
    Raises SqlRejected, with a reason the agent can act on.
    """
    if len(sql) > MAX_SQL_LENGTH:
        raise SqlRejected(f"query is too long ({len(sql)} characters, limit {MAX_SQL_LENGTH})")

    tree = _parse_single(sql, dialect)
    if not isinstance(tree, ALLOWED_ROOTS):
        raise SqlRejected(f"only SELECT queries are allowed, not {type(tree).__name__.upper()}")

    _reject_forbidden_nodes(tree)
    _reject_forbidden_functions(tree)
    tables = _check_tables(tree, policy)
    _apply_row_scopes(tree, policy)
    limit = _apply_limit(tree, policy.max_rows) if add_limit else 0
    return GuardedQuery(sql=tree.sql(dialect=TRINO, comments=False), tables=tables, limit=limit)


def to_trino(sql: str, source_dialect: str) -> str:
    """Rewrite one statement from a source's dialect into Trino's."""
    try:
        results = sqlglot.transpile(sql, read=source_dialect, write=TRINO, error_level=ErrorLevel.RAISE)
    except (SqlglotError, ValueError) as error:
        raise SqlRejected(f"could not translate this query from {source_dialect}: {_first_line(error)}") from error
    if len(results) != 1:
        raise SqlRejected("send exactly one statement at a time")
    return results[0]


def _parse_single(sql: str, dialect: str) -> exp.Expression:
    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect, error_level=ErrorLevel.RAISE) if s is not None]
    except (SqlglotError, ValueError, RecursionError) as error:
        raise SqlRejected(f"could not parse this query: {_first_line(error)}") from error
    if len(statements) != 1:
        raise SqlRejected(f"send exactly one statement at a time, not {len(statements)}")
    return statements[0]


def _reject_forbidden_nodes(tree: exp.Expression) -> None:
    # Walking the whole tree catches writes hidden inside a read, such as
    # WITH x AS (DELETE ... RETURNING *) SELECT * FROM x.
    for node in tree.walk():
        if isinstance(node, FORBIDDEN_NODES):
            raise SqlRejected(f"{type(node).__name__.upper()} is not allowed; queries must be read-only")


def _reject_forbidden_functions(tree: exp.Expression) -> None:
    for node in tree.find_all(exp.Anonymous):
        name = node.name.lower()
        if name in FORBIDDEN_FUNCTIONS:
            raise SqlRejected(f"function {name} is not allowed")
    # catalog.schema.fn(...) reaches functions outside the default namespace.
    for node in tree.find_all(exp.Dot):
        if isinstance(node.expression, exp.Func):
            raise SqlRejected("schema-qualified function calls are not allowed")


def _cte_names(tree: exp.Expression) -> set[str]:
    return {cte.alias.lower() for cte in tree.find_all(exp.CTE)}


def _check_tables(tree: exp.Expression, policy: SqlPolicy) -> tuple[str, ...]:
    ctes = _cte_names(tree)
    catalogs = {c.lower() for c in policy.allowed_catalogs}
    system_tables = {t.lower() for t in policy.allowed_system_tables}
    seen: list[str] = []

    for table in tree.find_all(exp.Table):
        target = table.this
        if not isinstance(target, exp.Identifier):
            raise SqlRejected("table functions and unusual table references are not allowed; name a table directly")

        name, schema, catalog = table.name.lower(), table.db.lower(), table.catalog.lower()
        if not schema and not catalog and name in ctes:
            continue
        if not (catalog and schema):
            raise SqlRejected(f"table {table.sql(dialect=TRINO)!r} must be fully qualified as catalog.schema.table")

        qualified = f"{catalog}.{schema}.{name}"
        if qualified in system_tables:
            pass
        elif catalog in SYSTEM_CATALOGS or schema in SYSTEM_SCHEMAS or schema.startswith("pg_") or name.startswith("pg_"):
            raise SqlRejected(f"system table {qualified} is not allowed")
        elif catalog not in catalogs:
            raise SqlRejected(f"catalog {catalog!r} is not a connected source; available: {sorted(catalogs)}")
        if qualified not in seen:
            seen.append(qualified)
    return tuple(seen)


def _apply_row_scopes(tree: exp.Expression, policy: SqlPolicy) -> None:
    """Replace each scoped table with a subquery of the rows this user may read. Every table is
    rewritten where it appears, so it holds in joins, CTEs, subqueries and unions alike, and
    however the model writes its own WHERE clause, since an OR can't reach inside a subquery."""
    if not policy.row_scopes:
        return
    ctes = _cte_names(tree)
    for table in list(tree.find_all(exp.Table)):
        name, schema, catalog = table.name.lower(), table.db.lower(), table.catalog.lower()
        if not schema and not catalog and name in ctes:
            continue
        scope = policy.row_scopes.get(catalog)
        if scope is None:
            continue
        qualified = f"{catalog}.{schema}.{name}"
        if qualified in {t.lower() for t in scope.shared_tables}:
            continue
        if qualified not in {t.lower() for t in scope.scoped_tables}:
            raise SqlRejected(f"table {qualified} can't be limited to your data, so it isn't available to you")
        alias = table.alias or table.name
        source = table.copy()
        source.set("alias", None)
        condition = sqlglot.parse_one(scope_clause(scope.column, scope.values, numeric=scope.numeric), read=TRINO)
        table.replace(exp.select(exp.Star()).from_(source).where(condition).subquery(alias=alias, copy=False))


def _apply_limit(tree: exp.Expression, max_rows: int) -> int:
    """Keep a smaller literal LIMIT, and cap everything else. Covers FETCH FIRST too,
    which is stored in the same place."""
    current = tree.args.get("limit")
    requested = _literal_limit(current)
    if requested is not None and requested <= max_rows and isinstance(current, exp.Limit):
        return requested
    limit = min(requested, max_rows) if requested is not None else max_rows
    tree.set("limit", exp.Limit(expression=exp.Literal.number(limit)))
    return limit


def _literal_limit(node: exp.Expression | None) -> int | None:
    if node is None:
        return None
    count = node.args.get("expression") if isinstance(node, exp.Limit) else node.args.get("count")
    if isinstance(count, exp.Literal) and not count.is_string:
        try:
            return int(count.this)
        except ValueError:
            return None
    return None


def _first_line(error: Exception) -> str:
    return str(error).splitlines()[0][:200] if str(error) else type(error).__name__
