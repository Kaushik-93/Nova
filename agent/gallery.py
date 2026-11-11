"""A project with one of every chart Nova can draw, on the rental demo data: `make gallery`.

It's a catalogue for people choosing a chart, and the chart tests use these same specs, so what's
shown here is what's tested. It involves no model: its small semantic model and its charts are
written out by hand. The model is published beside the skills, under `skills/chart-gallery/`, with
the rental source's tenant limits, so a shared gallery shows each person their own tenants.
"""

from __future__ import annotations

import argparse
import uuid

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from agent.config import get_settings
from agent.dashboard_agent import dashboard_key_for
from agent.db import apply_schema, create_pool
from agent.harness.audit import AuditLog, RunContext
from agent.harness.budgets import BudgetLimits, RunBudget
from agent.harness.executor import SqlExecutor
from agent.semantic.access import Tenancy, tenancy_of
from agent.semantic.charts import ChartSpec, DashboardFilter, DashboardSpec, Slot, Stacking, Viz
from agent.semantic.model import Cube, DataModel, Dimension, Filter, Measure, MemberMeta
from agent.tools.profiler import ColumnProfile, Profiler, SourceProfile, TableProfile
from agent.tools.skill_store import SkillStore
from agent.tools.sources import get_source_by_slug

PROJECT_SLUG = "chart-gallery"
PROJECT_NAME = "Chart gallery"
PROJECT_DESCRIPTION = "One of every chart Nova can draw, on the rental demo data."
MODEL_SLUG = "chart-gallery"
CATALOG = "demo_rental"
SOURCE = "demo-rental"

A = "demo_rental__gallery_agreements"
ORG = "demo_rental__gallery_organisation"
ROUTES = "demo_rental__gallery_routes"

AGREEMENTS_SQL = """SELECT a.agreement_id, a.tenant_id, t.tenant_name, t.country_code, t.plan, v.vehicle_id, v.vehicle_class, v.make,
       a.status, a.booking_channel, pickup.city AS pickup_city, dropoff.city AS return_city, pickup.branch_type,
       CAST(a.start_ts AS timestamp(3)) AS start_ts, date_diff('day', a.start_ts, a.planned_end_ts) AS rental_days,
       a.odometer_in_km - a.odometer_out_km AS km_driven
FROM demo_rental.rental.rental_agreements a
JOIN demo_rental.rental.tenants t ON t.tenant_id = a.tenant_id
JOIN demo_rental.rental.vehicles v ON v.vehicle_id = a.vehicle_id
JOIN demo_rental.rental.branches pickup ON pickup.branch_id = a.pickup_branch_id
JOIN demo_rental.rental.branches dropoff ON dropoff.branch_id = a.return_branch_id
WHERE a.deleted_at IS NULL"""
# A tree needs one top node, so every tenant's rows carry it and any tenant's view still has it.
ORGANISATION_SQL = """SELECT t.tenant_id, t.tenant_name, 'platform' AS node_id, CAST(NULL AS varchar) AS parent_id,
       'Rental platform' AS node_name, 'platform-' || CAST(t.tenant_id AS varchar) AS row_id
FROM demo_rental.rental.tenants t
UNION ALL
SELECT t.tenant_id, t.tenant_name, 'tenant-' || CAST(t.tenant_id AS varchar), 'platform', t.tenant_name,
       'tenant-' || CAST(t.tenant_id AS varchar)
FROM demo_rental.rental.tenants t
UNION ALL
SELECT b.tenant_id, t.tenant_name, 'branch-' || CAST(b.branch_id AS varchar), 'tenant-' || CAST(b.tenant_id AS varchar),
       b.branch_name, 'branch-' || CAST(b.branch_id AS varchar)
FROM demo_rental.rental.branches b JOIN demo_rental.rental.tenants t ON t.tenant_id = b.tenant_id"""
ROUTES_SQL = """SELECT a.agreement_id, a.tenant_id, t.tenant_name, CAST(a.start_ts AS timestamp(3)) AS start_ts,
       pickup.city AS pickup_city, dropoff.city AS return_city
FROM demo_rental.rental.rental_agreements a
JOIN demo_rental.rental.tenants t ON t.tenant_id = a.tenant_id
JOIN demo_rental.rental.branches pickup ON pickup.branch_id = a.pickup_branch_id
JOIN demo_rental.rental.branches dropoff ON dropoff.branch_id = a.return_branch_id
WHERE a.deleted_at IS NULL AND pickup.city <> dropoff.city"""


def dim(name: str, kind: str = "string", *, title: str | None = None, key: bool = False) -> Dimension:
    return Dimension(name=name, title=title or name.replace("_", " ").capitalize(), sql=f"{{CUBE}}.{name}", type=kind,
                     primary_key=key)


def rate(name: str, title: str, condition: str) -> Measure:
    return Measure(name=name, title=title, type="avg", sql=f"CASE WHEN {condition} THEN 1.0 ELSE 0 END", format="percent")


MODEL = DataModel(cubes=[
    Cube(name=A, title="Rental agreements (gallery)", sql=AGREEMENTS_SQL,
         dimensions=[dim("agreement_id", "number", key=True), dim("tenant_name", title="Tenant"), dim("country_code", title="Country"),
                     dim("plan"), dim("vehicle_class"), dim("make"), dim("status"), dim("booking_channel"),
                     dim("pickup_city"), dim("return_city"), dim("branch_type"), dim("start_ts", "time", title="Started"),
                     dim("rental_days", "number")],
         measures=[Measure(name="rentals", title="Rentals", type="count"),
                   Measure(name="rental_days_total", title="Rental days", type="sum", sql="{CUBE}.rental_days"),
                   Measure(name="vehicles", title="Vehicles", type="count_distinct", sql="{CUBE}.vehicle_id"),
                   Measure(name="average_days", title="Average days", type="avg", sql="{CUBE}.rental_days"),
                   Measure(name="average_km", title="Average km", type="avg", sql="{CUBE}.km_driven", meta=MemberMeta(unit="km")),
                   Measure(name="completed_rentals", title="Completed rentals", type="count",
                           filters=[Filter(sql="{CUBE}.status = 'completed'")]),
                   rate("completed", "Completed", "{CUBE}.status = 'completed'"),
                   rate("booked_online", "Booked on the web", "{CUBE}.booking_channel = 'web'"),
                   rate("booked_on_mobile", "Booked on mobile", "{CUBE}.booking_channel = 'mobile_app'"),
                   rate("from_airports", "Picked up at airports", "{CUBE}.branch_type = 'airport'")]),
    Cube(name=ORG, title="Organisation (gallery)", sql=ORGANISATION_SQL,
         dimensions=[dim("row_id", key=True), dim("node_id"), dim("parent_id"), dim("node_name"), dim("tenant_name", title="Tenant")],
         measures=[Measure(name="nodes", title="Nodes", type="count")]),
    Cube(name=ROUTES, title="One-way routes (gallery)", sql=ROUTES_SQL,
         dimensions=[dim("agreement_id", "number", key=True), dim("pickup_city"), dim("return_city"),
                     dim("tenant_name", title="Tenant"), dim("start_ts", "time", title="Started")],
         measures=[Measure(name="rentals", title="Rentals", type="count")]),
])

RENTALS = f"{A}.rentals"
WHEN = f"{A}.start_ts"
LAST_12_MONTHS = dict(time_dimension=WHEN, grain="month", periods=12)


def chart(viz: Viz, title: str, **fields) -> ChartSpec:
    words = "".join(c if c.isalnum() else " " for c in title.lower()).split()
    key = "_".join(words if words[0][0].isalpha() else ["chart", *words])
    fields.setdefault("measures", [RENTALS])
    return ChartSpec(key=key, title=title, viz=viz, **fields)


def member(name: str, cube: str = A) -> str:
    return f"{cube}.{name}"


SHEETS: dict[str, list[list[tuple[ChartSpec, int]]]] = {
    "Numbers": [
        [(chart(Viz.BIG_NUMBER, "KPI", subtitle="Rentals in the last 12 months", **LAST_12_MONTHS), 0),
         (chart(Viz.BIG_NUMBER_TREND, "KPI with a trend", **LAST_12_MONTHS), 0),
         (chart(Viz.GAUGE, "Gauge", measures=[member("completed")], gauge_max=1, **LAST_12_MONTHS), 0),
         (chart(Viz.BULLET, "Bullet chart", measures=[member("completed")], target=0.95, bullet_ranges=[0.7, 0.85, 1],
                **LAST_12_MONTHS), 0)],
    ],
    "Trends": [
        [(chart(Viz.LINE, "Line chart", **LAST_12_MONTHS), 6), (chart(Viz.SMOOTH_LINE, "Smooth line chart", **LAST_12_MONTHS), 6)],
        [(chart(Viz.STEP_LINE, "Step line chart", **LAST_12_MONTHS), 6),
         (chart(Viz.AREA, "Stacked area chart", dimensions=[member("vehicle_class")], stacking=Stacking.STACKED,
                time_axis=True, **LAST_12_MONTHS), 6)],
        [(chart(Viz.COMBO, "Combo chart", line_measures=[member("completed_rentals")], **LAST_12_MONTHS), 6),
         (chart(Viz.DOTS, "Dot chart over time", **LAST_12_MONTHS), 6)],
        [(chart(Viz.CALENDAR, "Calendar heatmap", time_dimension=WHEN, grain="day", periods=365), 12)],
    ],
    "Comparisons": [
        [(chart(Viz.BAR, "Bar chart", dimensions=[member("vehicle_class")], **LAST_12_MONTHS), 6),
         (chart(Viz.BAR, "Horizontal bar chart", dimensions=[member("make")], horizontal=True, limit=10, **LAST_12_MONTHS), 6)],
        [(chart(Viz.BAR, "Stacked bar chart", dimensions=[member("vehicle_class"), member("booking_channel")],
                stacking=Stacking.STACKED, **LAST_12_MONTHS), 6),
         (chart(Viz.BAR, "100% stacked bar chart", dimensions=[member("vehicle_class"), member("booking_channel")],
                stacking=Stacking.PERCENT, **LAST_12_MONTHS), 6)],
        [(chart(Viz.BAR, "Bars per month, split by class", dimensions=[member("vehicle_class")], time_axis=True,
                stacking=Stacking.STACKED, **LAST_12_MONTHS), 12)],
        [(chart(Viz.WATERFALL, "Waterfall chart", dimensions=[member("booking_channel")], **LAST_12_MONTHS), 7),
         (chart(Viz.RADAR, "Radar chart", measures=[member("completed"), member("booked_online"), member("booked_on_mobile"),
                                                    member("from_airports")], dimensions=[member("vehicle_class")], limit=3,
                **LAST_12_MONTHS), 5)],
    ],
    "Parts of a whole": [
        [(chart(Viz.PIE, "Donut chart", dimensions=[member("booking_channel")], **LAST_12_MONTHS), 6),
         (chart(Viz.PIE, "Pie chart", dimensions=[member("vehicle_class")], donut=False, **LAST_12_MONTHS), 6)],
        [(chart(Viz.TREEMAP, "Treemap", dimensions=[member("vehicle_class"), member("make")], **LAST_12_MONTHS), 6),
         (chart(Viz.SUNBURST, "Sunburst", dimensions=[member("booking_channel"), member("vehicle_class")], **LAST_12_MONTHS), 6)],
        [(chart(Viz.FUNNEL, "Funnel chart", dimensions=[member("status")], **LAST_12_MONTHS), 6),
         (chart(Viz.WORD_CLOUD, "Word cloud", dimensions=[member("make")], limit=30, **LAST_12_MONTHS), 6)],
    ],
    "Distributions": [
        [(chart(Viz.HISTOGRAM, "Histogram", value_dimension=member("rental_days"), bins=20, **LAST_12_MONTHS), 6),
         (chart(Viz.BOX_PLOT, "Box plot", measures=[member("average_days")], dimensions=[member("vehicle_class")],
                distribute_across=[member("pickup_city")], **LAST_12_MONTHS), 6)],
        [(chart(Viz.SCATTER, "Scatter plot", measures=[RENTALS, member("average_days"), member("vehicles")],
                dimensions=[member("pickup_city"), member("branch_type")], **LAST_12_MONTHS), 6),
         (chart(Viz.HEATMAP, "Heatmap", dimensions=[member("vehicle_class")], **LAST_12_MONTHS), 6)],
    ],
    "Flows and structures": [
        [(chart(Viz.SANKEY, "Sankey chart", dimensions=[member("booking_channel"), member("vehicle_class")], **LAST_12_MONTHS), 12)],
        [(chart(Viz.NETWORK, "Network chart", measures=[member("rentals", ROUTES)],
                dimensions=[member("pickup_city", ROUTES), member("return_city", ROUTES)], limit=80,
                time_dimension=member("start_ts", ROUTES), grain="month", periods=12), 6),
         (chart(Viz.TREE, "Org chart", measures=[member("nodes", ORG)],
                dimensions=[member("node_id", ORG), member("parent_id", ORG), member("node_name", ORG)],
                tree_root="platform", limit=200), 6)],
    ],
    "Maps": [
        [(chart(Viz.WORLD_MAP, "World map", dimensions=[member("country_code")], **LAST_12_MONTHS), 12)],
    ],
    "Tables": [
        [(chart(Viz.TABLE, "Table", measures=[RENTALS, member("rental_days_total"), member("average_km")],
                dimensions=[member("vehicle_class"), member("booking_channel")], **LAST_12_MONTHS), 12)],
        [(chart(Viz.PIVOT, "Pivot table", dimensions=[member("vehicle_class")], pivot_columns=[member("booking_channel")],
                **LAST_12_MONTHS), 12)],
    ],
}
CHARTS = [spec for rows in SHEETS.values() for row in rows for spec, _ in row]


def sheet_spec(name: str) -> DashboardSpec:
    rows = SHEETS[name]
    charts = [spec for row in rows for spec, _ in row]
    tenant = DashboardFilter(name="Tenant", member=member("tenant_name"),
                             applies_to=[c.key for c in charts if all(m.startswith(A) for m in c.members())])
    return DashboardSpec(title=name, charts=charts, rows=[[Slot(key=spec.key, width=width) for spec, width in row] for row in rows],
                         filters=[tenant] if tenant.applies_to else [], time_filter=True)


def live_profile(executor: SqlExecutor) -> SourceProfile:
    """The rental tables as they are now: enough to tell which carry the tenant column."""
    context = RunContext(run_id=f"gallery-{uuid.uuid4().hex[:8]}", user_id="system")
    shape = Profiler(executor, context=context, budget=RunBudget(BudgetLimits())).read_shape(CATALOG)
    return SourceProfile(source_slug=MODEL_SLUG, catalog=CATALOG, dialect="postgres", fingerprint="gallery",
                         profiled_at="2026-01-01T00:00:00Z", tables=[
                             TableProfile(schema_name=name.split(".")[0], name=name.split(".")[1],
                                          columns=[ColumnProfile(name=c, type=t, nullable=True) for c, t in cols.items()])
                             for name, cols in shape.items()])


def publish_model(store: SkillStore, executor: SqlExecutor, tenancy: Tenancy) -> None:
    store.path(MODEL_SLUG).mkdir(parents=True, exist_ok=True)
    store.publish_model(MODEL_SLUG, MODEL, profile=live_profile(executor), tenancy=tenancy)


def build_gallery(pool: ConnectionPool, store: SkillStore, executor: SqlExecutor, *, owner: str) -> str:
    source = get_source_by_slug(pool, SOURCE)
    publish_model(store, executor, tenancy_of(source) if source else Tenancy())
    with pool.connection() as conn:
        project_id = conn.execute(
            """INSERT INTO projects (slug, name, description, created_by, shared) VALUES (%s, %s, %s, %s, true)
               ON CONFLICT (slug) DO UPDATE SET updated_at = now() RETURNING id""",
            (PROJECT_SLUG, PROJECT_NAME, PROJECT_DESCRIPTION, owner)).fetchone()[0]
        for position, name in enumerate(SHEETS):
            thread = f"chart-gallery-{name.lower().replace(' ', '-')}"
            conn.execute(
                """INSERT INTO dashboards (slug, thread_id, title, summary, created_by, catalogs, project_id, sheet_name,
                                          position, spec)
                   VALUES (%s, %s, %s, '', %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (slug) DO UPDATE SET spec = EXCLUDED.spec, title = EXCLUDED.title, updated_at = now()""",
                (dashboard_key_for(thread), thread, name, owner, [CATALOG], project_id, name, position,
                 Jsonb(sheet_spec(name).model_dump(mode="json"))))
    return PROJECT_SLUG


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", help="Who owns the project. Defaults to the first administrator")
    arguments = parser.parse_args()
    settings = get_settings()
    pool = create_pool(settings.database_url)
    apply_schema(pool)
    store = SkillStore(settings.nova_skills_dir, pool)
    slug = build_gallery(pool, store, SqlExecutor(settings.trino_url, AuditLog(pool)),
                         owner=arguments.owner or settings.nova_admin_email)
    pool.close()
    print(f"Built {len(CHARTS)} charts on {len(SHEETS)} sheets. Open {settings.nova_ui_origin}/projects/{slug}")


if __name__ == "__main__":
    main()
