"""A project with one of every chart Nova can build, on the rental demo data: `make gallery`.

It's a catalogue for people choosing a chart, and the chart tests run these same specs, so what's
shown here is what's tested. It involves no model, because the charts are written out by hand.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

from psycopg_pool import ConnectionPool

from agent.config import get_settings
from agent.dashboard_agent import dashboard_key_for
from agent.db import apply_schema, create_pool
from agent.tools.superset_client import SupersetClient
from agent.tools.superset_payloads import (KPI_ROW_HEIGHT, ChartSlot, ChartSpec, Metric, SelectFilter, Stacking, TimeFilter,
                                           TimeGrain, Viz)

PROJECT_SLUG = "chart-gallery"
PROJECT_NAME = "Chart gallery"
PROJECT_DESCRIPTION = "One of every chart Nova can build, on the rental demo data."
CATALOG = "demo_rental"

AGREEMENTS = """
SELECT a.tenant_id, t.tenant_name, t.country_code, t.plan, v.vehicle_id, v.vehicle_class, v.make, a.status,
       a.booking_channel, pickup.city AS pickup_city, dropoff.city AS return_city, pickup.branch_type,
       CAST(a.start_ts AS timestamp(3)) AS start_ts,
       date_diff('day', a.start_ts, a.planned_end_ts) AS rental_days,
       a.odometer_in_km - a.odometer_out_km AS km_driven
FROM demo_rental.rental.rental_agreements a
JOIN demo_rental.rental.tenants t ON t.tenant_id = a.tenant_id
JOIN demo_rental.rental.vehicles v ON v.vehicle_id = a.vehicle_id
JOIN demo_rental.rental.branches pickup ON pickup.branch_id = a.pickup_branch_id
JOIN demo_rental.rental.branches dropoff ON dropoff.branch_id = a.return_branch_id
WHERE a.deleted_at IS NULL
"""
# A tree needs one top node, so every tenant's rows carry it and any tenant's view still has it.
ORGANISATION = """
SELECT t.tenant_id, t.tenant_name, 'platform' AS node_id, CAST(NULL AS varchar) AS parent_id, 'Rental platform' AS node_name
FROM demo_rental.rental.tenants t
UNION ALL
SELECT t.tenant_id, t.tenant_name, 'tenant-' || CAST(t.tenant_id AS varchar), 'platform', t.tenant_name
FROM demo_rental.rental.tenants t
UNION ALL
SELECT b.tenant_id, t.tenant_name, 'branch-' || CAST(b.branch_id AS varchar), 'tenant-' || CAST(b.tenant_id AS varchar), b.branch_name
FROM demo_rental.rental.branches b JOIN demo_rental.rental.tenants t ON t.tenant_id = b.tenant_id
"""
US_STATES = """
SELECT a.tenant_id, t.tenant_name, CAST(a.start_ts AS timestamp(3)) AS start_ts,
       CASE pickup.city WHEN 'Boston' THEN 'US-MA' WHEN 'Chicago' THEN 'US-IL' ELSE 'US-CO' END AS state_code
FROM demo_rental.rental.rental_agreements a
JOIN demo_rental.rental.tenants t ON t.tenant_id = a.tenant_id
JOIN demo_rental.rental.branches pickup ON pickup.branch_id = a.pickup_branch_id
WHERE a.deleted_at IS NULL AND pickup.city IN ('Boston', 'Chicago', 'Denver')
"""
ROUTES = """
SELECT a.tenant_id, t.tenant_name, CAST(a.start_ts AS timestamp(3)) AS start_ts, pickup.city AS pickup_city, dropoff.city AS return_city
FROM demo_rental.rental.rental_agreements a
JOIN demo_rental.rental.tenants t ON t.tenant_id = a.tenant_id
JOIN demo_rental.rental.branches pickup ON pickup.branch_id = a.pickup_branch_id
JOIN demo_rental.rental.branches dropoff ON dropoff.branch_id = a.return_branch_id
WHERE a.deleted_at IS NULL AND pickup.city <> dropoff.city
"""
DATASETS = {"agreements": AGREEMENTS, "organisation": ORGANISATION, "us_states": US_STATES, "routes": ROUTES}

RENTALS = Metric(label="Rentals", sql="COUNT(*)")
RENTAL_DAYS = Metric(label="Rental days", sql="SUM(rental_days)")
VEHICLES = Metric(label="Vehicles", sql="COUNT(DISTINCT vehicle_id)")
AVERAGE_DAYS = Metric(label="Average days", sql="AVG(rental_days)")
AVERAGE_KM = Metric(label="Average km", sql="AVG(km_driven)")
COMPLETED = Metric(label="Completed", sql="AVG(CASE WHEN status = 'completed' THEN 1.0 ELSE 0 END)")
COMPLETED_RENTALS = Metric(label="Completed rentals", sql="SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END)")
BOOKED_ONLINE = Metric(label="Booked on the web", sql="AVG(CASE WHEN booking_channel = 'web' THEN 1.0 ELSE 0 END)")
BOOKED_ON_MOBILE = Metric(label="Booked on mobile", sql="AVG(CASE WHEN booking_channel = 'mobile_app' THEN 1.0 ELSE 0 END)")
FROM_AIRPORTS = Metric(label="Picked up at airports", sql="AVG(CASE WHEN branch_type = 'airport' THEN 1.0 ELSE 0 END)")
LAST_12_MONTHS = dict(time_column="start_ts", time_grain=TimeGrain.MONTH, periods=12)


@dataclass(frozen=True)
class Tile:
    """A chart on a sheet, or a block of text, which has no spec."""

    dataset: str
    spec: ChartSpec | None
    width: int = 6
    text: str | None = None


def tile(viz: Viz, title: str, *, dataset: str = "agreements", width: int = 6, **fields) -> Tile:
    key = title.lower().replace(" ", "-").replace(",", "")
    return Tile(dataset, ChartSpec(key=key, title=title, viz=viz, **fields), width)


def note(text: str, *, width: int = 4) -> Tile:
    return Tile("", None, width, text)


SHEETS: dict[str, list[list[Tile]]] = {
    "Numbers": [
        [tile(Viz.BIG_NUMBER, "KPI", metrics=[RENTALS], y_axis_format=",d", width=4, subtitle="Rentals in the last 12 months",
              **LAST_12_MONTHS),
         tile(Viz.BIG_NUMBER_TREND, "KPI with a trend", metrics=[RENTALS], y_axis_format=",d", width=4, **LAST_12_MONTHS),
         note("### Text\n\nA block of **formatted text** for headings and notes. It's static, so a live figure "
              "belongs in a KPI, which can carry a caption.")],
        [tile(Viz.GAUGE, "Gauge", metrics=[COMPLETED], y_axis_format=".0%", gauge_max=1),
         tile(Viz.BULLET, "Bullet chart", metrics=[COMPLETED], y_axis_format=".0%", bullet_target=0.95,
              bullet_ranges=[0.7, 0.85, 1])],
    ],
    "Trends": [
        [tile(Viz.LINE, "Line chart", metrics=[RENTALS], y_axis_format=",d", **LAST_12_MONTHS),
         tile(Viz.SMOOTH_LINE, "Smooth line chart", metrics=[RENTALS], y_axis_format=",d", **LAST_12_MONTHS)],
        [tile(Viz.STEP_LINE, "Step line chart", metrics=[RENTALS], y_axis_format=",d", **LAST_12_MONTHS),
         tile(Viz.AREA, "Stacked area chart", metrics=[RENTALS], dimensions=["vehicle_class"], stacking=Stacking.STACKED,
              y_axis_format=",d", **LAST_12_MONTHS)],
        [tile(Viz.COMBO, "Combo chart", metrics=[RENTALS], line_metrics=[COMPLETED_RENTALS], y_axis_format=",d", **LAST_12_MONTHS),
         tile(Viz.DOTS, "Dot chart over time", metrics=[RENTALS], y_axis_format=",d", **LAST_12_MONTHS)],
        [tile(Viz.CALENDAR, "Calendar heatmap", metrics=[RENTALS], y_axis_format=",d", width=12, time_column="start_ts",
              time_grain=TimeGrain.DAY, periods=365)],
    ],
    "Comparisons": [
        [tile(Viz.BAR, "Bar chart", metrics=[RENTALS], dimensions=["vehicle_class"], y_axis_format=",d", **LAST_12_MONTHS),
         tile(Viz.BAR, "Horizontal bar chart", metrics=[RENTALS], dimensions=["make"], horizontal=True, row_limit=10,
              y_axis_format=",d", **LAST_12_MONTHS)],
        [tile(Viz.BAR, "Stacked bar chart", metrics=[RENTALS], dimensions=["vehicle_class", "booking_channel"],
              stacking=Stacking.STACKED, y_axis_format=",d", **LAST_12_MONTHS),
         tile(Viz.BAR, "100% stacked bar chart", metrics=[RENTALS], dimensions=["vehicle_class", "booking_channel"],
              stacking=Stacking.PERCENT, **LAST_12_MONTHS)],
        [tile(Viz.BAR, "Bars per month, split by class", metrics=[RENTALS], dimensions=["vehicle_class"], time_axis=True,
              stacking=Stacking.STACKED, y_axis_format=",d", width=12, **LAST_12_MONTHS)],
        [tile(Viz.WATERFALL, "Waterfall chart", metrics=[RENTALS], dimensions=["booking_channel"], y_axis_format=",d",
              width=7, **LAST_12_MONTHS),
         tile(Viz.RADAR, "Radar chart", metrics=[COMPLETED, BOOKED_ONLINE, BOOKED_ON_MOBILE, FROM_AIRPORTS], dimensions=["vehicle_class"],
              width=5, row_limit=3, y_axis_format=".0%", **LAST_12_MONTHS)],
    ],
    "Parts of a whole": [
        [tile(Viz.PIE, "Donut chart", metrics=[RENTALS], dimensions=["booking_channel"], y_axis_format=",d", **LAST_12_MONTHS),
         tile(Viz.PIE, "Pie chart", metrics=[RENTALS], dimensions=["vehicle_class"], donut=False, y_axis_format=",d",
              **LAST_12_MONTHS)],
        [tile(Viz.TREEMAP, "Treemap", metrics=[RENTALS], dimensions=["vehicle_class", "make"], y_axis_format=",d", **LAST_12_MONTHS),
         tile(Viz.SUNBURST, "Sunburst", metrics=[RENTALS], dimensions=["booking_channel", "vehicle_class"], y_axis_format=",d",
              **LAST_12_MONTHS)],
        [tile(Viz.FUNNEL, "Funnel chart", metrics=[RENTALS], dimensions=["status"], y_axis_format=",d", **LAST_12_MONTHS),
         tile(Viz.WORD_CLOUD, "Word cloud", metrics=[RENTALS], dimensions=["make"], row_limit=30, **LAST_12_MONTHS)],
    ],
    "Distributions": [
        [tile(Viz.HISTOGRAM, "Histogram", value_column="rental_days", bins=20, y_axis_format=",d", **LAST_12_MONTHS),
         tile(Viz.BOX_PLOT, "Box plot", metrics=[AVERAGE_DAYS], dimensions=["vehicle_class"], distribute_across=["pickup_city"],
              y_axis_format=".1f", **LAST_12_MONTHS)],
        [tile(Viz.SCATTER, "Scatter plot", metrics=[RENTALS, AVERAGE_DAYS, VEHICLES], dimensions=["pickup_city", "branch_type"],
              y_axis_format=".1f", **LAST_12_MONTHS),
         tile(Viz.HEATMAP, "Heatmap", metrics=[RENTALS], dimensions=["vehicle_class"], y_axis_format=",d", **LAST_12_MONTHS)],
    ],
    "Flows and structures": [
        [tile(Viz.SANKEY, "Sankey chart", metrics=[RENTALS], dimensions=["booking_channel", "vehicle_class"], width=12,
              **LAST_12_MONTHS)],
        [tile(Viz.NETWORK, "Network chart", dataset="routes", metrics=[RENTALS], dimensions=["pickup_city", "return_city"], row_limit=80,
              **LAST_12_MONTHS),
         tile(Viz.TREE, "Org chart", dataset="organisation", metrics=[RENTALS], dimensions=["node_id", "parent_id", "node_name"],
              tree_root="platform", row_limit=200)],
    ],
    "Maps": [
        [tile(Viz.WORLD_MAP, "World map", metrics=[RENTALS], dimensions=["country_code"], y_axis_format=",d", **LAST_12_MONTHS),
         tile(Viz.COUNTRY_MAP, "Country map", dataset="us_states", metrics=[RENTALS], dimensions=["state_code"], map_country="usa",
              y_axis_format=",d", **LAST_12_MONTHS)],
    ],
    "Tables": [
        [tile(Viz.TABLE, "Table", metrics=[RENTALS, RENTAL_DAYS, AVERAGE_KM], dimensions=["vehicle_class", "booking_channel"],
              y_axis_format=",.0f", width=12, **LAST_12_MONTHS)],
        [tile(Viz.PIVOT, "Pivot table", metrics=[RENTALS], dimensions=["vehicle_class"], pivot_columns=["booking_channel"],
              y_axis_format=",d", width=12, **LAST_12_MONTHS)],
    ],
}
TILES = [item for rows in SHEETS.values() for row in rows for item in row if item.spec]


def build_sheet(superset: SupersetClient, name: str, rows: list[list[Tile]], *, key: str, ui_origin: str) -> tuple[int, str]:
    """Create one sheet's datasets, charts and dashboard. Run again, it updates them."""
    database_id = superset.database_id("Nova Trino")
    wanted = {item.dataset for row in rows for item in row if item.spec}
    datasets = {dataset: superset.ensure_dataset(key=f"{key}/{dataset}", name=f"{key}__{dataset}", sql=DATASETS[dataset],
                                                 database_id=database_id) for dataset in sorted(wanted)}
    slots = []
    for row in rows:
        height = KPI_ROW_HEIGHT if all(item.spec and item.spec.viz is Viz.BIG_NUMBER for item in row) else 50
        slots.append([])
        for item in row:
            if item.text:
                slots[-1].append(ChartSlot(text=item.text, width=item.width, height=height))
            else:
                chart = superset.ensure_chart(item.spec, dataset_id=datasets[item.dataset].id, dashboard_key=key)
                slots[-1].append(ChartSlot(chart_id=chart.id, title=item.spec.title, width=item.width, height=height))
    filters = [TimeFilter(), SelectFilter(name="Tenant", dataset_id=next(iter(datasets.values())).id, column="tenant_name")]
    dashboard = superset.ensure_dashboard(slug=key, title=name, rows=slots, filters=filters)
    return dashboard.id, superset.enable_embedding(dashboard.id, [ui_origin])



def build_gallery(pool: ConnectionPool, superset: SupersetClient, *, owner: str, ui_origin: str) -> str:
    with pool.connection() as conn:
        project_id = conn.execute(
            """INSERT INTO projects (slug, name, description, created_by, shared) VALUES (%s, %s, %s, %s, true)
               ON CONFLICT (slug) DO UPDATE SET updated_at = now() RETURNING id""",
            (PROJECT_SLUG, PROJECT_NAME, PROJECT_DESCRIPTION, owner)).fetchone()[0]
    for position, (name, rows) in enumerate(SHEETS.items()):
        thread = f"chart-gallery-{name.lower().replace(' ', '-')}"
        key = dashboard_key_for(thread)
        dashboard_id, embed_uuid = build_sheet(superset, name, rows, key=key, ui_origin=ui_origin)
        with pool.connection() as conn:
            conn.execute(
                """INSERT INTO dashboards (slug, thread_id, superset_id, embed_uuid, title, summary, created_by, catalogs,
                                          project_id, sheet_name, position)
                   VALUES (%s, %s, %s, %s, %s, '', %s, %s, %s, %s, %s)
                   ON CONFLICT (slug) DO UPDATE SET embed_uuid = EXCLUDED.embed_uuid, updated_at = now()""",
                (key, thread, dashboard_id, embed_uuid, name, owner, [CATALOG], project_id, name, position))
    return PROJECT_SLUG


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", help="Who owns the project. Defaults to the first administrator")
    arguments = parser.parse_args()
    settings = get_settings()
    pool = create_pool(settings.database_url)
    apply_schema(pool)
    with SupersetClient(settings.superset_url, settings.superset_admin_user, settings.superset_admin_password) as superset:
        slug = build_gallery(pool, superset, owner=arguments.owner or settings.nova_admin_email, ui_origin=settings.nova_ui_origin)
    pool.close()
    print(f"Built {len(TILES)} charts on {len(SHEETS)} sheets. Open {settings.nova_ui_origin}/projects/{slug}")


if __name__ == "__main__":
    main()
