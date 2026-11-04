import pytest
import yaml
from deepagents.backends import FilesystemBackend
from deepagents.middleware.skills import _list_skills

from agent.harness.errors import InvalidRequest
from agent.tools.profiler import (ColumnProfile, ColumnRole, GotchaKind, TableProfile, classify_column, diff_shapes,
                                  find_gotchas, fingerprint)
from agent.tools.skill_store import (DefinitionStatus, Metric, SemanticLayer, SkillFrontmatter, SkillStatus,
                                     describe_change, merge_semantic_layers, parse_frontmatter, render_frontmatter,
                                     skill_slug)

SHAPE = {"shop.orders": {"id": "bigint", "status": "varchar"}, "shop.items": {"id": "bigint"}}


def front(**overrides) -> SkillFrontmatter:
    values = dict(name="demo-shop", description="Use for shop questions.", source_id="abc", dialect="postgres",
                  schema_fingerprint="f" * 64, version=2, status=SkillStatus.REVIEWED,
                  generated_at="2026-11-04T00:00:00+00:00", reviewed_by="sam")
    return SkillFrontmatter(**(values | overrides))


# --- Fingerprints ------------------------------------------------------------

def test_a_fingerprint_ignores_listing_order():
    reordered = {"shop.items": {"id": "bigint"}, "shop.orders": {"status": "varchar", "id": "bigint"}}
    assert fingerprint(SHAPE) == fingerprint(reordered)


@pytest.mark.parametrize("change", [
    {"shop.orders": {"id": "bigint"}, "shop.items": {"id": "bigint"}},
    {**SHAPE, "shop.new": {"id": "bigint"}},
    {"shop.orders": {"id": "integer", "status": "varchar"}, "shop.items": {"id": "bigint"}},
    {"shop.orders": {"id": "bigint", "state": "varchar"}, "shop.items": {"id": "bigint"}},
], ids=["dropped column", "new table", "changed type", "renamed column"])
def test_a_fingerprint_changes_with_the_schema(change):
    assert fingerprint(SHAPE) != fingerprint(change)


def test_a_diff_reports_each_kind_of_change():
    new = {"shop.orders": {"id": "integer", "total": "bigint"}, "shop.refunds": {"id": "bigint"}}
    diff = diff_shapes(SHAPE, new)

    assert diff.added_tables == ["shop.refunds"]
    assert diff.removed_tables == ["shop.items"]
    assert diff.added_columns == ["shop.orders.total"]
    assert diff.removed_columns == ["shop.orders.status"]
    assert diff.changed_types == ["shop.orders.id bigint -> integer"]
    assert diff.changed_tables == ["shop.orders", "shop.refunds"]


def test_no_change_gives_an_empty_diff():
    diff = diff_shapes(SHAPE, dict(SHAPE))
    assert diff.is_empty
    assert describe_change(diff) == "- No schema changes"


def test_a_diff_handles_awkward_column_and_type_names():
    old = {"shop.t": {"a.b c": "timestamp(6) with time zone"}}
    new = {"shop.t": {"a.b c": "timestamp(3) with time zone"}}
    assert diff_shapes(old, new).changed_tables == ["shop.t"]


def test_a_diff_describes_itself_for_the_changelog():
    assert "- Added columns: shop.orders.x" in describe_change(diff_shapes(SHAPE, {
        "shop.orders": {**SHAPE["shop.orders"], "x": "bigint"}, "shop.items": {"id": "bigint"}}))


# --- Skill names and files ---------------------------------------------------

@pytest.mark.parametrize("value, expected", [("demo_shop", "demo-shop"), ("Demo Shop!", "demo-shop"),
                                             ("a__b", "a-b"), ("--x--", "x"), ("sales2026", "sales2026")])
def test_slugs_meet_the_skill_naming_rules(value, expected):
    assert skill_slug(value) == expected


def test_slugs_are_length_limited():
    assert len(skill_slug("x" * 200)) == 64


def test_an_unusable_name_is_refused():
    with pytest.raises(InvalidRequest):
        skill_slug("!!!")


def test_frontmatter_round_trips():
    parsed, body = parse_frontmatter(render_frontmatter(front()) + "# Guide\n")
    assert parsed == front()
    assert body == "# Guide\n"


def test_an_empty_reviewer_reads_back_as_none():
    parsed, _ = parse_frontmatter(render_frontmatter(front(reviewed_by=None)) + "x")
    assert parsed.reviewed_by is None


def test_nova_fields_live_under_metadata_as_strings():
    data = yaml.safe_load(render_frontmatter(front()).strip().strip("-"))
    assert set(data) == {"name", "description", "metadata"}
    assert all(isinstance(v, str) for v in data["metadata"].values())


def test_an_overlong_description_is_refused_rather_than_truncated():
    with pytest.raises(InvalidRequest, match="limit is 1024"):
        render_frontmatter(front(description="x" * 1025))


def test_deep_agents_really_loads_a_skill_we_write(tmp_path):
    skill = tmp_path / "skills" / "demo-shop"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(render_frontmatter(front()) + "# Guide\n")

    backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
    loaded = _list_skills(backend, "/skills/")

    assert [s["name"] for s in loaded] == ["demo-shop"]
    assert loaded[0]["description"] == "Use for shop questions."
    assert loaded[0]["metadata"]["schema_fingerprint"] == "f" * 64
    assert loaded[0]["metadata"]["status"] == "reviewed"


# --- Reviewed definitions ----------------------------------------------------

def metric(sql: str, status=DefinitionStatus.INFERRED) -> Metric:
    return Metric(label="Revenue", sql=sql, base="shop.orders o", status=status)


def test_a_reviewed_metric_survives_a_different_proposal():
    reviewed = metric("SUM(a) - SUM(refunds)", DefinitionStatus.REVIEWED)
    merged = merge_semantic_layers(SemanticLayer(metrics={"revenue": reviewed}),
                                   SemanticLayer(metrics={"revenue": metric("SUM(a)")}))

    assert merged.metrics["revenue"] == reviewed
    assert merged.proposed["revenue"].sql == "SUM(a)"


def test_an_identical_proposal_raises_no_flag():
    reviewed = metric("SUM(a)", DefinitionStatus.REVIEWED)
    merged = merge_semantic_layers(SemanticLayer(metrics={"revenue": reviewed}),
                                   SemanticLayer(metrics={"revenue": metric("SUM(a)")}))
    assert merged.proposed == {}


def test_a_reviewed_metric_is_kept_when_the_proposal_omits_it():
    reviewed = metric("SUM(a)", DefinitionStatus.REVIEWED)
    merged = merge_semantic_layers(SemanticLayer(metrics={"revenue": reviewed}), SemanticLayer())
    assert merged.metrics["revenue"] == reviewed


def test_inferred_metrics_are_simply_replaced():
    merged = merge_semantic_layers(SemanticLayer(metrics={"revenue": metric("OLD")}),
                                   SemanticLayer(metrics={"revenue": metric("NEW")}))
    assert merged.metrics["revenue"].sql == "NEW"
    assert merged.proposed == {}


def test_earlier_proposals_are_not_lost():
    current = SemanticLayer(proposed={"margin": metric("x")})
    assert "margin" in merge_semantic_layers(current, SemanticLayer()).proposed


# --- Heuristics --------------------------------------------------------------

def table(*columns: ColumnProfile, **overrides) -> TableProfile:
    return TableProfile(**({"schema_name": "s", "name": "t", "sample_rows": 1000, "columns": list(columns)} | overrides))


def column(name: str, kind: str = "varchar", **overrides) -> ColumnProfile:
    return ColumnProfile(**({"name": name, "type": kind, "nullable": True} | overrides))


def kinds(*columns: ColumnProfile, **overrides) -> set[GotchaKind]:
    return {g.kind for g in find_gotchas([table(*columns, **overrides)])}


@pytest.mark.parametrize("found, expected", [
    (column("deleted_at", "timestamp(6)", null_pct=99.0), GotchaKind.SOFT_DELETE),
    (column("is_deleted", "boolean"), GotchaKind.SOFT_DELETE),
    (column("is_test", "boolean"), GotchaKind.TEST_ROWS),
    (column("unit_price_cents", "integer"), GotchaKind.MONEY_UNIT),
    (column("spend_usd", "decimal(12,2)"), GotchaKind.MONEY_UNIT),
    (column("is_latest", "tinyint"), GotchaKind.REVISIONED_ROWS),
    (column("revision", "tinyint"), GotchaKind.REVISIONED_ROWS),
    (column("order_ts", "timestamp(6) with time zone"), GotchaKind.TIME_ZONE),
    (column("spend_date", "date"), GotchaKind.TIME_ZONE),
    (column("status", top_values=[["paid", 5], ["cancelled", 1]]), GotchaKind.STATUS_FILTER),
    (column("email", duplicate_values=50), GotchaKind.DUPLICATED_KEY),
])
def test_traps_are_flagged(found, expected):
    assert expected in kinds(found)


@pytest.mark.parametrize("innocent", [
    column("customer_name"),
    column("quantity", "integer"),
    column("email", duplicate_values=0),
    column("email", distinct=950),
    column("status"),
    column("deleted_count", "integer"),
    column("testimonial"),
])
def test_ordinary_columns_are_not_flagged(innocent):
    assert kinds(innocent) == set()


def test_several_currencies_are_flagged_with_their_values():
    found = find_gotchas([table(column("currency", "char(3)", top_values=[["USD", 5], ["EUR", 3]]))])
    assert found[0].kind is GotchaKind.MIXED_CURRENCY
    assert "USD, EUR" in found[0].detail


def test_one_currency_is_not_a_trap():
    assert kinds(column("currency", "char(3)", top_values=[["USD", 5]])) == set()


def test_repeated_findings_collapse_to_one_line_per_table():
    found = find_gotchas([table(column("a_cents", "integer"), column("b_cents", "integer"), column("c_at", "date"),
                                column("d_at", "date"))])
    assert len(found) == 2
    assert all("Applies to:" in g.detail for g in found)
    assert "a_cents, b_cents" in found[0].detail


def test_different_findings_stay_separate():
    found = find_gotchas([table(column("a_cents", "integer"), column("b_usd", "decimal(10,2)"))])
    assert len(found) == 2


def test_a_status_gotcha_names_the_values():
    gotcha = find_gotchas([table(column("status", top_values=[["paid", 5], ["cancelled", 1]]))])[0]
    assert "cancelled" in gotcha.detail


@pytest.mark.parametrize("found, role", [
    (column("order_id", "bigint"), ColumnRole.IDENTIFIER),
    (column("order_ts", "timestamp(6) with time zone"), ColumnRole.TIME),
    (column("spend_date", "date"), ColumnRole.TIME),
    (column("quantity", "integer"), ColumnRole.MEASURE),
    (column("spend_usd", "decimal(12,2)"), ColumnRole.MEASURE),
    (column("status", distinct=6), ColumnRole.DIMENSION),
    (column("description", distinct=5000), ColumnRole.TEXT),
    (column("is_test", "boolean"), ColumnRole.FLAG),
    (column("is_latest", "tinyint"), ColumnRole.FLAG),
])
def test_columns_are_classified(found, role):
    assert classify_column(found, table(found)) is role
