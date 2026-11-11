"""Checking a drafted data model by running it. The model proposes; this decides.

A draft is written to `skills/_drafts/<id>/`, which Cube compiles as a separate model that only a
token naming it can reach, and every measure, example and tenant limit is run through Cube. Joins
and keys are checked with plain SQL through the guard, because a wrong one doesn't fail: a join
declared many-to-one that really matches several rows silently multiplies every total it touches.

What fails is reported for repair, and once repairs run out, `finalize` drops the failing parts
until what's left all works. Nothing unverified is ever published.
"""

from __future__ import annotations

import json
import re
import shutil
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

from pydantic import BaseModel, Field

from agent.harness.audit import RunContext
from agent.harness.budgets import RunBudget
from agent.harness.errors import ErrorCategory, NovaError, QueryFailure
from agent.harness.executor import SqlExecutor
from agent.harness.guardrails import SqlPolicy
from agent.semantic.access import Tenancy, apply_tenancy
from agent.semantic.client import CubeClient, CubeIdentity
from agent.semantic.model import TENANT_DIMENSION, Cube, DataModel, Dimension, write_atomically
from agent.semantic.query import Example
from agent.semantic.validate import PLACEHOLDER, Problem, check_model, render_checked
from agent.tools.profiler import SourceProfile

COLUMN = re.compile(r"^(?:\{CUBE\}\.)?([a-z_][a-z0-9_]*)$|^\{CUBE\.([a-z_][a-z0-9_]*)\}$")
NO_TENANT = "__nova_no_tenant__"
MAX_ROUNDS = 4
PARALLEL = 8  # checks at once: each is one small query, and Cube, Trino and the clients take them concurrently
STALE_DRAFT_SECONDS = 3600


class Verification(BaseModel):
    values: dict[str, float] = Field(default_factory=dict, description="cube.measure -> its value over all rows")
    examples: list[Example] = Field(default_factory=list)
    problems: list[Problem] = Field(default_factory=list)

    @property
    def blocking(self) -> list[Problem]:
        return [problem for problem in self.problems if not problem.example]


@dataclass
class Verifier:
    cube: CubeClient
    executor: SqlExecutor
    skills_dir: Path
    slug: str
    profile: SourceProfile
    context: RunContext
    budget: RunBudget
    tenancy: Tenancy = field(default_factory=Tenancy)
    taken: set[str] = field(default_factory=set)

    @property
    def catalog(self) -> str:
        return self.profile.catalog

    @property
    def policy(self) -> SqlPolicy:
        return SqlPolicy(allowed_catalogs=frozenset({self.catalog}), max_rows=10)

    def verify(self, model: DataModel, examples: list[Example]) -> Verification:
        tables = {f"{self.catalog}.{table.qualified}".lower() for table in self.profile.tables}
        problems = check_model(model, catalog=self.catalog, tables=tables, taken=self.taken)
        if problems:
            # Cube would refuse to compile it, or worse, compile something unsafe.
            return Verification(problems=problems)
        scoped, manifest = apply_tenancy(model, catalog=self.catalog, profile=self.profile, tenancy=self.tenancy)
        self._remove_stale_drafts()
        draft = uuid.uuid4().hex[:16]
        directory = self.skills_dir / "_drafts" / draft
        try:
            write_atomically(directory / "model" / f"{self.slug}.yml", render_checked(scoped))
            write_atomically(directory / "model" / "access.json", json.dumps(manifest, indent=2))
            identity = CubeIdentity(sub=f"nova-{self.context.run_id}", role="admin", catalogs=[self.catalog],
                                    model=f"draft-{draft}")
            try:
                self.cube.meta(identity)
            except NovaError as error:
                return Verification(problems=[_compile_problem(error.message, model)])
            report = Verification()
            checks: list[Callable[[], None]] = []
            for cube in model.cubes:
                checks += [partial(self._check_measure, cube, measure, identity, report) for measure in cube.measures]
                checks += [partial(self._check_dimension, cube, dimension, identity, report) for dimension in cube.dimensions
                           if not dimension.primary_key and dimension.public is not False]
                checks.append(partial(self._check_primary_key, cube, report))
                checks += [partial(self._check_join, cube, join.name, join.relationship, join.sql, model, report)
                           for join in cube.joins]
            for name, rule in manifest["cubes"].items():
                if rule.get("tenant_dimension"):
                    checks.append(partial(self._check_tenant_limit, model.cube(name), identity, report))
            passed = [False] * len(examples)
            for index, example in enumerate(examples):
                checks.append(partial(self._check_example, example, identity, report, passed, index))
            _run_all(checks)
            report.examples = [example for example, ok in zip(examples, passed) if ok]
            report.problems.sort(key=lambda problem: problem.item)  # checks finish in any order
            return report
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def verify_member(self, model: DataModel, cube_name: str, member_name: str, identity: CubeIdentity) -> None:
        """Check one new definition in an otherwise published model, as the person asking for it, so
        their own limits apply. Raises with every reason it fails."""
        tables = {f"{self.catalog}.{table.qualified}".lower() for table in self.profile.tables}
        problems = check_model(model, catalog=self.catalog, tables=tables, taken=self.taken)
        if not problems:
            scoped, manifest = apply_tenancy(model, catalog=self.catalog, profile=self.profile, tenancy=self.tenancy)
            self._remove_stale_drafts()
            draft = uuid.uuid4().hex[:16]
            directory = self.skills_dir / "_drafts" / draft
            try:
                write_atomically(directory / "model" / f"{self.slug}.yml", render_checked(scoped))
                write_atomically(directory / "model" / "access.json", json.dumps(manifest, indent=2))
                as_draft = identity.model_copy(update={"model": f"draft-{draft}"})
                report = Verification()
                try:
                    self.cube.meta(as_draft)
                except NovaError as error:
                    raise QueryFailure(f"Cube can't compile the model with it: {error.message}") from error
                cube = model.cube(cube_name)
                member = cube.member(member_name)
                if isinstance(member, Dimension):
                    self._check_dimension(cube, member, as_draft, report)
                else:
                    self._check_measure(cube, member, as_draft, report)
                problems = report.problems
            finally:
                shutil.rmtree(directory, ignore_errors=True)
        if problems:
            raise QueryFailure("; ".join(f"{p.item}: {p.reason}" for p in problems)[:2000])

    def _load(self, identity: CubeIdentity, query: dict) -> list[dict]:
        self.budget.spend_query()
        return self.cube.load(identity, query).data

    def _remove_stale_drafts(self) -> None:
        """A run that was killed mid-check can't clean up after itself, so the next one does."""
        drafts = self.skills_dir / "_drafts"
        for directory in drafts.iterdir() if drafts.is_dir() else []:
            if time.time() - directory.stat().st_mtime > STALE_DRAFT_SECONDS:
                shutil.rmtree(directory, ignore_errors=True)

    def _check_measure(self, cube: Cube, measure, identity: CubeIdentity, report: Verification) -> None:
        member = f"{cube.name}.{measure.name}"
        try:
            rows = self._load(identity, {"measures": [member]})
        except NovaError as error:
            _reraise_budget(error)
            report.problems.append(Problem(item=f"measure {member}", cube=cube.name, member=measure.name,
                                           reason=error.message))
            return
        value = rows[0].get(member) if rows else None
        if value is None:
            report.problems.append(Problem(item=f"measure {member}", cube=cube.name, member=measure.name,
                                           reason="it returned NULL: its filters may match nothing"))
        else:
            report.values[member] = float(value)

    def _check_dimension(self, cube: Cube, dimension, identity: CubeIdentity, report: Verification) -> None:
        member = f"{cube.name}.{dimension.name}"
        query = ({"timeDimensions": [{"dimension": member, "granularity": "month"}], "measures": [], "limit": 1}
                 if dimension.type == "time" else {"dimensions": [member], "limit": 1})
        try:
            self._load(identity, query)
        except NovaError as error:
            _reraise_budget(error)
            report.problems.append(Problem(item=f"dimension {member}", cube=cube.name, member=dimension.name,
                                           reason=error.message))

    def _check_primary_key(self, cube: Cube, report: Verification) -> None:
        keys = [dimension for dimension in cube.dimensions if dimension.primary_key]
        expression = _on_alias(keys[0].sql, "a") if len(keys) == 1 else None
        if expression is None:
            return
        rows = self._run(f"SELECT count(*), count(DISTINCT {expression}) FROM {_source(cube)} a", report, cube)
        if rows and rows[0][0] != rows[0][1]:
            report.problems.append(Problem(
                item=f"cube {cube.name}", cube=cube.name, member=keys[0].name,
                reason=f"its primary key {keys[0].name} isn't unique: {rows[0][0]:,} rows but {rows[0][1]:,} distinct "
                       "non-null values. Cube relies on it to keep joins from multiplying rows"))

    def _check_join(self, cube: Cube, target_name: str, relationship: str, sql: str, model: DataModel,
                    report: Verification) -> None:
        """A many-to-one or one-to-one join must not add rows. If it does, every sum over this
        cube that touches the join is too high."""
        target = model.cube(target_name)
        if relationship == "one_to_many" or target is None:
            return
        condition = _join_condition(sql, cube, target)
        if condition is None:
            return  # it refers to calculated members; Cube's own checks are all there is
        rows = self._run(f"SELECT (SELECT count(*) FROM {_source(cube)} a), "
                         f"(SELECT count(*) FROM {_source(cube)} a LEFT JOIN {_source(target)} b ON {condition})",
                         report, cube)
        if rows and rows[0][1] > rows[0][0]:
            report.problems.append(Problem(
                item=f"cube {cube.name} join to {target_name}", cube=cube.name, join=target_name,
                reason=f"declared {relationship}, but rows match several {target_name} rows: the join turns "
                       f"{rows[0][0]:,} rows into {rows[0][1]:,}. Fix the condition, or declare the join one_to_many "
                       f"from {target_name} instead"))

    def _check_tenant_limit(self, cube: Cube | None, identity: CubeIdentity, report: Verification) -> None:
        """Run the cube as someone limited to a tenant, which makes Cube apply the tenant filter."""
        if cube is None:
            return
        members = [f"{cube.name}.{m.name}" for m in cube.measures] or [
            f"{cube.name}.{d.name}" for d in cube.dimensions
            if not d.primary_key and d.public is not False and d.name != TENANT_DIMENSION and d.type != "time"]
        if not members:
            return
        limited = identity.model_copy(update={"scopes": {self.catalog: [NO_TENANT]}})
        key = "measures" if cube.measures else "dimensions"
        try:
            self._load(limited, {key: members[:1], "limit": 1})
        except NovaError as error:
            _reraise_budget(error)
            report.problems.append(Problem(
                item=f"cube {cube.name}", cube=cube.name,
                reason=f"it can't be limited to one tenant, because the tenant column {self.tenancy.column} isn't in "
                       f"its rows ({error.message}). Select it in the cube's sql"))

    def _check_example(self, example: Example, identity: CubeIdentity, report: Verification, passed: list[bool],
                       index: int) -> None:
        try:
            rows = self._load(identity, example.query.to_cube())
        except NovaError as error:
            _reraise_budget(error)
            report.problems.append(Problem(item=f"example {example.question!r}", example=example.question,
                                           reason=error.message))
            return
        if not rows:
            report.problems.append(Problem(item=f"example {example.question!r}", example=example.question,
                                           reason="it returned no rows"))
        else:
            passed[index] = True

    def _run(self, sql: str, report: Verification, cube: Cube) -> list[list]:
        try:
            return self.executor.run(sql, policy=self.policy, context=self.context, budget=self.budget).rows
        except NovaError as error:
            _reraise_budget(error)
            report.problems.append(Problem(item=f"cube {cube.name}", cube=cube.name, reason=error.message))
            return []


def _run_all(checks: list[Callable[[], None]]) -> None:
    """Run the checks concurrently. The first to raise, such as a spent budget, stops the rest."""
    with ThreadPoolExecutor(max_workers=PARALLEL, thread_name_prefix="verify") as pool:
        futures = [pool.submit(check) for check in checks]
        try:
            for future in futures:
                future.result()
        except BaseException:
            for future in futures:
                future.cancel()
            raise


def _reraise_budget(error: NovaError) -> None:
    if error.category is ErrorCategory.BUDGET:
        raise error


def _source(cube: Cube) -> str:
    return cube.sql_table if cube.sql_table else f"({cube.sql})"


def _on_alias(sql: str, alias: str) -> str | None:
    """A plain column reference, rewritten onto a table alias. Anything more is skipped."""
    match = COLUMN.match(sql.strip())
    if not match:
        return None
    return f"{alias}.{match.group(1) or match.group(2)}"


def _join_condition(sql: str, cube: Cube, target: Cube) -> str | None:
    """The join's condition in plain SQL over aliases a (this cube) and b (the other), or None if
    it refers to anything other than columns."""
    out, position = [], 0
    for match in PLACEHOLDER.finditer(sql):
        out.append(sql[position:match.start()])
        position = match.end()
        name, _, member = match.group(1).strip().partition(".")
        alias = "a" if name in ("CUBE", cube.name) else "b" if name == target.name else None
        if alias is None:
            return None
        if not member:
            out.append(alias)
            continue
        definition = (cube if alias == "a" else target).member(member)
        if definition is None:
            column = f"{alias}.{member}"  # {cube.column} reads a column directly
        elif isinstance(definition, Dimension):
            column = _on_alias(definition.sql, alias)
        else:
            column = None
        if column is None:
            return None
        out.append(column)
    out.append(sql[position:])
    return "".join(out)


def _compile_problem(message: str, model: DataModel) -> Problem:
    """Cube compiles a model as a whole, but its errors usually name the cube at fault."""
    named = [cube.name for cube in model.cubes if cube.name in message]
    return Problem(item=f"cube {named[0]}" if named else "model", cube=named[0] if len(named) == 1 else None,
                   reason=f"Cube can't compile it: {message}")


def prune(model: DataModel, problems: list[Problem]) -> tuple[DataModel, list[str]]:
    """Drop what failed: a member, a join, or a whole cube."""
    model = model.model_copy(deep=True)
    dropped: list[str] = []
    for problem in problems:
        cube = model.cube(problem.cube) if problem.cube else None
        if cube is None:
            continue
        if problem.join:
            cube.joins = [join for join in cube.joins if join.name != problem.join]
        elif problem.member:
            cube.dimensions = [m for m in cube.dimensions if m.name != problem.member]
            cube.measures = [m for m in cube.measures if m.name != problem.member]
            cube.segments = [m for m in cube.segments if m.name != problem.member]
        else:
            model.cubes = [other for other in model.cubes if other.name != cube.name]
        dropped.append(f"{problem.item}: {problem.reason}")
    return model, dropped


def finalize(verifier: Verifier, model: DataModel, examples: list[Example], *,
             verified: Verification | None = None) -> tuple[DataModel, Verification, list[str]]:
    """Verify, dropping whatever fails, until everything left works. `verified` is a report on
    exactly this model and these examples, if there is one, which saves running it all again."""
    dropped: list[str] = []
    for round_ in range(MAX_ROUNDS):
        report = verified if round_ == 0 and verified is not None else verifier.verify(model, examples)
        if not report.blocking:
            dropped += [f"{p.item}: {p.reason}" for p in report.problems]
            return model, report, dropped
        if any(problem.cube is None for problem in report.blocking):
            raise QueryFailure("the model doesn't work as a whole: "
                               + "; ".join(p.reason for p in report.blocking if p.cube is None)[:2000])
        model, notes = prune(model, report.blocking)
        dropped += notes
        if not model.cubes:
            raise QueryFailure("nothing in the model works")
    raise QueryFailure("couldn't reach a model where everything works")

