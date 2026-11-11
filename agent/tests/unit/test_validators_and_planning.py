from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from agent.dashboard_agent import dashboard_key_for, load_prompt
from agent.tools.planning import DashboardPlan, PlannedChart


def test_every_prompt_knows_todays_date():
    today = datetime.now(timezone.utc).date().isoformat()
    for name in ("main", "modeler", "chart_designer", "verifier"):
        assert today in load_prompt(name)


def test_a_thread_keeps_its_dashboard():
    assert dashboard_key_for("a") == dashboard_key_for("a")
    assert dashboard_key_for("a") != dashboard_key_for("b")


def test_a_plan_needs_charts_and_sources():
    with pytest.raises(ValidationError):
        DashboardPlan(title="t", goal="g", sources=[], charts=[])


def test_a_planned_chart_names_its_measures():
    with pytest.raises(ValidationError):
        PlannedChart(key="k", title="t", viz="bar", measures=[])
    assert PlannedChart(key="k", title="t", viz="line", measures=["c.m"], grain="month").grain == "month"
