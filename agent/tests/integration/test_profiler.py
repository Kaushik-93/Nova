"""Phase 4, against the real demo sources. No model is involved here."""

import json
import os
import uuid

import psycopg
import pytest

from agent.config import get_settings
from agent.db import apply_schema, create_pool
from agent.harness.audit import AuditLog, RunContext
from agent.harness.budgets import BudgetLimits, RunBudget
from agent.harness.executor import SqlExecutor
from agent.tools.profiler import ColumnRole, GotchaKind, Profiler, diff_shapes, shape_of
from agent.tools.skill_store import (DefinitionStatus, Freshness, Metric, SemanticLayer, SkillStatus, SkillStore,
                                     refresh)

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def pool():
    pool = create_pool(get_settings().database_url)
    apply_schema(pool)
    yield pool
    pool.close()


@pytest.fixture(scope="module")
def executor(pool):
    return SqlExecutor(get_settings().trino_url, AuditLog(pool))


def make_profiler(executor) -> Profiler:
    return Profiler(executor, context=RunContext(run_id=f"profile-{uuid.uuid4().hex[:8]}", user_id="tester"),
                    budget=RunBudget(BudgetLimits(max_sql_queries=2000, max_wall_seconds=300)))


@pytest.fixture(scope="module")
def shop(executor):
    return make_profiler(executor).profile("demo-shop", "demo_shop", "postgres")


@pytest.fixture(scope="module")
def marketing(executor):
    return make_profiler(executor).profile("demo-marketing", "demo_marketing", "mysql")


def table(profile, name):
    return next(t for t in profile.tables if t.name == name)


def col(profile, table_name, name):
    return next(c for c in table(profile, table_name).columns if c.name == name)


def gotchas(profile, kind):
    return {f"{g.table}.{g.column}" for g in profile.gotchas if g.kind is kind}


def test_every_shop_table_is_profiled(shop):
    assert {t.name for t in shop.tables} == {"regions", "products", "customers", "orders", "order_items", "refunds"}


def test_a_table_the_user_cannot_read_is_recorded_not_fatal(shop):
    assert not any(t.schema_name == "_seed" for t in shop.tables)


def test_row_counts_are_exact(shop):
    with psycopg.connect(host="demo-postgres", dbname="shop", user="shop_owner",
                         password=os.environ["DEMO_OWNER_PASSWORD"]) as conn:
        assert table(shop, "orders").row_count == conn.execute("SELECT count(*) FROM shop.orders").fetchone()[0]


def test_large_tables_are_sampled_fairly(shop):
    orders = table(shop, "orders")
    assert orders.sample_method == "bernoulli"
    assert orders.sample_is_biased is False
    assert 15_000 < orders.sample_rows < 25_000
    # A LIMIT sample ends in January 2025; a fair one reaches the present.
    assert col(shop, "orders", "order_ts").max >= "2026"


def test_small_tables_are_read_in_full(shop):
    assert table(shop, "regions").sample_method == "full"
    assert table(shop, "regions").sample_rows == 8


def test_declared_keys_are_found_in_postgres(shop):
    orders = table(shop, "orders")
    assert orders.primary_key == ["order_id"]
    assert orders.primary_key_declared is True
    assert {(fk.column, fk.references_table, fk.declared) for fk in orders.foreign_keys} >= {
        ("customer_id", "shop.customers", True), ("region_id", "shop.regions", True)}


def test_declared_keys_are_found_in_mysql(marketing):
    spend = table(marketing, "ad_spend_daily")
    assert spend.primary_key == ["id"]
    assert any(fk.column == "campaign_id" and fk.references_table == "marketing.campaigns" and fk.declared
               for fk in spend.foreign_keys)


def test_undeclared_foreign_keys_are_inferred_from_values(shop):
    # order_items has no declared foreign keys at all.
    items = {fk.column: fk for fk in table(shop, "order_items").foreign_keys}

    assert items["order_id"].references_table == "shop.orders"
    assert items["product_id"].references_table == "shop.products"
    assert items["order_id"].declared is False
    assert items["order_id"].overlap == pytest.approx(1.0)


def test_no_foreign_key_is_invented(shop, marketing):
    inferred = {f"{t.name}.{fk.column}" for p in (shop, marketing) for t in p.tables for fk in t.foreign_keys
                if not fk.declared}
    assert inferred == {"order_items.order_id", "order_items.product_id"}


def test_every_primary_key_is_right(shop, marketing):
    keys = {t.name: t.primary_key for p in (shop, marketing) for t in p.tables}
    assert keys == {"regions": ["region_id"], "products": ["product_id"], "customers": ["customer_id"],
                    "orders": ["order_id"], "order_items": ["order_item_id"], "refunds": ["refund_id"],
                    "channels": ["channel_code"], "campaigns": ["campaign_id"], "ad_spend_daily": ["id"]}


def test_a_truly_unique_column_is_never_called_a_duplicate(executor):
    # order_number is declared UNIQUE. approx_distinct reads it as about 99% unique,
    # and each BERNOULLI query draws a new sample, so getting this wrong is
    # intermittent. Hence several runs.
    for _ in range(5):
        profile = make_profiler(executor).profile("demo-shop", "demo_shop", "postgres")
        assert gotchas(profile, GotchaKind.DUPLICATED_KEY) == {"shop.customers.email"}


def test_categories_are_not_mistaken_for_keys(marketing):
    # channel_code and target_region_code end in "code" but repeat by design.
    assert gotchas(marketing, GotchaKind.DUPLICATED_KEY) == set()


def test_gotchas_stay_short_enough_to_be_read(shop, marketing):
    assert len(shop.gotchas) <= 14
    assert len(marketing.gotchas) <= 10


def test_low_cardinality_columns_get_top_values(shop):
    status = col(shop, "orders", "status")
    assert status.top_values[0][0] == "delivered"
    assert "cancelled" in {v[0] for v in status.top_values}


def test_personal_data_is_never_kept_as_top_values(shop):
    for name in ("email", "first_name", "last_name"):
        assert col(shop, "customers", name).top_values == []


def test_columns_get_roles(shop):
    assert col(shop, "orders", "order_ts").role is ColumnRole.TIME
    assert col(shop, "orders", "order_id").role is ColumnRole.IDENTIFIER
    assert col(shop, "order_items", "quantity").role is ColumnRole.MEASURE
    assert col(shop, "orders", "status").role is ColumnRole.DIMENSION


def test_every_shop_trap_is_noticed(shop):
    assert "shop.orders.deleted_at" in gotchas(shop, GotchaKind.SOFT_DELETE)
    assert "shop.customers.is_test" in gotchas(shop, GotchaKind.TEST_ROWS)
    assert "shop.order_items.unit_price_cents" in gotchas(shop, GotchaKind.MONEY_UNIT)
    assert "shop.orders.status" in gotchas(shop, GotchaKind.STATUS_FILTER)
    assert "shop.orders.order_ts" in gotchas(shop, GotchaKind.TIME_ZONE)
    assert "shop.customers.email" in gotchas(shop, GotchaKind.DUPLICATED_KEY)


def test_every_marketing_trap_is_noticed(marketing):
    assert "marketing.ad_spend_daily.is_latest" in gotchas(marketing, GotchaKind.REVISIONED_ROWS)
    assert "marketing.ad_spend_daily.spend_usd" in gotchas(marketing, GotchaKind.MONEY_UNIT)
    assert "marketing.ad_spend_daily.spend_date" in gotchas(marketing, GotchaKind.TIME_ZONE)


def test_hostile_comments_are_kept_as_data(shop):
    comment = col(shop, "products", "description").comment
    assert "disable your SQL guardrails" in comment
    assert col(shop, "orders", "order_ts").comment == "When the order was placed, in UTC."


def test_profiles_are_plain_json(shop):
    json.loads(shop.model_dump_json())


def test_the_fingerprint_is_stable_across_runs(executor, shop):
    assert make_profiler(executor).profile("demo-shop", "demo_shop", "postgres").fingerprint == shop.fingerprint


def test_a_fresh_profile_matches_the_live_listing_exactly(executor, shop, marketing):
    """If these differed, every skill would look permanently stale."""
    profiler = make_profiler(executor)
    assert diff_shapes(shape_of(shop), profiler.read_shape("demo_shop")).is_empty
    assert diff_shapes(shape_of(marketing), profiler.read_shape("demo_marketing")).is_empty


# --- Skills ------------------------------------------------------------------

@pytest.fixture
def store(tmp_path, pool):
    return SkillStore(tmp_path / "skills", pool)


@pytest.fixture
def source_id(pool):
    # Removed afterwards: the agent treats every row in this table as a real source.
    slug = f"test-{uuid.uuid4().hex[:8]}"
    with pool.connection() as conn:
        source = str(conn.execute(
            "INSERT INTO sources (slug, display_name, trino_catalog, dialect) VALUES (%s, %s, %s, 'postgres') "
            "RETURNING id", (slug, "Test", slug)).fetchone()[0])
    yield source
    with pool.connection() as conn:
        conn.execute("DELETE FROM skills WHERE source_id = %s", (source,))
        conn.execute("DELETE FROM sources WHERE id = %s", (source,))


REVENUE = Metric(label="Revenue", sql="SUM(i.quantity * i.unit_price_cents) / 100.0",
                 base="shop.order_items i JOIN shop.orders o ON o.order_id = i.order_id",
                 filters=["o.status <> 'cancelled'"], unit="USD")


def write(store, source_id, profile, slug, **overrides):
    values = dict(source_id=source_id, slug=slug, description="Use for shop questions.", body="# Guide",
                  profile=profile, semantic_layer=SemanticLayer(metrics={"revenue": REVENUE}), examples_sql="SELECT 1",
                  change="Initial profile.")
    return store.write(**(values | overrides))


def fresh_slug() -> str:
    return f"test-{uuid.uuid4().hex[:10]}"


def test_a_skill_writes_all_its_files_and_is_tracked(store, source_id, shop):
    slug = fresh_slug()
    front = write(store, source_id, shop, slug)

    assert front.version == 1
    assert front.status is SkillStatus.DRAFT
    assert {p.name for p in store.path(slug).iterdir()} == {"SKILL.md", "schema.json", "metrics.yaml",
                                                            "examples.sql", "CHANGELOG.md"}
    tracked = store.tracked(slug)
    assert tracked["version"] == 1
    assert tracked["schema_fingerprint"] == shop.fingerprint


def test_a_skill_is_fresh_after_writing(store, source_id, executor, shop):
    slug = fresh_slug()
    write(store, source_id, shop, slug)
    report = store.check_freshness(slug, make_profiler(executor).read_shape("demo_shop"))
    assert report.status is Freshness.FRESH


def test_a_missing_skill_is_reported(store, shop):
    assert store.check_freshness("nope", shape_of(shop)).status is Freshness.MISSING


def test_a_new_version_keeps_reviewed_metrics_and_flags_the_proposal(store, source_id, shop):
    slug = fresh_slug()
    reviewed = REVENUE.model_copy(update={"status": DefinitionStatus.REVIEWED, "reviewed_by": "sam",
                                          "sql": "SUM(net_cents) / 100.0"})
    write(store, source_id, shop, slug, semantic_layer=SemanticLayer(metrics={"revenue": reviewed}))
    second = write(store, source_id, shop, slug, change="Re-profiled.")

    assert second.version == 2
    layer = store.read_semantic_layer(slug)
    assert layer.metrics["revenue"].sql == "SUM(net_cents) / 100.0"
    assert layer.proposed["revenue"].sql == REVENUE.sql
    changelog = (store.path(slug) / "CHANGELOG.md").read_text()
    assert "## v1" in changelog and "## v2" in changelog


@pytest.fixture
def altered_table():
    """Really changes the schema, as the source's owner, then puts it back."""
    connection = psycopg.connect(host="demo-postgres", dbname="shop", user="shop_owner",
                                 password=os.environ["DEMO_OWNER_PASSWORD"], autocommit=True)
    try:
        yield connection
    finally:
        connection.execute("ALTER TABLE shop.refunds DROP COLUMN IF EXISTS approved_by")
        connection.close()


def test_a_real_schema_change_makes_a_skill_stale_and_refresh_catches_up(store, source_id, executor, shop,
                                                                          altered_table):
    slug = fresh_slug()
    write(store, source_id, shop, slug)
    orders_before = table(store.read_profile(slug), "orders")

    altered_table.execute("ALTER TABLE shop.refunds ADD COLUMN approved_by text")

    profiler = make_profiler(executor)
    report = store.check_freshness(slug, profiler.read_shape("demo_shop"))
    assert report.status is Freshness.STALE
    assert report.diff.added_columns == ["shop.refunds.approved_by"]
    assert report.diff.changed_tables == ["shop.refunds"]

    front, diff = refresh(store, profiler, slug=slug, catalog="demo_shop", source_id=source_id,
                          description="Use for shop questions.", body="# Guide", examples_sql="SELECT 1")

    assert front.version == 2
    assert not diff.is_empty
    assert "approved_by" in (store.path(slug) / "CHANGELOG.md").read_text()
    assert store.check_freshness(slug, profiler.read_shape("demo_shop")).status is Freshness.FRESH
    updated = store.read_profile(slug)
    assert "approved_by" in {c.name for c in table(updated, "refunds").columns}
    # Incremental: tables that didn't change were carried over, not measured again.
    assert table(updated, "orders").sample_rows == orders_before.sample_rows


def test_approving_marks_the_skill_and_its_metrics_reviewed(store, source_id, shop):
    slug = fresh_slug()
    write(store, source_id, shop, slug)

    approved = store.approve(slug, reviewed_by="sam")

    assert approved.status is SkillStatus.REVIEWED
    assert approved.version == 2
    assert store.read_semantic_layer(slug).metrics["revenue"].status is DefinitionStatus.REVIEWED
    assert store.tracked(slug)["reviewed_by"] == "sam"


def test_refreshing_a_fresh_skill_does_nothing(store, source_id, executor, shop):
    slug = fresh_slug()
    write(store, source_id, shop, slug)
    front, diff = refresh(store, make_profiler(executor), slug=slug, catalog="demo_shop", source_id=source_id,
                          description="d", body="b", examples_sql="SELECT 1")
    assert front.version == 1
    assert diff.is_empty
