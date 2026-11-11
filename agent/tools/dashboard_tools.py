"""The agent's tools for building dashboards on the semantic layer.

Every chart is a semantic query that Cube runs as the person the run is for, so nothing here can
show anyone a number their own dashboard wouldn't. Charts and layout are saved as one spec under
the conversation's key, so a retry or a follow-up changes what's there rather than adding to it.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from agent.dashboards import catalogs_of, filter_reach, published_model, run_chart, today
from agent.harness.errors import InvalidRequest, NovaError
from agent.harness.untrusted import wrap_untrusted
from agent.runtime import Runtime
from agent.semantic.charts import KPIS, ChartSpec, DashboardFilter, DashboardSpec, Slot
from agent.semantic.model import Dimension, Measure
from agent.semantic.query import SemanticQuery
from agent.semantic.review import certified
from agent.tools.renderer import render_dashboard, review_screenshot
from agent.tools.sql_tools import NoInput, reporting_failures

MAX_ROWS_SHOWN = 20
MAX_CHARTS = 24  # far more than a dashboard needs; this stops a confused repair loop


class QueryInput(BaseModel):
    query: SemanticQuery


class ChartInput(BaseModel):
    spec: ChartSpec


class KeyInput(BaseModel):
    key: str


class FilterInput(BaseModel):
    name: str = Field(description="What people see, such as Region")
    member: str = Field(description="The dimension to filter on, cube.member")


class LayoutInput(BaseModel):
    title: str
    rows: list[list[Slot]] = Field(min_length=1, description="Rows of chart keys, top to bottom. Widths are columns of "
                                                             "12; leave 0 to share a row evenly")
    filters: list[FilterInput] = Field(default_factory=list)
    time_filter: bool = Field(default=True, description="Let people change the time window")


class FinishInput(BaseModel):
    summary: str = Field(description="What was built, for the user. A few sentences.")
    definitions: list[str] = Field(description="The definitions used, such as 'Revenue excludes cancelled orders'")


class DefinitionInput(BaseModel):
    source: str = Field(description="The skill's name")
    cube: str
    measure: Measure | None = None
    dimension: Dimension | None = None
    reason: str = Field(description="Why the dashboard needs it")


def rows_text(rows: list[dict[str, Any]]) -> str:
    return wrap_untrusted(json.dumps(rows[:MAX_ROWS_SHOWN], default=str))


def make_semantic_query(runtime: Runtime) -> BaseTool:
    @reporting_failures
    def semantic_query(query: SemanticQuery) -> str:
        """Ask the semantic layer a question and see the answer: measures, dimensions, time and filters
        by their cube.member names, exactly as a chart would. Use it to check numbers and explore."""
        runtime.budget.spend_query()
        result = runtime.cube.load(runtime.cube_identity, query.to_cube())
        model = published_model(runtime.store)
        mark = "certified" if certified(model, query.members()) else "uses AI-inferred definitions"
        return f"{len(result.data)} rows ({mark}):\n{rows_text(result.data)}"

    return StructuredTool.from_function(func=semantic_query, name="semantic_query", args_schema=QueryInput)


def make_add_definition(runtime: Runtime) -> BaseTool:
    @reporting_failures
    def add_definition(source: str, cube: str, reason: str, measure: Measure | None = None,
                       dimension: Dimension | None = None) -> str:
        """Add a measure or dimension the dashboard needs and the model lacks. It's checked like the
        profiler's definitions, then published as AI-inferred, for a person to review in the Catalog.
        Never use it to change an existing definition: propose_skill_update does that."""
        from agent.semantic.access import tenancy_of
        from agent.semantic.verify import Verifier

        member = measure or dimension
        if member is None or (measure and dimension):
            raise InvalidRequest("give exactly one of measure and dimension")
        found = next((s for s in runtime.sources if s.slug == source), None)
        model = runtime.store.read_model(source) if found else None
        if found is None or model is None:
            raise InvalidRequest(f"no published model for {source}; sources: {[s.slug for s in runtime.sources]}")
        verifier = Verifier(cube=runtime.cube, executor=runtime.executor, skills_dir=runtime.store.root, slug=source,
                            profile=runtime.store.read_profile(source), context=runtime.context, budget=runtime.budget,
                            tenancy=tenancy_of(found), taken=runtime.store.cube_names(excluding=source))
        runtime.store.add_member(source, cube, member, reason=reason, verifier=verifier, identity=runtime.cube_identity)
        return f"Added {cube}.{member.name}. It's AI-inferred until a person reviews it, so charts using it aren't certified."

    return StructuredTool.from_function(func=add_definition, name="add_definition", args_schema=DefinitionInput)


def make_builder_tools(runtime: Runtime) -> dict[str, BaseTool]:
    state = runtime.state

    def current_spec() -> DashboardSpec:
        if state.spec is None:
            state.spec = DashboardSpec(title=(state.plan or {}).get("title") or "Dashboard")
        return state.spec

    def save() -> None:
        runtime.save_dashboard(catalogs_of(current_spec(), runtime.store))

    def check(chart: ChartSpec) -> dict[str, Any]:
        runtime.budget.spend_query()
        return run_chart(runtime.cube, runtime.cube_identity, DashboardSpec(title="check", charts=[chart]), chart.key,
                         model=published_model(runtime.store))

    @reporting_failures
    def create_chart(spec: ChartSpec) -> str:
        """Create or update a chart. The same key updates the same chart. It's run straight away, and
        trends stop at the last complete period automatically."""
        current = current_spec()
        exists = current.chart(spec.key) is not None
        if not exists and len(current.charts) >= MAX_CHARTS:
            return f"FAILED: this dashboard already has {len(current.charts)} charts. Reuse their keys."
        result = check(spec)  # refused by Cube if it names anything that doesn't exist
        current.charts = [c for c in current.charts if c.key != spec.key] + [spec]
        state.spec = DashboardSpec.model_validate(current.model_dump())
        save()
        runtime.emit("chart_ready", key=spec.key, title=spec.title, viz=spec.viz.value, slug=state.dashboard_key)
        window = f", window {result['window'][0]} to {result['window'][1]}" if result["window"] else ""
        if result["row_count"] == 0:
            return f"{spec.key} was saved but returned no rows{window}. Check its filters and window."
        if result["truncated"]:
            return f"{spec.key}: stopped at its limit of {spec.limit} rows, so it may not show everything{window}."
        return f"{spec.key}: {result['row_count']} rows{window}"

    @reporting_failures
    def remove_chart(key: str) -> str:
        """Remove a chart, and its place in the layout."""
        current = current_spec()
        if current.chart(key) is None:
            return f"FAILED: no chart {key}. Charts: {[c.key for c in current.charts]}"
        rows = [[slot for slot in row if slot.key != key] for row in current.rows]
        state.spec = current.model_copy(update={"charts": [c for c in current.charts if c.key != key],
                                                "rows": [row for row in rows if row]})
        save()
        return f"Removed {key}."

    @reporting_failures
    def set_layout(title: str, rows: list[list[Slot]], filters: list[FilterInput] | None = None,
                   time_filter: bool = True) -> str:
        """Lay the charts out and set the dashboard's filters. Pass the complete layout: a chart left out
        isn't shown. Each filter applies to every chart Cube can join it to."""
        current = current_spec()
        laid = DashboardSpec(title=title, charts=current.charts, rows=rows, time_filter=time_filter,
                             filters=[DashboardFilter(name=f.name, member=f.member) for f in filters or []])
        laid = filter_reach(runtime.cube, runtime.cube_identity, laid)
        useless = [f.name for f in laid.filters if not f.applies_to]
        if useless:
            return f"FAILED: these filters reach no chart: {useless}. Use a dimension the charts' cubes join to."
        first = not state.laid_out
        state.spec, state.laid_out = laid, True
        save()
        runtime.emit("dashboard_ready" if first else "dashboard_updated", slug=state.dashboard_key, title=title)
        reach = "; ".join(f"{f.name} reaches {len(f.applies_to)} of {len(laid.charts)} charts" for f in laid.filters)
        unplaced = laid.unplaced()
        note = f" Not placed, so not shown: {unplaced}." if unplaced else ""
        return f"Laid out {sum(len(row) for row in rows)} charts in {len(rows)} rows. {reach}.{note}".replace(" .", ".")

    @reporting_failures
    def get_dashboard() -> str:
        """Read the dashboard so far: its charts, layout and filters, with every setting."""
        if state.spec is None:
            return "Nothing has been built in this conversation yet."
        return state.spec.model_dump_json(exclude_defaults=True)

    @reporting_failures
    def inspect_dashboard() -> str:
        """Show what chart data can't: the window each chart covers today, which charts each filter
        reaches, whether each chart is certified, and anything not placed."""
        if state.spec is None:
            return "Nothing has been built in this conversation yet."
        model = published_model(runtime.store)
        current = state.spec
        return json.dumps({
            "title": current.title, "time_filter": current.time_filter, "not_placed": current.unplaced(),
            "filters": [{"name": f.name, "member": f.member,
                         "missing_from": [c.key for c in current.charts if c.key not in f.applies_to]} for f in current.filters],
            "charts": [{"key": c.key, "title": c.title, "viz": c.viz.value, "window": c.window(today()),
                        "measures": c.measures + c.line_measures, "dimensions": c.dimensions,
                        "certified": certified(model, c.members())} for c in current.charts],
        })

    @reporting_failures
    def get_chart_data(key: str) -> str:
        """Run a chart as it's saved and show the result, with the dates its window covers, both included."""
        if state.spec is None or state.spec.chart(key) is None:
            return f"FAILED: no chart {key}."
        runtime.budget.spend_query()
        result = run_chart(runtime.cube, runtime.cube_identity, state.spec, key)
        window = f"\nwindow: {result['window'][0]} to {result['window'][1]}" if result["window"] else ""
        return f"{result['row_count']} rows{window}\n{rows_text(result['rows'])}"

    @reporting_failures
    def render_dashboard_screenshot() -> str:
        """Open the dashboard in a real browser and have it reviewed. Use this once the charts' data looks
        right: it's the only check that proves they draw."""
        if not state.laid_out:
            return "FAILED: lay the dashboard out first."
        placed = sum(len(row) for row in state.spec.rows)
        rendered = render_dashboard(runtime.settings, state.dashboard_key, user_id=runtime.context.user_id,
                                    expected_charts=placed)
        review = review_screenshot(runtime.model, rendered.screenshot)
        return (f"{rendered.charts} of {placed} charts drew. Errors on the page: {rendered.errors or 'none'}."
                f"\nReview:\n{review}")

    @reporting_failures
    def finish_dashboard(summary: str, definitions: list[str]) -> str:
        """Finish. Every chart is run when you call this, and you're only done if they all return data.
        Otherwise you get the failures back to fix."""
        if not state.laid_out or state.spec is None:
            return "FAILED: there's no dashboard yet: create the charts, then set_layout."
        state.finish_attempts += 1
        current = state.spec
        placed = {slot.key for row in current.rows for slot in row}
        problems, warnings = [], []
        for chart in current.charts:
            if chart.key not in placed:
                continue
            try:
                result = check(chart)
            except NovaError as error:
                if error.category.value == "budget":
                    raise
                problems.append(f"{chart.title} ({chart.key}): {error.message}")
                continue
            if result["row_count"] == 0:
                problems.append(f"{chart.title} ({chart.key}): no rows")
            elif result["truncated"] and chart.viz not in KPIS:
                warnings.append(f"{chart.title}: shows only the first {chart.limit} rows")
            if not result["certified"]:
                warnings.append(f"{chart.title}: uses definitions no person has reviewed yet")
        for item in current.filters:
            missing = [c.title for c in current.charts if c.key in placed and c.key not in item.applies_to]
            if missing:
                warnings.append(f"The {item.name} filter doesn't apply to: {', '.join(missing)}")
        state.warnings = warnings
        if problems and state.finish_attempts <= runtime.settings.nova_chart_repair_rounds:
            lines = "\n".join(f"- {problem}" for problem in problems)
            return f"Not finished. These charts failed:\n{lines}\n\nFix them and call finish_dashboard again."
        state.warnings += [f"Failed: {problem}" for problem in problems]
        # Charts left out of the layout are dropped, so the saved dashboard is what people see.
        state.spec = current.model_copy(update={"charts": [c for c in current.charts if c.key in placed]})
        state.finished, state.summary, state.definitions = True, summary, definitions
        save()
        return "Finished."

    tools = [
        StructuredTool.from_function(func=create_chart, name="create_chart", args_schema=ChartInput),
        StructuredTool.from_function(func=remove_chart, name="remove_chart", args_schema=KeyInput),
        StructuredTool.from_function(func=set_layout, name="set_layout", args_schema=LayoutInput),
        StructuredTool.from_function(func=get_dashboard, name="get_dashboard", args_schema=NoInput),
        StructuredTool.from_function(func=inspect_dashboard, name="inspect_dashboard", args_schema=NoInput),
        StructuredTool.from_function(func=get_chart_data, name="get_chart_data", args_schema=KeyInput),
        StructuredTool.from_function(func=render_dashboard_screenshot, name="render_dashboard_screenshot", args_schema=NoInput),
        StructuredTool.from_function(func=finish_dashboard, name="finish_dashboard", args_schema=FinishInput),
    ]
    return {tool.name: tool for tool in tools}
