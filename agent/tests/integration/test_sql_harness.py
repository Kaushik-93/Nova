"""Phase 3, against real Trino, the demo sources and the application database."""

import uuid

import httpx
import pytest

from agent.config import get_settings
from agent.db import apply_schema, create_pool
from agent.harness.audit import AuditKind, AuditLog, AuditStatus, RunContext
from agent.harness.budgets import BudgetLimits, RunBudget
from agent.harness.errors import BudgetExceeded, QueryFailure, QueryTimedOut, QueryTooExpensive, SqlRejected
from agent.harness.executor import CostLimits, SqlExecutor
from agent.harness.guardrails import SqlPolicy
from agent.harness.untrusted import wrap_untrusted

pytestmark = pytest.mark.integration

ORDERS = "demo_shop.shop.orders"
ITEMS = "demo_shop.shop.order_items"
POLICY = SqlPolicy(allowed_catalogs=frozenset({"demo_shop", "demo_marketing"}), max_rows=50)


@pytest.fixture(scope="module")
def pool():
    pool = create_pool(get_settings().database_url)
    apply_schema(pool)
    yield pool
    pool.close()


@pytest.fixture(scope="module")
def audit(pool):
    return AuditLog(pool)


@pytest.fixture(scope="module")
def executor(audit):
    return SqlExecutor(get_settings().trino_url, audit)


@pytest.fixture
def context():
    return RunContext(run_id=f"test-{uuid.uuid4().hex[:10]}", user_id="tester")


@pytest.fixture
def budget():
    return RunBudget(BudgetLimits())


def test_the_schema_can_be_applied_repeatedly(pool):
    apply_schema(pool)
    apply_schema(pool)


def test_a_query_runs_and_is_audited(executor, audit, context, budget):
    result = executor.run(f"SELECT status, COUNT(*) AS n FROM {ORDERS} GROUP BY 1 ORDER BY 2 DESC",
                          policy=POLICY, context=context, budget=budget)

    assert result.columns == ["status", "n"]
    assert result.rows[0][0] == "delivered"
    assert result.truncated is False
    assert budget.sql_queries == 1

    entry = audit.for_run(context.run_id)[0]
    assert entry["kind"] == "sql"
    assert entry["status"] == "ok"
    assert entry["user_id"] == "tester"
    assert entry["target"] == ORDERS
    assert entry["rows"] == result.row_count
    assert entry["statement"] == result.sql
    assert entry["occurred_at"] is not None


def test_results_are_limited_and_flagged(executor, context, budget):
    result = executor.run(f"SELECT order_id FROM {ORDERS}", policy=POLICY, context=context, budget=budget)
    assert result.row_count == 50
    assert result.truncated is True


def test_a_cross_source_query_runs(executor, context, budget):
    result = executor.run(f"""
        SELECT c.channel_code, COUNT(*) AS orders
        FROM {ORDERS} o
        JOIN demo_marketing.marketing.campaigns c ON UPPER(REPLACE(o.utm_campaign, '-', '_')) = c.campaign_code
        GROUP BY 1
    """, policy=POLICY, context=context, budget=budget)
    assert result.row_count >= 3


def test_refused_sql_never_reaches_trino_and_is_audited(executor, audit, context, budget):
    with pytest.raises(SqlRejected):
        executor.run("SELECT * FROM TABLE(demo_shop.system.query(query => 'select version()'))",
                     policy=POLICY, context=context, budget=budget)

    entry = audit.for_run(context.run_id)[0]
    assert entry["status"] == "rejected"
    assert "system.query" in entry["statement"]
    assert entry["detail"]["reason"]


def test_a_catalog_outside_the_policy_is_refused(executor, context, budget):
    only_shop = SqlPolicy(allowed_catalogs=frozenset({"demo_shop"}))
    with pytest.raises(SqlRejected, match="not a connected source"):
        executor.run("SELECT * FROM demo_marketing.marketing.campaigns", policy=only_shop, context=context,
                     budget=budget)


@pytest.mark.parametrize("sql", [
    f"SELECT COUNT(*) FROM {ITEMS} a CROSS JOIN {ITEMS} b",
    f"SELECT COUNT(*) FROM {ITEMS} a CROSS JOIN demo_marketing.marketing.ad_spend_daily b CROSS JOIN {ORDERS} c",
], ids=["cross join", "cross source cross join"])
def test_runaway_joins_are_refused_by_the_cost_check(audit, sql):
    executor = SqlExecutor(get_settings().trino_url, audit, cost_limits=CostLimits(max_estimated_rows=1_000_000))
    context = RunContext(run_id=f"test-{uuid.uuid4().hex[:10]}", user_id="tester")

    with pytest.raises(QueryTooExpensive, match="check the joins"):
        executor.run(sql, policy=POLICY, context=context, budget=RunBudget(BudgetLimits()))

    entry = audit.for_run(context.run_id)[0]
    assert entry["status"] == "rejected"
    assert entry["detail"]["estimate"]["rows"] > 1_000_000


def test_ordinary_queries_pass_the_cost_check(audit, context, budget):
    executor = SqlExecutor(get_settings().trino_url, audit, cost_limits=CostLimits(max_estimated_rows=1_000_000))
    result = executor.run(f"SELECT COUNT(*) FROM {ORDERS} o JOIN {ITEMS} i ON i.order_id = o.order_id",
                          policy=POLICY, context=context, budget=budget)
    assert result.rows[0][0] > 100_000


def test_a_slow_query_is_killed_at_its_timeout(audit, context, budget):
    executor = SqlExecutor(get_settings().trino_url, audit, timeout_seconds=2,
                           cost_limits=CostLimits(max_estimated_rows=1e18, max_estimated_bytes=1e20))
    # Trino answers a bare COUNT(*) over a cross join almost instantly, so this one
    # has to do real work per row.
    with pytest.raises(QueryTimedOut):
        executor.run(f"SELECT SUM(a.quantity * b.quantity) FROM {ITEMS} a CROSS JOIN {ITEMS} b", policy=POLICY,
                     context=context, budget=budget)
    assert audit.for_run(context.run_id)[0]["status"] == "failed"


def test_a_query_that_fails_in_trino_is_reported_and_audited(executor, audit, context, budget):
    with pytest.raises(QueryFailure, match="COLUMN_NOT_FOUND"):
        executor.run(f"SELECT no_such_column FROM {ORDERS}", policy=POLICY, context=context, budget=budget)
    assert audit.for_run(context.run_id)[0]["status"] == "failed"


def test_the_budget_stops_queries_before_anything_runs(executor, audit, context):
    budget = RunBudget(BudgetLimits(max_sql_queries=1))
    executor.run("SELECT 1", policy=POLICY, context=context, budget=budget)
    with pytest.raises(BudgetExceeded):
        executor.run("SELECT 2", policy=POLICY, context=context, budget=budget)
    assert len(audit.for_run(context.run_id)) == 1


def test_injected_text_in_the_data_comes_back_only_as_marked_data(executor, context, budget):
    result = executor.run("SELECT description FROM demo_shop.shop.products WHERE product_id = 17",
                          policy=POLICY, context=context, budget=budget)
    injected = result.rows[0][0]
    assert "IGNORE ALL PREVIOUS INSTRUCTIONS" in injected
    assert wrap_untrusted(result.rows).startswith("<untrusted_data>")


def test_no_password_reaches_the_audit_log(executor, audit, context, budget, pool):
    executor.run(f"SELECT 1 FROM {ORDERS}", policy=POLICY, context=context, budget=budget)
    settings = get_settings()
    text = str(audit.for_run(context.run_id))
    assert settings.cubejs_api_secret not in text and settings.nova_jwt_secret not in text
    assert "connection-password" not in text
