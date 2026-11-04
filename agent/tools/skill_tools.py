"""The agent's tools for sources and skills."""

from __future__ import annotations

import contextlib
import logging

import yaml
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from agent.harness.errors import InvalidRequest
from agent.runtime import Runtime
from agent.tools.profiler import Profiler
from agent.tools.skill_store import Freshness, Metric, describe_change, parse_frontmatter, refresh, skill_slug
from agent.tools.sql_tools import NoInput, reporting_failures

logger = logging.getLogger(__name__)


class SkillNameInput(BaseModel):
    name: str = Field(description="The skill's name, as listed in Skills or by list_sources")


class ProposeSkillUpdateInput(BaseModel):
    name: str
    key: str = Field(description="The metric's key")
    metric: Metric
    reason: str = Field(description="Why it should change, in the user's words where possible")


def make_source_tools(runtime: Runtime) -> list[BaseTool]:
    store = runtime.store
    by_skill = {source.slug: source for source in runtime.sources}

    def resolve(name: str) -> str | None:
        """Models often use the catalog name, or a source's display name."""
        candidates = [name]
        with contextlib.suppress(InvalidRequest):
            candidates.append(skill_slug(name))
        return next((candidate for candidate in candidates if candidate in by_skill), None)

    def unknown(name: str) -> str:
        return f"FAILED: no skill named {name!r}. Skills: {sorted(by_skill)}"

    def profiler() -> Profiler:
        return Profiler(runtime.executor, context=runtime.context, budget=runtime.budget)

    @reporting_failures
    def list_sources() -> str:
        """List the connected data sources and whether each has a skill."""
        lines = []
        for source in runtime.sources:
            if store.exists(source.slug):
                front = store.read_frontmatter(source.slug)
                state = f"skill `{source.slug}` v{front.version} ({front.status.value})"
            else:
                state = "no skill yet"
            lines.append(f"- {source.display_name}: catalog `{source.trino_catalog}`, {state}")
        return "\n".join(lines) or "No sources are connected."

    @reporting_failures
    def check_skill_freshness(name: str) -> str:
        """Compare a skill with the live schema, and report anything that changed."""
        name = resolve(name) or name
        source = by_skill.get(name)
        if source is None:
            return unknown(name)
        report = store.check_freshness(name, profiler().read_shape(source.trino_catalog))
        if report.status is Freshness.STALE:
            return f"stale:\n{describe_change(report.diff)}"
        return report.status.value

    @reporting_failures
    def load_skill(name: str) -> str:
        """Load a skill: its guide, metric definitions and tested examples. Do this before
        writing SQL against a source. A skill that has fallen behind the live schema is
        brought up to date first."""
        name = resolve(name) or name
        source = by_skill.get(name)
        if source is None or not store.exists(name):
            return unknown(name)
        note = ""
        report = store.check_freshness(name, profiler().read_shape(source.trino_catalog))
        if report.status is Freshness.STALE:
            directory = store.path(name)
            front, body = parse_frontmatter((directory / "SKILL.md").read_text())
            refresh(store, profiler(), slug=name, catalog=source.trino_catalog, source_id=source.id,
                    description=front.description, body=body, examples_sql=(directory / "examples.sql").read_text())
            note = ("This skill was updated because the schema changed. Check that its definitions still hold:\n"
                    f"{describe_change(report.diff)}\n\n")
        directory = store.path(name)
        _, body = parse_frontmatter((directory / "SKILL.md").read_text())
        layer = store.read_semantic_layer(name).model_dump(mode="json", exclude={"proposed"})
        return (f"{note}{body}\n\n## Definitions (metrics.yaml)\n```yaml\n{yaml.safe_dump(layer, sort_keys=False)}```\n\n"
                f"## Tested examples\n```sql\n{(directory / 'examples.sql').read_text()}```")

    @reporting_failures
    def propose_skill_update(name: str, key: str, metric: Metric, reason: str) -> str:
        """Suggest a change to a metric definition, when the user corrects one. A person
        accepts or rejects it; it doesn't take effect until then."""
        name = resolve(name) or name
        if not store.exists(name):
            return unknown(name)
        store.propose_metric(name, key, metric, reason=reason)
        return "Proposed. It takes effect once someone accepts it on the Skills page."

    return [
        StructuredTool.from_function(func=list_sources, name="list_sources", args_schema=NoInput),
        StructuredTool.from_function(func=check_skill_freshness, name="check_skill_freshness",
                                     args_schema=SkillNameInput),
        StructuredTool.from_function(func=load_skill, name="load_skill", args_schema=SkillNameInput),
        StructuredTool.from_function(func=propose_skill_update, name="propose_skill_update",
                                     args_schema=ProposeSkillUpdateInput),
    ]
