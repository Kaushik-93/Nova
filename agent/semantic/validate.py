"""Checks on a data model before Cube loads it.

A model is drafted by a model that has read the source's contents, so it is untrusted, and Cube
sends its SQL to Trino without passing Nova's SQL guard. These checks are therefore an allowlist,
like the guard, and fail closed:

  * Cube treats every `sql` field as a Python f-string, so a `{...}` placeholder may only name a
    cube or a member. Anything else, or a stray brace, is refused.
  * Cube renders a whole `.yml` file as a Jinja template if it contains `{{`, `}}`, `{%` or `%}`
    anywhere, descriptions included, so no field may contain them.
  * A cube's own `sql` must be one plain SELECT over this source's tables that passes the guard,
    with no subqueries, aggregates or window functions. Each row of a cube must be one row of
    the data, or a tenant's limit on the cube's rows wouldn't hold for the values derived from it.
  * Member expressions are row-level expressions over the cube's columns: no tables, no
    subqueries, nothing the guard forbids. Only `number` measures may aggregate.
"""

from __future__ import annotations

import re

import sqlglot
from pydantic import BaseModel
from sqlglot import exp
from sqlglot.errors import SqlglotError

from agent.harness.errors import SqlRejected
from agent.harness.guardrails import FORBIDDEN_FUNCTIONS, FORBIDDEN_NODES, TRINO, SqlPolicy, guard
from agent.semantic.model import CUBE_SEPARATOR, TENANT_DIMENSION, Cube, DataModel

NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
REFERENCE = re.compile(r"^(?P<cube>CUBE|[a-z][a-z0-9_]*)(?:\.(?P<member>[a-z][a-z0-9_]*))?$")
JINJA = re.compile(r"{%|%}|{{|}}")
TABLE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")
RESERVED_PREFIX = "nova_"


class Problem(BaseModel):
    """What's wrong, and where. `cube`, `member` and `join` say what can be dropped to get past it;
    with none of them set, the whole model is at fault."""

    item: str
    reason: str
    cube: str | None = None
    member: str | None = None
    join: str | None = None
    example: str | None = None  # an example question that failed; dropping it is enough


def check_model(model: DataModel, *, catalog: str, tables: set[str], taken: set[str] = frozenset()) -> list[Problem]:
    """Everything wrong with a drafted model. `tables` holds the source's tables as
    catalog.schema.table, and `taken` the cube names other sources already use."""
    problems: list[Problem] = []
    names = [cube.name for cube in model.cubes]
    if not model.cubes:
        problems.append(Problem(item="model", reason="it defines no cubes"))
    for cube in model.cubes:
        if not NAME.match(cube.name) or not cube.name.startswith(f"{catalog}{CUBE_SEPARATOR}"):
            problems.append(Problem(item=f"cube {cube.name}", cube=cube.name, reason=f"cube names are lowercase "
                                    f"snake_case starting with {catalog}{CUBE_SEPARATOR}, such as "
                                    f"{catalog}{CUBE_SEPARATOR}orders"))
        if names.count(cube.name) > 1 or cube.name in taken:
            problems.append(Problem(item=f"cube {cube.name}", reason="another cube already has this name"))
        problems += _check_cube(cube, model, catalog=catalog, tables=tables)
    problems += _check_text(model)
    return problems


def _check_cube(cube: Cube, model: DataModel, *, catalog: str, tables: set[str]) -> list[Problem]:
    problems: list[Problem] = []
    item = f"cube {cube.name}"
    members = [m.name for m in [*cube.dimensions, *cube.measures, *cube.segments]]
    for name in members:
        if not NAME.match(name) or name.startswith(RESERVED_PREFIX):
            problems.append(Problem(item=f"{item}.{name}", cube=cube.name, member=name, reason="member names are "
                                    f"lowercase snake_case, and may not start with {RESERVED_PREFIX}"))
        if members.count(name) > 1:
            problems.append(Problem(item=f"{item}.{name}", cube=cube.name, reason="two members of this cube have "
                                                                                 "this name"))
    if not any(dimension.primary_key for dimension in cube.dimensions):
        problems.append(Problem(item=item, cube=cube.name, reason="a cube needs a primary_key dimension, which keeps "
                                                                  "joins from multiplying rows"))

    if cube.sql_table is not None:
        if not TABLE.match(cube.sql_table) or cube.sql_table.split(".")[0] != catalog:
            problems.append(Problem(item=item, cube=cube.name, reason=f"sql_table must be {catalog}.schema.table"))
        elif cube.sql_table not in tables:
            problems.append(Problem(item=item, cube=cube.name, reason=f"there's no table {cube.sql_table} in this "
                                                                      "source"))
    else:
        problems += [Problem(item=item, cube=cube.name, reason=reason) for reason in _check_select(cube.sql or "", catalog)]

    for join in cube.joins:
        where = f"{item} join to {join.name}"
        if model.cube(join.name) is None or join.name == cube.name:
            problems.append(Problem(item=where, cube=cube.name, join=join.name,
                                    reason="joins must name another cube in this model"))
        problems += [Problem(item=where, cube=cube.name, join=join.name, reason=r)
                     for r in _check_fragment(join.sql, cube, model, condition=True)]
    for dimension in cube.dimensions:
        problems += [Problem(item=f"{item}.{dimension.name}", cube=cube.name, member=dimension.name, reason=r)
                     for r in _check_fragment(dimension.sql, cube, model)]
    for measure in cube.measures:
        where = f"{item}.{measure.name}"
        if measure.sql:
            problems += [Problem(item=where, cube=cube.name, member=measure.name, reason=r)
                         for r in _check_fragment(measure.sql, cube, model, aggregate=measure.type == "number")]
        for condition in measure.filters:
            problems += [Problem(item=f"{where} filter", cube=cube.name, member=measure.name, reason=r)
                         for r in _check_fragment(condition.sql, cube, model, condition=True)]
    for segment in cube.segments:
        problems += [Problem(item=f"{item}.{segment.name}", cube=cube.name, member=segment.name, reason=r)
                     for r in _check_fragment(segment.sql, cube, model, condition=True)]
    return problems


def _check_select(sql: str, catalog: str) -> list[str]:
    if PLACEHOLDER.search(sql) or "{" in sql or "}" in sql:
        return ["a cube's sql can't contain braces; name tables directly"]
    try:
        guarded = guard(sql, SqlPolicy(allowed_catalogs=frozenset({catalog})), add_limit=False)
        tree = sqlglot.parse_one(guarded.sql, read=TRINO)
    except SqlRejected as error:
        return [error.message]
    except SqlglotError as error:
        return [f"could not parse this sql: {error}"]
    if not isinstance(tree, exp.Select):
        return ["a cube's sql must be a single SELECT"]
    if any(node is not tree for node in tree.find_all(exp.Select)):
        return ["a cube's sql can't have subqueries or CTEs; use joins between cubes instead"]
    if tree.find(exp.AggFunc) or tree.args.get("group"):
        return ["a cube's sql must keep one row per row of data; aggregate in measures instead"]
    if tree.find(exp.Window):
        return ["a cube's sql can't use window functions"]
    if tree.args.get("limit"):
        return ["a cube's sql can't have a LIMIT"]
    return []


def _check_fragment(sql: str, cube: Cube, model: DataModel, *, condition: bool = False,
                    aggregate: bool = False) -> list[str]:
    """A member's expression, a filter, a segment or a join condition."""
    problems: list[str] = []
    substituted = sql
    for index, match in enumerate(PLACEHOLDER.finditer(sql)):
        reference = REFERENCE.match(match.group(1).strip())
        if reference is None:
            return [f"{{{match.group(1)}}} isn't a reference to a cube or member; braces may only hold one"]
        problem = _check_reference(reference.group("cube"), reference.group("member"), cube, model)
        if problem:
            problems.append(problem)
        target = "t" if reference.group("cube") == "CUBE" else f"nova_ref_{index}"
        replacement = f"{target}.{reference.group('member')}" if reference.group("member") else target
        substituted = substituted.replace(match.group(0), replacement, 1)
    if "{" in substituted or "}" in substituted:
        return ["unmatched brace; braces may only hold a reference such as {CUBE}.column or {other_cube}"]

    statement = f"SELECT 1 FROM t WHERE {substituted}" if condition else f"SELECT {substituted} FROM t"
    try:
        tree = sqlglot.parse_one(statement, read=TRINO)
    except (SqlglotError, ValueError, RecursionError) as error:
        return [*problems, f"could not parse: {str(error).splitlines()[0][:200]}"]
    if not isinstance(tree, exp.Select) or len(list(tree.find_all(exp.Select))) != 1:
        return [*problems, "an expression can't contain a query or subquery"]
    if len(list(tree.find_all(exp.Table))) != 1:
        return [*problems, "an expression can't name a table; refer to columns of the cube"]
    for node in tree.walk():
        if isinstance(node, FORBIDDEN_NODES):
            return [*problems, f"{type(node).__name__.upper()} is not allowed"]
    for node in tree.find_all(exp.Anonymous):
        if node.name.lower() in FORBIDDEN_FUNCTIONS:
            return [*problems, f"function {node.name.lower()} is not allowed"]
    for node in tree.find_all(exp.Dot):
        if isinstance(node.expression, exp.Func):
            return [*problems, "schema-qualified function calls are not allowed"]
    if tree.find(exp.Window):
        problems.append("window functions are not allowed")
    if tree.find(exp.AggFunc) and not aggregate:
        problems.append("only a measure of type number may use aggregate functions; others aggregate by their type")
    return problems


def _check_reference(cube_name: str, member: str | None, cube: Cube, model: DataModel) -> str | None:
    if cube_name == "CUBE":
        if member is not None and cube.member(member) is None and not _is_column(member):
            return f"{{CUBE.{member}}} names no member of {cube.name}"
        return None
    if member is None:
        if cube.member(cube_name) is not None or model.cube(cube_name) is not None:
            return None
        return f"{{{cube_name}}} is neither a member of {cube.name} nor a cube"
    target = model.cube(cube_name)
    if target is None:
        return f"{{{cube_name}.{member}}} refers to a cube that isn't in this model"
    if target.member(member) is None:
        return f"{{{cube_name}.{member}}} names no member of {cube_name}"
    return None


def _is_column(name: str) -> bool:
    # {CUBE.column} reads a column directly, which Cube allows; the name just has to be plain.
    return NAME.match(name) is not None


def _check_text(model: DataModel) -> list[Problem]:
    """Cube runs a model file through Jinja if any of it looks like a template."""
    problems = []
    for cube in model.cubes:
        fields = [cube.title, cube.description, cube.sql, cube.sql_table]
        for member in [*cube.dimensions, *cube.measures, *cube.segments]:
            fields += [member.title, member.description, getattr(member, "sql", None)]
        fields += [join.sql for join in cube.joins]
        fields += [f.sql for measure in cube.measures for f in measure.filters]
        if any(text and JINJA.search(text) for text in fields):
            problems.append(Problem(item=f"cube {cube.name}", cube=cube.name,
                                    reason="no text may contain {{, }}, {% or %}"))
    return problems


def render_checked(model: DataModel) -> str:
    """The model as YAML, refusing anything Cube would treat as a template. This is the last
    check before the file is written, whatever the caller has already checked."""
    text = model.to_yaml()
    if JINJA.search(text):
        raise SqlRejected("the model contains text Cube would run as a template")
    reserved = [d.name for cube in model.cubes for d in cube.dimensions
                if d.name.startswith(RESERVED_PREFIX) and d.name != TENANT_DIMENSION]
    if reserved:
        raise SqlRejected(f"reserved member names: {reserved}")
    return text
