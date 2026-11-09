"""The harness's check on what the model proposes, using hand-written drafts against real Cube and
Trino. No model is involved: this is what stands between a confident model and a wrong skill."""

import uuid
from pathlib import Path

import pytest

from agent.config import get_settings
from agent.db import apply_schema, create_pool
from agent.harness.audit import AuditLog, RunContext
from agent.harness.budgets import BudgetLimits, RunBudget
from agent.harness.errors import BudgetExceeded, QueryFailure
from agent.harness.executor import SqlExecutor
from agent.semantic.access import Tenancy
from agent.semantic.client import CubeClient
from agent.semantic.model import Cube, DataModel, Dimension, Filter, Join, Measure, MemberMeta, Status
from agent.semantic.query import Example, SemanticQuery
from agent.semantic.verify import Verifier, finalize
from agent.subagents.profiler import SkillDraft, Submission, make_submit_skill
from agent.tools.profiler import ColumnProfile, Profiler, SourceProfile, TableProfile

pytestmark = pytest.mark.integration

CATALOG = "demo_shop"
ORDERS, ITEMS, CUSTOMERS = "demo_shop__orders", "demo_shop__order_items", "demo_shop__customers"


def orders(**overrides) -> Cube:
    values = dict(
        name=ORDERS, title="Orders", sql="SELECT o.* FROM demo_shop.shop.orders o WHERE o.deleted_at IS NULL",
        joins=[Join(name=CUSTOMERS, relationship="many_to_one", sql=f"{{CUBE}}.customer_id = {{{CUSTOMERS}}}.customer_id")],
        dimensions=[Dimension(name="order_id", sql="{CUBE}.order_id", type="number", primary_key=True),
                    Dimension(name="status", sql="{CUBE}.status", type="string"),
                    Dimension(name="ordered_at", sql="{CUBE}.order_ts", type="time")],
        measures=[Measure(name="count", type="count", filters=[Filter(sql="{CUBE}.status <> 'cancelled'")]),
                  Measure(name="tax", type="sum", sql="{CUBE}.tax_cents / 100.0", format="currency")])
    return Cube(**(values | overrides))


def items(**overrides) -> Cube:
    values = dict(
        name=ITEMS, sql_table="demo_shop.shop.order_items",
        joins=[Join(name=ORDERS, relationship="many_to_one", sql=f"{{CUBE}}.order_id = {{{ORDERS}}}.order_id")],
        dimensions=[Dimension(name="order_item_id", sql="{CUBE}.order_item_id", type="number", primary_key=True)],
        measures=[Measure(name="revenue", type="sum", sql="{CUBE}.quantity * {CUBE}.unit_price_cents / 100.0")])
    return Cube(**(values | overrides))


def customers() -> Cube:
    return Cube(name=CUSTOMERS, sql_table="demo_shop.shop.customers",
                dimensions=[Dimension(name="customer_id", sql="{CUBE}.customer_id", type="number", primary_key=True),
                            Dimension(name="region_id", sql="{CUBE}.region_id", type="number")],
                measures=[Measure(name="count", type="count")])


GOOD_EXAMPLE = Example(question="How many orders by status?",
                       query=SemanticQuery(measures=[f"{ORDERS}.count"], dimensions=[f"{ORDERS}.status"]))


def model(*cubes: Cube) -> DataModel:
    return DataModel(cubes=list(cubes) or [orders(), items(), customers()])


@pytest.fixture(scope="module")
def pool():
    pool = create_pool(get_settings().database_url)
    apply_schema(pool)
    yield pool
    pool.close()


@pytest.fixture(scope="module")
def executor(pool):
    return SqlExecutor(get_settings().trino_url, AuditLog(pool))


@pytest.fixture(scope="module")
def cube():
    settings = get_settings()
    client = CubeClient(settings.cube_url, settings.cubejs_api_secret)
    yield client
    client.close()


@pytest.fixture(scope="module")
def profile(executor):
    """The real table layout, read live. Skills aren't in the repo, so these can't rely on one."""
    context = RunContext(run_id=f"test-{uuid.uuid4().hex[:10]}", user_id="tester")
    shape = Profiler(executor, context=context, budget=RunBudget(BudgetLimits())).read_shape(CATALOG)
    return SourceProfile(source_slug="demo-shop", catalog=CATALOG, dialect="postgres", fingerprint="f",
                         profiled_at="2026-11-09T00:00:00Z", tables=[
                             TableProfile(schema_name=name.split(".")[0], name=name.split(".")[1],
                                          columns=[ColumnProfile(name=c, type=t, nullable=True) for c, t in cols.items()])
                             for name, cols in shape.items()])


@pytest.fixture
def verifier(cube, executor, profile):
    return Verifier(cube=cube, executor=executor, skills_dir=Path(get_settings().nova_skills_dir),
                    slug=f"test-verify-{uuid.uuid4().hex[:8]}", profile=profile,
                    context=RunContext(run_id=f"verify-{uuid.uuid4().hex[:8]}", user_id="tester"),
                    budget=RunBudget(BudgetLimits(max_sql_queries=500)))


def reasons(report, **where) -> list[str]:
    return [p.reason for p in report.problems if all(getattr(p, key) == value for key, value in where.items())]


def test_a_good_model_passes_with_real_values(verifier):
    report = verifier.verify(model(), [GOOD_EXAMPLE])
    assert report.problems == []
    assert report.values[f"{ORDERS}.count"] > 50_000
    assert report.values[f"{ITEMS}.revenue"] > 0
    assert report.examples == [GOOD_EXAMPLE]


def test_the_draft_is_gone_afterwards(verifier):
    verifier.verify(model(), [GOOD_EXAMPLE])
    drafts = Path(get_settings().nova_skills_dir) / "_drafts"
    assert not any(drafts.iterdir()) if drafts.exists() else True


@pytest.mark.parametrize("bad, expected", [
    (Measure(name="tax", type="sum", sql="{CUBE}.no_such_column"), "no_such_column"),
    (Measure(name="tax", type="sum", sql="{CUBE}.tax_cents", filters=[Filter(sql="{CUBE}.status = 'nope'")]), "NULL"),
], ids=["bad column", "matches nothing"])
def test_a_bad_measure_is_caught_where_it_is(verifier, bad, expected):
    count = Measure(name="count", type="count")
    report = verifier.verify(model(orders(measures=[count, bad]), items(), customers()), [])
    assert f"{ORDERS}.tax" not in report.values
    assert any(expected in reason for reason in reasons(report, cube=ORDERS, member="tax"))


def test_a_join_that_multiplies_rows_is_caught(verifier):
    """Declared many-to-one, but an order has many items: every order total would be inflated."""
    wrong = orders(joins=[Join(name=ITEMS, relationship="many_to_one", sql=f"{{CUBE}}.order_id = {{{ITEMS}}}.order_id")])
    report = verifier.verify(model(wrong, items(joins=[]), customers()), [])
    [reason] = reasons(report, cube=ORDERS, join=ITEMS)
    assert "turns" in reason


def test_a_primary_key_that_isnt_unique_is_caught(verifier):
    keyed = items(dimensions=[Dimension(name="order_id", sql="{CUBE}.order_id", type="number", primary_key=True)])
    report = verifier.verify(model(orders(), keyed, customers()), [])
    assert any("isn't unique" in reason for reason in reasons(report, cube=ITEMS))


def test_static_problems_stop_before_cube_sees_anything(verifier):
    report = verifier.verify(model(orders(sql="SELECT * FROM demo_marketing.marketing.campaigns"), items(), customers()), [])
    assert any("not a connected source" in reason for reason in reasons(report, cube=ORDERS))
    assert report.values == {}


def test_a_bad_example_is_caught_and_left_out(verifier):
    bad = Example(question="Revenue by colour?", query=SemanticQuery(measures=[f"{ORDERS}.count"],
                                                                     dimensions=[f"{ORDERS}.colour"]))
    empty = Example(question="Orders with no status?", query=SemanticQuery(
        measures=[f"{ORDERS}.count"], dimensions=[f"{ORDERS}.status"],
        filters=[{"member": f"{ORDERS}.status", "operator": "equals", "values": ["no such status"]}]))
    report = verifier.verify(model(), [GOOD_EXAMPLE, bad, empty])
    assert report.examples == [GOOD_EXAMPLE]
    assert {p.example for p in report.problems} == {bad.question, empty.question}


def test_a_cube_without_its_tenant_column_is_caught(verifier):
    """Pretending region is the tenant: a cube whose rows don't carry it can't be limited."""
    verifier.tenancy = Tenancy(column="region_id")
    narrow = orders(sql="SELECT o.order_id, o.status, o.order_ts, o.tax_cents, o.customer_id "
                        "FROM demo_shop.shop.orders o WHERE o.deleted_at IS NULL")
    report = verifier.verify(model(narrow, items(joins=[]), customers()), [])
    assert any("can't be limited" in reason for reason in reasons(report, cube=ORDERS))
    assert not reasons(report, cube=CUSTOMERS)  # its table carries region_id, so the limit works


def test_finalize_drops_what_fails_and_keeps_what_works(verifier):
    broken = orders(measures=[Measure(name="count", type="count"), Measure(name="bad", type="sum", sql="{CUBE}.nope")])
    final, report, dropped = finalize(verifier, model(broken, items(), customers()), [GOOD_EXAMPLE])
    assert [m.name for m in final.cube(ORDERS).measures] == ["count"]
    assert report.problems == [] and len(dropped) == 1


def test_finalize_gives_up_on_a_model_that_doesnt_work_as_a_whole(verifier):
    with pytest.raises(QueryFailure):
        finalize(verifier, DataModel(cubes=[]), [])


def test_a_spent_budget_stops_verification(verifier):
    verifier.budget = RunBudget(BudgetLimits(max_sql_queries=1))
    with pytest.raises(BudgetExceeded):
        verifier.verify(model(), [GOOD_EXAMPLE])


# --- Submitting --------------------------------------------------------------------------------

def draft(*cubes: Cube, examples=(GOOD_EXAMPLE,)) -> SkillDraft:
    return SkillDraft(description="d", body="b", cubes=list(cubes) or model().cubes, examples=list(examples))


def submit(tool, candidate: SkillDraft) -> str:
    return tool.invoke(candidate.model_dump(mode="json"))


def test_submitting_a_good_skill_is_accepted(verifier):
    submission = Submission()
    assert "Accepted" in submit(make_submit_skill(submission, verifier=verifier, reviewed=None), draft())
    assert submission.accepted


def test_failures_go_back_to_the_model_until_the_limit(verifier):
    submission = Submission()
    tool = make_submit_skill(submission, verifier=verifier, reviewed=None)
    broken = draft(orders(measures=[Measure(name="count", type="count"), Measure(name="bad", type="sum", sql="{CUBE}.nope")]),
                   items(), customers())
    first, second, third = submit(tool, broken), submit(tool, broken), submit(tool, broken)
    assert "Not accepted" in first and "bad" in first
    assert "Not accepted" in second
    assert "without the parts that failed" in third
    assert submission.accepted


def test_a_draft_cant_mark_its_own_definitions_reviewed():
    sneaky = orders(measures=[Measure(name="count", type="count", meta=MemberMeta(status=Status.REVIEWED, reviewed_by="cfo"))])
    member = draft(sneaky).model().cube(ORDERS).member("count")
    assert (member.meta.status, member.meta.reviewed_by) == (Status.INFERRED, None)
