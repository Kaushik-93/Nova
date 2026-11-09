"""The profiler subagent: turns a measured SourceProfile into a skill, whose semantic layer is a
Cube data model.

The model proposes; the harness verifies. The agent finishes by calling `submit_skill`, which
checks the model statically, compiles it in Cube as a draft no one else can reach, and runs every
measure, join, key, tenant limit and example (agent/semantic/verify.py), outside the model's
control. Failures go straight back for repair a limited number of times, and what still fails is
dropped and recorded. Definitions people have reviewed are never changed by a model.

Submission is an ordinary tool rather than `response_format` for two reasons. A malformed
submission becomes feedback the model can fix instead of an exception that loses the run.
And it needs no forced `tool_choice`, which Claude Opus 5.5 rejects, so it behaves the
same on every provider.
"""

from __future__ import annotations

import argparse
import logging
import uuid
from pathlib import Path
from typing import Any

from deepagents import create_deep_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from agent.config import Settings, get_settings
from agent.db import apply_schema, create_pool
from agent.harness.audit import AuditLog, RunContext
from agent.harness.budgets import BudgetLimits, RunBudget
from agent.harness.errors import QueryFailure
from agent.harness.executor import SqlExecutor
from agent.harness.guardrails import SqlPolicy
from agent.harness.logs import configure_logging
from agent.harness.middleware import EventSink, RunMiddleware
from agent.harness.untrusted import wrap_untrusted
from agent.llm import build_model
from agent.semantic.access import Tenancy, tenancy_of
from agent.semantic.client import CubeClient
from agent.semantic.model import Cube, DataModel, MemberMeta, Status
from agent.semantic.query import Example
from agent.semantic.review import merge_reviewed
from agent.semantic.verify import Verification, Verifier, finalize
from agent.tools.profiler import Profiler, SourceProfile, TableProfile
from agent.tools.skill_store import SkillFrontmatter, SkillStore, skill_slug
from agent.tools.sources import get_source_by_slug, register_source
from agent.tools.sql_tools import make_run_readonly_sql

logger = logging.getLogger(__name__)

PROMPT = Path(__file__).parent.parent / "prompts" / "profiler.md"
MAX_REPAIR_ROUNDS = 2
RECURSION_LIMIT = 200


class SkillDraft(BaseModel):
    description: str = Field(description="Under 800 characters: what it holds and when to load it")
    body: str = Field(description="The markdown guide")
    cubes: list[Cube] = Field(min_length=1, description="The Cube data model: usually one cube per table")
    examples: list[Example] = Field(min_length=1)

    def model(self) -> DataModel:
        """The draft as a data model, with every definition marked inferred: only a person can
        mark one reviewed, whatever the draft says."""
        model = DataModel(cubes=self.cubes).model_copy(deep=True)
        for cube in model.cubes:
            for member in [*cube.dimensions, *cube.measures]:
                member.meta = MemberMeta(status=Status.INFERRED, unit=member.meta.unit)
        return model


def describe_column(column) -> str:
    facts = [column.type, column.role.value]
    if column.null_pct:
        facts.append(f"{column.null_pct:g}% null")
    if column.distinct is not None:
        facts.append(f"about {column.distinct:,} distinct")
    if column.min is not None:
        facts.append(f"range {column.min} to {column.max}")
    if column.top_values:
        facts.append("values: " + ", ".join(f"{value} ({count:,})" for value, count in column.top_values))
    if column.comment:
        facts.append(f"comment: {column.comment}")
    return f"- {column.name}: " + "; ".join(facts)


def describe_table(table: TableProfile, catalog: str) -> str:
    lines = [f"### {catalog}.{table.qualified}", f"{table.row_count:,} rows; measured on {table.sample_rows:,} "
             f"({table.sample_method} sample{', BIASED towards the oldest rows' if table.sample_is_biased else ''})"]
    if table.comment:
        lines.append(f"comment: {table.comment}")
    if table.primary_key:
        lines.append(f"primary key: {', '.join(table.primary_key)}"
                     + ("" if table.primary_key_declared else " (inferred)"))
    for key in table.foreign_keys:
        how = "declared" if key.declared else f"inferred, {key.overlap:.0%} of values match"
        lines.append(f"foreign key: {key.column} -> {key.references_table}.{key.references_column} ({how})")
    return "\n".join([*lines, *(describe_column(c) for c in table.columns)])


def describe_profile(profile: SourceProfile) -> str:
    """The measurements as compact text. Comments and values are in here, so this
    must only reach the model through wrap_untrusted."""
    sections = [describe_table(t, profile.catalog) for t in profile.tables]
    if profile.gotchas:
        sections.append("### Possible traps\n" + "\n".join(
            f"- {g.kind.value} on {g.table}.{g.column}: {g.detail}" for g in profile.gotchas))
    if profile.unreadable:
        sections.append("### Not readable with these credentials\n" + "\n".join(f"- {t}" for t in profile.unreadable))
    return "\n\n".join(sections)


class Submission:
    """What `submit_skill` has received so far."""

    def __init__(self) -> None:
        self.attempts = 0
        self.draft: SkillDraft | None = None
        self.report: Verification | None = None
        self.accepted = False


def make_submit_skill(submission: Submission, *, verifier: Verifier, reviewed: DataModel | None) -> BaseTool:
    def submit_skill(**fields: Any) -> str:
        """Submit the finished skill. This is how you finish.

        The model is compiled and every measure, join, key and example is run when you call this.
        If anything fails you'll get the failures back: fix them and submit the complete skill again.
        """
        draft = SkillDraft(**fields)
        merged, _ = merge_reviewed(reviewed, draft.model())
        report = verifier.verify(merged, draft.examples)
        submission.attempts += 1
        submission.draft, submission.report = draft, report
        logger.info("draft verified", extra={"attempt": submission.attempts, "measures": len(report.values),
                                             "problems": len(report.problems)})
        if not report.problems:
            submission.accepted = True
            return "Accepted: everything ran. You're done."
        if submission.attempts > MAX_REPAIR_ROUNDS:
            submission.accepted = True
            return "Accepted without the parts that failed. You're done."
        return repair_message(report)

    return StructuredTool.from_function(func=submit_skill, name="submit_skill", args_schema=SkillDraft)


def build_agent(model: BaseChatModel, *, tools: list[BaseTool], budget: RunBudget, run_id: str = "",
                sink: EventSink | None = None):
    return create_deep_agent(model=model, tools=tools, system_prompt=PROMPT.read_text(),
                             middleware=[RunMiddleware(budget, agent="profiler", run_id=run_id, sink=sink)])


def task_message(profile: SourceProfile, reviewed: DataModel | None = None) -> str:
    kept = ""
    if reviewed is not None:
        names = [f"{cube.name}.{member.name}" for cube in reviewed.cubes for member in [*cube.dimensions, *cube.measures]
                 if member.meta.status is Status.REVIEWED]
        if names:
            kept = ("\n\nPeople have reviewed these definitions, and they will be kept as they are whatever you "
                    "submit, so keep their cubes' names: " + ", ".join(names))
    return (f"Write the skill for the source `{profile.source_slug}`, which is Trino catalog "
            f"`{profile.catalog}` (underlying dialect: {profile.dialect}). Name every cube "
            f"`{profile.catalog}__<name>`.{kept}\n\n"
            f"Measurements:\n{wrap_untrusted(describe_profile(profile))}")


def repair_message(report: Verification) -> str:
    problems = "\n".join(f"- {p.item}: {p.reason}" for p in report.problems)
    return f"Not accepted. I checked everything and these failed:\n{problems}\n\nFix them and call submit_skill again."


def draft_skill(model: BaseChatModel, profile: SourceProfile, *, verifier: Verifier, executor: SqlExecutor,
                reviewed: DataModel | None, context: RunContext, budget: RunBudget,
                sink: EventSink | None = None) -> tuple[SkillDraft, Verification]:
    """Run the agent until it has submitted a skill the harness has checked."""
    policy = SqlPolicy(allowed_catalogs=frozenset({profile.catalog}), max_rows=1000)
    submission = Submission()
    agent = build_agent(model, budget=budget, run_id=context.run_id, sink=sink, tools=[
        make_run_readonly_sql(executor, policy=policy, context=context, budget=budget),
        make_submit_skill(submission, verifier=verifier, reviewed=reviewed),
    ])
    config = {"recursion_limit": RECURSION_LIMIT}
    result = agent.invoke({"messages": [HumanMessage(task_message(profile, reviewed))]}, config)
    if not submission.accepted:
        # It stopped without finishing, so remind it once rather than fail the run.
        reminder = HumanMessage("You stopped before submit_skill accepted the skill. Call submit_skill now.")
        agent.invoke({"messages": [*result["messages"], reminder]}, config)
    if submission.draft is None or submission.report is None:
        raise QueryFailure("the profiler never submitted a skill")
    return submission.draft, submission.report


def generate_skill(*, model: BaseChatModel, store: SkillStore, executor: SqlExecutor, cube: CubeClient, source_id: str,
                   slug: str, catalog: str, dialect: str, context: RunContext, budget: RunBudget,
                   tenancy: Tenancy | None = None, sink: EventSink | None = None) -> SkillFrontmatter:
    """Measure a source, have the subagent interpret it, and write the verified skill."""
    def on_table(table: str, done: int, total: int) -> None:
        if sink:
            sink({"event": "profile_table", "agent": "profiler", "table": table, "done": done, "total": total})

    tenancy = tenancy or Tenancy()
    profile = Profiler(executor, context=context, budget=budget, on_table=on_table).profile(slug, catalog, dialect)
    verifier = Verifier(cube=cube, executor=executor, skills_dir=store.root, slug=slug, profile=profile, context=context,
                        budget=budget, tenancy=tenancy, taken=store.cube_names(excluding=slug))
    reviewed = store.read_model(slug)
    draft, last = draft_skill(model, profile, verifier=verifier, executor=executor, reviewed=reviewed, context=context,
                              budget=budget, sink=sink)
    # The last submission was checked as exactly this model, so finalize needn't run it all again.
    merged, proposals = merge_reviewed(reviewed, draft.model())
    final, report, dropped = finalize(verifier, merged, draft.examples, verified=last)
    cubes = len(final.cubes)
    change = [f"Profiled {len(profile.tables)} tables. {cubes} cubes, {len(report.values)} measures and "
              f"{len(report.examples)} examples verified by running them."]
    change += [f"- Dropped {note}" for note in dropped]
    return store.write(source_id=source_id, slug=slug, description=draft.description, body=draft.body, profile=profile,
                       model=final, tenancy=tenancy, examples=report.examples, proposals=proposals,
                       change="\n".join(change))


PROFILE_WALL_SECONDS = 1800  # measuring, drafting and checking every definition: a limit for runaways, not a target


def budget_from(settings: Settings) -> RunBudget:
    return RunBudget(BudgetLimits(max_steps=settings.nova_max_steps, max_tokens=settings.nova_max_tokens,
                                  max_wall_seconds=max(settings.nova_max_wall_seconds, PROFILE_WALL_SECONDS),
                                  max_sql_queries=1500))


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the skill for one source.")
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--dialect", required=True)
    parser.add_argument("--name", help="display name; defaults to the catalog")
    args = parser.parse_args()

    configure_logging()
    settings = get_settings()
    pool = create_pool(settings.database_url)
    apply_schema(pool)
    slug = skill_slug(args.catalog)
    budget = budget_from(settings)
    cube = CubeClient(settings.cube_url, settings.cubejs_api_secret)
    source_id = register_source(pool, slug=slug, display_name=args.name or args.catalog, catalog=args.catalog,
                                dialect=args.dialect)
    # A source an administrator has set up as multi-tenant keeps those settings.
    source = get_source_by_slug(pool, slug)
    tenancy = tenancy_of(source) if source else Tenancy()
    front = generate_skill(
        model=build_model(settings), store=SkillStore(settings.nova_skills_dir, pool),
        executor=SqlExecutor(settings.trino_url, AuditLog(pool)), cube=cube,
        source_id=source_id, slug=slug, catalog=args.catalog, dialect=args.dialect, tenancy=tenancy,
        context=RunContext(run_id=f"profile-{uuid.uuid4().hex[:12]}", user_id="system"), budget=budget)
    print(f"wrote {front.name} v{front.version}: {budget.snapshot()}")
    cube.close()
    pool.close()


if __name__ == "__main__":
    main()
