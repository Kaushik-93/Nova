"""Every chart Nova can build, on real Superset and Trino: each one saves and returns data, and
each sheet draws in a real browser. Superset accepts almost any chart and only fails on rendering,
so saving proves little by itself. Charts are the same specs the Chart gallery project shows."""

import uuid

import pytest

from agent.config import get_settings
from agent.gallery import DATASETS, SHEETS, TILES
from agent.tools.renderer import render_dashboard
from agent.tools.superset_client import SupersetClient
from agent.tools.superset_payloads import ChartSlot, Viz

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def superset():
    settings = get_settings()
    with SupersetClient(settings.superset_url, settings.superset_admin_user, settings.superset_admin_password) as client:
        yield client


@pytest.fixture(scope="module")
def built(superset):
    """One dataset of each kind, and a clean-up of exactly what's made here."""
    key = f"gallery-test-{uuid.uuid4().hex[:8]}"
    database = superset.database_id("Nova Trino")
    datasets = {name: superset.ensure_dataset(key=f"{key}/{name}", name=f"{key}__{name}", sql=sql, database_id=database)
                for name, sql in DATASETS.items()}
    made = {"key": key, "datasets": datasets, "charts": {}, "dashboards": []}
    yield made
    for dashboard in made["dashboards"]:
        superset.delete_dashboard(dashboard)
    for chart in made["charts"].values():
        superset.delete_chart(chart)
    for dataset in datasets.values():
        superset.delete_dataset(dataset.id)


def chart_of(superset, built, tile) -> int:
    if tile.spec.key not in built["charts"]:
        chart = superset.ensure_chart(tile.spec, dataset_id=built["datasets"][tile.dataset].id, dashboard_key=built["key"])
        built["charts"][tile.spec.key] = chart.id
    return built["charts"][tile.spec.key]


def test_nothing_is_left_out():
    assert {tile.spec.viz for tile in TILES} == set(Viz)


@pytest.mark.parametrize("tile", TILES, ids=[tile.spec.key for tile in TILES])
def test_each_chart_saves_and_returns_data(superset, built, tile):
    data = superset.chart_data(chart_of(superset, built, tile), force=True)
    assert data.row_count > 0, f"{tile.spec.title} returned nothing"


@pytest.mark.parametrize("name", list(SHEETS))
def test_each_sheet_draws_in_a_real_browser(superset, built, name):
    settings = get_settings()
    rows = SHEETS[name]
    slots = [[ChartSlot(text=tile.text, width=tile.width) if tile.text
              else ChartSlot(chart_id=chart_of(superset, built, tile), title=tile.spec.title, width=tile.width) for tile in row] for row in rows]
    slug = f"{built['key']}-{name.lower().replace(' ', '-')}"
    dashboard = superset.ensure_dashboard(slug=slug, title=name, rows=slots)
    built["dashboards"].append(dashboard.id)

    rendered = render_dashboard(settings.superset_url, settings.superset_admin_user, settings.superset_admin_password, slug,
                                expected_charts=sum(1 for row in rows for tile in row if tile.spec), height=2600)

    assert rendered.errors == [], f"{name}: {rendered.errors}"
