"""Dashboards: stored as a spec, drawn by Nova, with every number fetched from Cube as the person
looking. The agent's tools and the API both come through here, so a chart the agent checked is
exactly the chart people see.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from typing import Any

from psycopg_pool import ConnectionPool

from agent.harness.errors import InvalidRequest, NotFound, NovaError
from agent.semantic.charts import ChartSpec, DashboardSpec
from agent.semantic.client import CubeClient, CubeIdentity, CubeResult
from agent.semantic.describe import annotate
from agent.semantic.model import DataModel
from agent.semantic.query import QueryFilter
from agent.semantic.review import certified
from agent.tools.skill_store import SkillStore

MAX_FILTER_OPTIONS = 500
REACH_WORKERS = 8


def today() -> date:
    return datetime.now(timezone.utc).date()


def load_spec(pool: ConnectionPool, slug: str) -> tuple[DashboardSpec | None, dict[str, Any]]:
    """A dashboard's spec, or None for one built in Superset before Nova drew its own, with its row."""
    with pool.connection() as conn:
        cursor = conn.execute("""SELECT slug, thread_id, title, summary, spec, catalogs, created_by, updated_at
                                 FROM dashboards WHERE slug = %s""", (slug,))
        row = cursor.fetchone()
        if row is None:
            raise NotFound(f"no dashboard {slug}")
        record = dict(zip([c.name for c in cursor.description], row))
    spec = DashboardSpec.model_validate(record["spec"]) if record["spec"] else None
    return spec, record


def cube_catalogs(store: SkillStore) -> dict[str, str]:
    """Which catalog each published cube reads, from the skills' access manifests."""
    found: dict[str, str] = {}
    for directory in store.root.iterdir() if store.root.is_dir() else []:
        if directory.is_dir() and not directory.name.startswith(("_", ".")):
            access = store.read_access(directory.name)
            found |= {cube: access["catalog"] for cube in access.get("cubes", {})}
    return found


def catalogs_of(spec: DashboardSpec, store: SkillStore) -> list[str]:
    """The catalogs a dashboard reads. Sharing and visibility depend on them, so a member whose cube
    isn't published is an error rather than ignored."""
    owners = cube_catalogs(store)
    members = [m for chart in spec.charts for m in chart.members()] + [f.member for f in spec.filters]
    cubes = {member.split(".")[0] for member in members}
    missing = sorted(cube for cube in cubes if cube not in owners)
    if missing:
        raise InvalidRequest(f"these aren't cubes in the semantic layer: {missing}")
    return sorted({owners[cube] for cube in cubes})


def published_model(store: SkillStore) -> DataModel:
    cubes = []
    for directory in store.root.iterdir() if store.root.is_dir() else []:
        if directory.is_dir() and not directory.name.startswith(("_", ".")):
            model = store.read_model(directory.name)
            cubes += model.cubes if model else []
    return DataModel(cubes=cubes)


def chart_filters(spec: DashboardSpec, chart: ChartSpec, chosen: dict[str, list[str]]) -> list[QueryFilter]:
    """The dashboard filters someone chose that reach this chart."""
    found = []
    for item in spec.filters:
        values = [str(value) for value in chosen.get(item.name) or []]
        if values and chart.key in item.applies_to:
            found.append(QueryFilter(member=item.member, operator="equals", values=values))
    return found


def run_chart(cube: CubeClient, identity: CubeIdentity, spec: DashboardSpec, key: str, *,
              chosen: dict[str, list[str]] | None = None, date_range: list[str] | None = None,
              model: DataModel | None = None) -> dict[str, Any]:
    chart = spec.chart(key)
    if chart is None:
        raise NotFound(f"no chart {key} on this dashboard")
    if date_range and (len(date_range) != 2 or date_range[0] > date_range[1]):
        raise InvalidRequest("a date range is [from, to]")
    query = chart.to_query(today(), extra_filters=chart_filters(spec, chart, chosen or {}),
                           date_range=date_range if spec.time_filter else None)
    result: CubeResult = cube.load(identity, query.to_cube())
    window = query.time_dimensions[0].date_range if query.time_dimensions else None
    return {"key": key, "rows": result.data, "row_count": len(result.data), "truncated": len(result.data) >= chart.limit,
            "window": window, "annotation": annotate(result.annotation, model),
            "certified": certified(model, chart.members()) if model is not None else False}


def filter_reach(cube: CubeClient, identity: CubeIdentity, spec: DashboardSpec) -> DashboardSpec:
    """Which charts each filter can reach: Cube has to find a join from the chart's cubes to the
    filter's. Compiling is enough to tell, so nothing is run."""
    def reaches(chart: ChartSpec, member: str) -> bool:
        query = chart.to_query(today(), extra_filters=[QueryFilter(member=member, operator="set")])
        try:
            cube.sql(identity, query.to_cube())
            return True
        except NovaError:
            return False

    spec = spec.model_copy(deep=True)
    with ThreadPoolExecutor(max_workers=REACH_WORKERS) as pool:
        for item in spec.filters:
            results = list(pool.map(lambda chart, member=item.member: reaches(chart, member), spec.charts))
            item.applies_to = [chart.key for chart, ok in zip(spec.charts, results) if ok]
    return spec


def filter_options(cube: CubeClient, identity: CubeIdentity, spec: DashboardSpec, name: str) -> list[str]:
    """The values someone can choose for a filter. Cube limits them to what this person may see, so
    someone limited to one tenant is only offered their own."""
    item = next((f for f in spec.filters if f.name == name), None)
    if item is None:
        raise NotFound(f"no filter {name} on this dashboard")
    rows = cube.load(identity, {"dimensions": [item.member], "order": [[item.member, "asc"]],
                                "limit": MAX_FILTER_OPTIONS}).data
    return [str(row[item.member]) for row in rows if row.get(item.member) is not None]
