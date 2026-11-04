"""The profiler subagent: turns a measured SourceProfile into a skill.

The model proposes; the harness verifies. The agent finishes by calling `submit_skill`,
which runs every metric and example, outside the model's control, and only what really
executes reaches the skill. Failures go straight back for repair a limited number of
times, and what still fails is dropped and recorded.

Submission is an ordinary tool rather than `response_format` for two reasons. A malformed
submission becomes feedback the model can fix instead of an exception that loses the run.
And it needs no forced `tool_choice`, which Claude Opus 5.5 rejects, so it behaves the
same on every provider.
"""

from __future__ import annotations

import argparse
import logging
import re
import uuid
from pathlib import Path
from typing import Any

from deepagents import create_deep_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool, StructuredTool
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from agent.config import Settings, get_settings
from agent.db import apply_schema, create_pool
from agent.harness.audit import AuditLog, RunContext
from agent.harness.budgets import BudgetLimits, RunBudget
from agent.harness.errors import ErrorCategory, NovaError, QueryFailure
from agent.harness.executor import SqlExecutor
from agent.harness.guardrails import SqlPolicy
from agent.harness.logs import configure_logging
from agent.harness.middleware import BudgetMiddleware
from agent.harness.untrusted import wrap_untrusted
from agent.llm import build_model
from agent.tools.profiler import Profiler, SourceProfile, TableProfile
from agent.tools.skill_store import Metric, SemanticLayer, SkillFrontmatter, SkillStore, skill_slug
from agent.tools.sql_tools import make_run_readonly_sql

logger = logging.getLogger(__name__)

PROMPT = Path(__file__).parent.parent / "prompts" / "profiler.md"
MAX_REPAIR_ROUNDS = 2
RECURSION_LIMIT = 200
METRIC_KEY = re.compile(r"^[a-z][a-z0-9_]*$")


class ProposedMetric(BaseModel):
    key: str = Field(description="snake_case identifier, such as net_revenue")
    label: str
    sql: str = Field(description="One aggregate expression")
    base: str = Field(description="FROM clause without the word FROM, with fully qualified tables")
    filters: list[str] = Field(default_factory=list, description="WHERE conditions, ANDed together")
    unit: str | None = None
    description: str


class ProposedDimension(BaseModel):
    name: str
    description: str


class ProposedExample(BaseModel):
    question: str
    sql: str


class SkillDraft(BaseModel):
    description: str = Field(description="Under 800 characters: what it holds and when to load it")
    body: str = Field(description="The markdown guide")
    metrics: list[ProposedMetric] = Field(min_length=1)
    dimensions: list[ProposedDimension] = Field(default_factory=list)
    joins: list[str] = Field(default_factory=list)
    examples: list[ProposedExample] = Field(min_length=1)


class Failure(BaseModel):
    item: str
    reason: str


class VerificationReport(BaseModel):
    metric_values: dict[str, float] = Field(default_factory=dict)
    verified_examples: list[ProposedExample] = Field(default_factory=list)
    failures: list[Failure] = Field(default_factory=list)


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


def metric_sql(metric: ProposedMetric) -> str:
    where = f" WHERE {' AND '.join(f'({condition})' for condition in metric.filters)}" if metric.filters else ""
    return f"SELECT {metric.sql} AS value FROM {metric.base}{where}"


def verify_draft(draft: SkillDraft, *, executor: SqlExecutor, policy: SqlPolicy, context: RunContext,
                 budget: RunBudget) -> VerificationReport:
    """Run everything the draft claims. The model has no say in the outcome."""
    report = VerificationReport()
    seen: set[str] = set()
    for metric in draft.metrics:
        name = f"metric {metric.key}"
        if not METRIC_KEY.match(metric.key) or metric.key in seen:
            report.failures.append(Failure(item=name, reason="the key must be unique snake_case"))
            continue
        seen.add(metric.key)
        try:
            result = executor.run(metric_sql(metric), policy=policy, context=context, budget=budget)
        except NovaError as error:
            if error.category is ErrorCategory.BUDGET:
                raise
            report.failures.append(Failure(item=name, reason=error.message))
            continue
        value = result.rows[0][0] if result.rows else None
        if len(result.rows) != 1 or len(result.columns) != 1:
            report.failures.append(Failure(item=name, reason="a metric must return exactly one value"))
        elif value is None:
            report.failures.append(Failure(item=name, reason="it returned NULL: the filters may match nothing"))
        else:
            report.metric_values[metric.key] = float(value)

    for example in draft.examples:
        name = f"example {example.question!r}"
        try:
            result = executor.run(example.sql, policy=policy, context=context, budget=budget)
        except NovaError as error:
            if error.category is ErrorCategory.BUDGET:
                raise
            report.failures.append(Failure(item=name, reason=error.message))
            continue
        if result.row_count == 0:
            report.failures.append(Failure(item=name, reason="it returned no rows"))
        else:
            report.verified_examples.append(example)
    return report


class Submission:
    """What `submit_skill` has received so far."""

    def __init__(self) -> None:
        self.attempts = 0
        self.draft: SkillDraft | None = None
        self.report: VerificationReport | None = None
        self.accepted = False


def make_submit_skill(submission: Submission, *, executor: SqlExecutor, policy: SqlPolicy, context: RunContext,
                      budget: RunBudget) -> BaseTool:
    def submit_skill(**fields: Any) -> str:
        """Submit the finished skill. This is how you finish.

        Every metric and example is run when you call this. If any fail you'll get the
        failures back: fix them and submit the complete skill again.
        """
        draft = SkillDraft(**fields)
        report = verify_draft(draft, executor=executor, policy=policy, context=context, budget=budget)
        submission.attempts += 1
        submission.draft, submission.report = draft, report
        logger.info("draft verified", extra={"attempt": submission.attempts, "metrics": len(report.metric_values),
                                             "failures": len(report.failures)})
        if not report.failures:
            submission.accepted = True
            return "Accepted: everything ran. You're done."
        if submission.attempts > MAX_REPAIR_ROUNDS:
            submission.accepted = True
            return "Accepted without the items that failed. You're done."
        return repair_message(report)

    return StructuredTool.from_function(func=submit_skill, name="submit_skill", args_schema=SkillDraft)


def build_agent(model: BaseChatModel, *, tools: list[BaseTool], budget: RunBudget):
    return create_deep_agent(model=model, tools=tools, system_prompt=PROMPT.read_text(),
                             middleware=[BudgetMiddleware(budget)])


def task_message(profile: SourceProfile) -> str:
    return (f"Write the skill for the source `{profile.source_slug}`, which is Trino catalog "
            f"`{profile.catalog}` (underlying dialect: {profile.dialect}).\n\n"
            f"Measurements:\n{wrap_untrusted(describe_profile(profile))}")


def repair_message(report: VerificationReport) -> str:
    problems = "\n".join(f"- {f.item}: {f.reason}" for f in report.failures)
    return f"Not accepted. I ran everything and these failed:\n{problems}\n\nFix them and call submit_skill again."


def draft_skill(model: BaseChatModel, profile: SourceProfile, *, executor: SqlExecutor, context: RunContext,
                budget: RunBudget) -> tuple[SkillDraft, VerificationReport]:
    """Run the agent until it has submitted a skill the harness has checked."""
    policy = SqlPolicy(allowed_catalogs=frozenset({profile.catalog}), max_rows=1000)
    submission = Submission()
    agent = build_agent(model, budget=budget, tools=[
        make_run_readonly_sql(executor, policy=policy, context=context, budget=budget),
        make_submit_skill(submission, executor=executor, policy=policy, context=context, budget=budget),
    ])
    config = {"recursion_limit": RECURSION_LIMIT}
    result = agent.invoke({"messages": [HumanMessage(task_message(profile))]}, config)
    if not submission.accepted:
        # It stopped without finishing, so remind it once rather than fail the run.
        reminder = HumanMessage("You stopped before submit_skill accepted the skill. Call submit_skill now.")
        agent.invoke({"messages": [*result["messages"], reminder]}, config)

    if submission.draft is None or submission.report is None or not submission.report.metric_values:
        raise QueryFailure("the profiler produced no metric that actually runs")
    return submission.draft, submission.report


def to_semantic_layer(draft: SkillDraft, report: VerificationReport) -> SemanticLayer:
    """Keep only what was verified."""
    metrics = {m.key: Metric(label=m.label, sql=m.sql, base=m.base, filters=m.filters, unit=m.unit,
                             description=m.description)
               for m in draft.metrics if m.key in report.metric_values}
    return SemanticLayer(metrics=metrics, dimensions={d.name: d.description for d in draft.dimensions},
                         joins=draft.joins)


def to_examples_sql(report: VerificationReport) -> str:
    # Questions are collapsed to one line so that none can break out of its SQL comment.
    return "\n\n".join(f"-- {' '.join(example.question.split())}\n{example.sql.rstrip().rstrip(';')};"
                       for example in report.verified_examples)


def register_source(pool: ConnectionPool, *, slug: str, display_name: str, catalog: str, dialect: str) -> str:
    with pool.connection() as conn:
        return str(conn.execute(
            """INSERT INTO sources (slug, display_name, trino_catalog, dialect) VALUES (%s, %s, %s, %s)
               ON CONFLICT (slug) DO UPDATE SET display_name = EXCLUDED.display_name RETURNING id""",
            (slug, display_name, catalog, dialect)).fetchone()[0])


def generate_skill(*, model: BaseChatModel, store: SkillStore, executor: SqlExecutor, source_id: str, slug: str,
                   catalog: str, dialect: str, context: RunContext, budget: RunBudget) -> SkillFrontmatter:
    """Measure a source, have the subagent interpret it, and write the verified skill."""
    profile = Profiler(executor, context=context, budget=budget).profile(slug, catalog, dialect)
    draft, report = draft_skill(model, profile, executor=executor, context=context, budget=budget)
    change = [f"Profiled {len(profile.tables)} tables. {len(report.metric_values)} metrics and "
              f"{len(report.verified_examples)} examples verified by running them."]
    change += [f"- Dropped {failure.item}: {failure.reason}" for failure in report.failures]
    return store.write(source_id=source_id, slug=slug, description=draft.description, body=draft.body, profile=profile,
                       semantic_layer=to_semantic_layer(draft, report), examples_sql=to_examples_sql(report),
                       change="\n".join(change))


def budget_from(settings: Settings) -> RunBudget:
    return RunBudget(BudgetLimits(max_steps=settings.nova_max_steps, max_tokens=settings.nova_max_tokens,
                                  max_wall_seconds=settings.nova_max_wall_seconds, max_sql_queries=1000))


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
    front = generate_skill(
        model=build_model(settings), store=SkillStore(settings.nova_skills_dir, pool),
        executor=SqlExecutor(settings.trino_url, AuditLog(pool)),
        source_id=register_source(pool, slug=slug, display_name=args.name or args.catalog, catalog=args.catalog,
                                  dialect=args.dialect),
        slug=slug, catalog=args.catalog, dialect=args.dialect,
        context=RunContext(run_id=f"profile-{uuid.uuid4().hex[:12]}", user_id="system"), budget=budget)
    print(f"wrote {front.name} v{front.version}: {budget.snapshot()}")
    pool.close()


if __name__ == "__main__":
    main()
