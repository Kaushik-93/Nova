"""The harness's check on what the model proposes, using hand-written drafts against
real Trino. No model is involved: this is what stands between a confident model and a
wrong skill."""

import uuid

import pytest

from agent.config import get_settings
from agent.db import apply_schema, create_pool
from agent.harness.audit import AuditLog, RunContext
from agent.harness.budgets import BudgetLimits, RunBudget
from agent.harness.errors import BudgetExceeded
from agent.harness.executor import SqlExecutor
from agent.harness.guardrails import SqlPolicy
from agent.subagents.profiler import (ProposedExample, ProposedMetric, SkillDraft, Submission, VerificationReport,
                                      make_submit_skill, metric_sql, to_examples_sql, to_semantic_layer, verify_draft)

pytestmark = pytest.mark.integration

ORDERS = "demo_shop.shop.orders o"
POLICY = SqlPolicy(allowed_catalogs=frozenset({"demo_shop"}), max_rows=1000)
GOOD_METRIC = ProposedMetric(key="orders", label="Orders", sql="COUNT(*)", base=ORDERS,
                             filters=["o.deleted_at IS NULL", "o.status <> 'cancelled'"], description="d")
GOOD_EXAMPLE = ProposedExample(question="How many orders?", sql="SELECT COUNT(*) FROM demo_shop.shop.orders")


def metric(**overrides) -> ProposedMetric:
    return GOOD_METRIC.model_copy(update=overrides)


def draft(*metrics, examples=(GOOD_EXAMPLE,)) -> SkillDraft:
    return SkillDraft(description="d", body="b", metrics=list(metrics) or [GOOD_METRIC], examples=list(examples))


@pytest.fixture(scope="module")
def pool():
    pool = create_pool(get_settings().database_url)
    apply_schema(pool)
    yield pool
    pool.close()


@pytest.fixture(scope="module")
def executor(pool):
    return SqlExecutor(get_settings().trino_url, AuditLog(pool))


@pytest.fixture
def harness(executor):
    return dict(executor=executor, policy=POLICY, context=RunContext(run_id=f"verify-{uuid.uuid4().hex[:8]}",
                                                                     user_id="tester"),
                budget=RunBudget(BudgetLimits()))


def test_metric_sql_combines_its_parts():
    assert metric_sql(GOOD_METRIC) == ("SELECT COUNT(*) AS value FROM demo_shop.shop.orders o "
                                       "WHERE (o.deleted_at IS NULL) AND (o.status <> 'cancelled')")


def test_filters_cannot_escape_each_other():
    sql = metric_sql(metric(filters=["a = 1 OR b = 2", "c = 3"]))
    assert "WHERE (a = 1 OR b = 2) AND (c = 3)" in sql


def test_a_good_draft_passes_with_real_values(harness):
    report = verify_draft(draft(), **harness)
    assert report.failures == []
    assert report.metric_values["orders"] > 50_000
    assert report.verified_examples == [GOOD_EXAMPLE]


@pytest.mark.parametrize("bad, reason", [
    (metric(sql="SUM(no_such_column)"), "COLUMN_NOT_FOUND"),
    (metric(base="demo_shop.shop.no_such_table o"), "TABLE_NOT_FOUND"),
    (metric(base="demo_marketing.marketing.campaigns c", sql="COUNT(*)", filters=[]), "not a connected source"),
    (metric(base="system.runtime.queries q", sql="COUNT(*)", filters=[]), "system table"),
    (metric(sql="SUM(o.tax_cents)", filters=["o.status = 'no such status'"]), "NULL"),
    (metric(key="Not Snake"), "snake_case"),
], ids=["bad column", "bad table", "other source", "system table", "matches nothing", "bad key"])
def test_a_bad_metric_is_caught(harness, bad, reason):
    report = verify_draft(draft(bad), **harness)
    assert bad.key not in report.metric_values
    assert reason in report.failures[0].reason


def test_a_metric_that_returns_many_rows_is_caught(harness):
    report = verify_draft(draft(metric(sql="COUNT(*) OVER ()", filters=[])), **harness)
    assert "exactly one value" in report.failures[0].reason


def test_duplicate_metric_keys_are_caught(harness):
    report = verify_draft(draft(GOOD_METRIC, GOOD_METRIC), **harness)
    assert len(report.failures) == 1


@pytest.mark.parametrize("sql, reason", [
    ("SELECT no_such_column FROM demo_shop.shop.orders", "COLUMN_NOT_FOUND"),
    ("SELECT * FROM demo_shop.shop.orders WHERE status = 'no such status'", "no rows"),
    ("DELETE FROM demo_shop.shop.orders", "only SELECT"),
    ("SELECT * FROM TABLE(demo_shop.system.query(query => 'select 1'))", "not allowed"),
], ids=["bad column", "no rows", "write", "passthrough"])
def test_a_bad_example_is_caught_and_left_out(harness, sql, reason):
    bad = ProposedExample(question="q", sql=sql)
    report = verify_draft(draft(examples=[GOOD_EXAMPLE, bad]), **harness)
    assert report.verified_examples == [GOOD_EXAMPLE]
    assert reason in report.failures[0].reason


def test_a_spent_budget_stops_verification(harness):
    harness["budget"] = RunBudget(BudgetLimits(max_sql_queries=1))
    with pytest.raises(BudgetExceeded):
        verify_draft(draft(), **harness)


def test_only_verified_metrics_reach_the_semantic_layer(harness):
    bad = metric(key="broken", sql="SUM(no_such_column)")
    candidate = draft(GOOD_METRIC, bad)
    layer = to_semantic_layer(candidate, verify_draft(candidate, **harness))
    assert set(layer.metrics) == {"orders"}


def test_a_question_cannot_break_out_of_its_comment():
    report = VerificationReport(verified_examples=[ProposedExample(question="q\nDROP TABLE x;", sql="SELECT 1;")])
    text = to_examples_sql(report)
    assert text == "-- q DROP TABLE x;\nSELECT 1;"


def submit(tool, candidate: SkillDraft) -> str:
    return tool.invoke(candidate.model_dump())


def test_submitting_a_good_skill_is_accepted(harness):
    submission = Submission()
    tool = make_submit_skill(submission, **harness)
    assert "Accepted" in submit(tool, draft())
    assert submission.accepted


def test_failures_go_back_to_the_model_until_the_limit(harness):
    submission = Submission()
    tool = make_submit_skill(submission, **harness)
    broken = draft(GOOD_METRIC, metric(key="broken", sql="SUM(no_such_column)"))

    first, second, third = submit(tool, broken), submit(tool, broken), submit(tool, broken)

    assert "Not accepted" in first and "broken" in first
    assert "Not accepted" in second
    assert "without the items that failed" in third
    assert submission.accepted
    assert set(submission.report.metric_values) == {"orders"}
