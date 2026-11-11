"""The checks between a drafted data model and Cube. Each case here is a way a model, prompted by
something hostile in a source, could get Cube to run what it shouldn't."""

import pytest

from agent.harness.errors import SqlRejected
from agent.semantic.access import Tenancy, apply_tenancy, tables_of, unlimitable
from agent.semantic.model import TENANT_DIMENSION, Cube, DataModel, Dimension, Filter, Join, Measure, Segment
from agent.semantic.query import QueryFilter, SemanticQuery, TimeDimension
from agent.semantic.validate import check_model, render_checked
from agent.semantic.verify import _join_condition, prune
from agent.semantic.validate import Problem
from agent.tools.profiler import ColumnProfile, SourceProfile, TableProfile

CATALOG = "rent"
TABLES = {"rent.app.invoices", "rent.app.tenants", "rent.app.fx"}
KEY = Dimension(name="id", sql="{CUBE}.invoice_id", type="number", primary_key=True)


def cube(**overrides) -> Cube:
    values = dict(name="rent__invoices", sql_table="rent.app.invoices", dimensions=[KEY],
                  measures=[Measure(name="billed", type="sum", sql="{CUBE}.total_cents / 100.0")])
    return Cube(**(values | overrides))


def problems(*cubes: Cube, taken: set[str] = frozenset()) -> list[str]:
    return [p.reason for p in check_model(DataModel(cubes=list(cubes)), catalog=CATALOG, tables=TABLES, taken=taken)]


def test_a_plain_model_passes():
    fx = Cube(name="rent__fx", sql_table="rent.app.fx", dimensions=[Dimension(name="k", sql="k", type="string",
                                                                             primary_key=True)])
    joined = cube(joins=[Join(name="rent__fx", relationship="many_to_one", sql="{CUBE}.currency = {rent__fx}.currency")],
                  measures=[Measure(name="billed_usd", type="sum", sql="{CUBE}.total_cents * {rent__fx.k}",
                                    filters=[Filter(sql="{CUBE}.status IN ('paid', 'open')")]),
                            Measure(name="count", type="count"),
                            Measure(name="avg_usd", type="number", sql="{billed_usd} / NULLIF({count}, 0)")],
                  segments=[Segment(name="paid", sql="{CUBE}.status = 'paid'")])
    assert problems(joined, fx) == []


@pytest.mark.parametrize("sql", [
    "{__import__('os').system('id')}",           # Cube evaluates sql fields as Python f-strings
    "{CUBE}.a + {1 + 1}",
    "{CUBE.a.b.c}",
    "{CUBE}.a }",                                  # a stray brace
])
def test_braces_may_only_hold_a_reference(sql):
    assert problems(cube(dimensions=[KEY, Dimension(name="x", sql=sql, type="string")]))


@pytest.mark.parametrize("text", ["{{ 7 * 7 }}", "{% for x in y %}", "ok }} there", "%}"])
def test_nothing_may_look_like_jinja_even_in_a_description(text):
    """Cube renders a whole .yml file as a Jinja template if any of it looks like one."""
    assert any("{{" in reason for reason in problems(cube(description=text)))


def test_render_refuses_jinja_whatever_the_caller_checked():
    with pytest.raises(SqlRejected):
        render_checked(DataModel(cubes=[cube(title="{{ secret }}")]))


@pytest.mark.parametrize("sql, expected", [
    ("SELECT * FROM rent.app.invoices WHERE id IN (SELECT id FROM rent.app.tenants)", "subqueries"),
    ("SELECT tenant_id, sum(total) AS total FROM rent.app.invoices GROUP BY 1", "one row per row"),
    ("SELECT *, row_number() OVER () AS n FROM rent.app.invoices", "window"),
    ("SELECT * FROM rent.app.invoices LIMIT 10", "LIMIT"),
    ("SELECT * FROM other.app.secrets", "not a connected source"),
    ("SELECT * FROM TABLE(rent.system.query(query => 'select 1'))", "not allowed"),
    ("DELETE FROM rent.app.invoices", "only SELECT"),
    ("SELECT * FROM rent.app.invoices; SELECT 1", "exactly one statement"),
    ("SELECT * FROM rent.app.invoices WHERE x = '{CUBE}'", "braces"),
])
def test_a_cubes_own_sql_is_one_plain_select_over_this_source(sql, expected):
    reasons = problems(cube(sql_table=None, sql=sql))
    assert any(expected in reason for reason in reasons), reasons


def test_a_cube_may_drop_rows_by_joining_another_table():
    sql = ("SELECT i.* FROM rent.app.invoices i JOIN rent.app.tenants t ON t.tenant_id = i.tenant_id "
           "WHERE t.is_demo = false")
    assert problems(cube(sql_table=None, sql=sql)) == []


@pytest.mark.parametrize("sql", [
    "(SELECT max(x) FROM rent.app.tenants)",
    "pg_sleep(10)",
    "rent.app.fn({CUBE}.a)",
    "sum({CUBE}.a) OVER ()",
])
def test_member_expressions_are_row_level_and_name_no_tables(sql):
    assert problems(cube(dimensions=[KEY, Dimension(name="x", sql=sql, type="number")]))


def test_only_number_measures_aggregate():
    assert problems(cube(measures=[Measure(name="m", type="sum", sql="sum({CUBE}.a)")]))
    assert problems(cube(measures=[Measure(name="m", type="number", sql="sum({CUBE}.a) / count(*)")])) == []


def test_references_must_name_real_cubes_and_members():
    assert problems(cube(dimensions=[KEY, Dimension(name="x", sql="{rent__ghost.a}", type="number")]))
    assert problems(cube(measures=[Measure(name="m", type="number", sql="{missing} / 2")]))


def test_names_are_prefixed_with_the_catalog_and_unique_across_sources():
    assert problems(cube(name="invoices"))
    assert problems(cube(), taken={"rent__invoices"})


def test_nova_member_names_are_reserved():
    assert problems(cube(dimensions=[KEY, Dimension(name=TENANT_DIMENSION, sql="'1'", type="string")]))


def test_every_cube_needs_a_primary_key_and_a_real_table():
    assert problems(cube(dimensions=[]))
    assert problems(cube(sql_table="rent.app.nope"))


# --- Tenancy ----------------------------------------------------------------------------------

def profile() -> SourceProfile:
    def table(name: str, *columns: str) -> TableProfile:
        return TableProfile(schema_name="app", name=name,
                            columns=[ColumnProfile(name=c, type="integer", nullable=False) for c in columns])
    return SourceProfile(source_slug="rent", catalog=CATALOG, dialect="postgres", fingerprint="f",
                         profiled_at="2026-11-09T00:00:00Z",
                         tables=[table("invoices", "invoice_id", "tenant_id"), table("tenants", "tenant_id"),
                                 table("fx", "k"), table("notes", "id")])


def model_of(*cubes: Cube) -> DataModel:
    return DataModel(cubes=list(cubes))


def test_tenant_cubes_get_the_hidden_dimension_and_shared_cubes_are_marked():
    fx = Cube(name="rent__fx", sql_table="rent.app.fx", dimensions=[Dimension(name="k", sql="k", type="string",
                                                                             primary_key=True)])
    notes = Cube(name="rent__notes", sql_table="rent.app.notes", dimensions=[KEY])
    scoped, manifest = apply_tenancy(model_of(cube(), fx, notes), catalog=CATALOG, profile=profile(),
                                     tenancy=Tenancy(column="tenant_id", shared_tables=["app.fx"]))

    tenant = scoped.cube("rent__invoices").member(TENANT_DIMENSION)
    assert tenant.sql == "CAST({CUBE}.tenant_id AS varchar)" and tenant.public is False
    assert manifest["cubes"] == {"rent__invoices": {"tenant_dimension": TENANT_DIMENSION},
                                 "rent__fx": {"shared": True}, "rent__notes": {}}
    assert unlimitable(manifest, multi_tenant=True) == ["rent__notes"]


def test_a_cube_reading_an_unscoped_table_cant_be_limited():
    sql = "SELECT i.* FROM rent.app.invoices i JOIN rent.app.notes n ON n.id = i.invoice_id"
    _, manifest = apply_tenancy(model_of(cube(sql_table=None, sql=sql)), catalog=CATALOG, profile=profile(),
                                tenancy=Tenancy(column="tenant_id"))
    assert manifest["cubes"]["rent__invoices"] == {}


def test_without_tenancy_no_cube_is_limited_and_nothing_is_added():
    scoped, manifest = apply_tenancy(model_of(cube()), catalog=CATALOG, profile=profile(), tenancy=Tenancy())
    assert scoped.cube("rent__invoices").member(TENANT_DIMENSION) is None
    assert manifest["cubes"] == {"rent__invoices": {}}


def test_a_hostile_tenant_column_is_refused():
    with pytest.raises(SqlRejected):
        apply_tenancy(model_of(cube()), catalog=CATALOG, profile=profile(),
                      tenancy=Tenancy(column="tenant_id) OR (1=1"))


def test_a_cubes_own_row_table_comes_first():
    sql = "SELECT i.* FROM rent.app.invoices i JOIN rent.app.tenants t ON t.tenant_id = i.tenant_id"
    assert tables_of(cube(sql_table=None, sql=sql), CATALOG) == ["rent.app.invoices", "rent.app.tenants"]


# --- Queries, joins and pruning ---------------------------------------------------------------

def test_a_query_becomes_cubes_format():
    query = SemanticQuery(measures=["c.m"], time_dimensions=[TimeDimension(dimension="c.t", granularity="month",
                                                                          date_range=["2026-01-01", "2026-06-30"])],
                          filters=[QueryFilter(member="c.s", operator="equals", values=["paid"])], limit=10)
    assert query.to_cube() == {"measures": ["c.m"], "dimensions": [],
                               "timeDimensions": [{"dimension": "c.t", "granularity": "month",
                                                   "dateRange": ["2026-01-01", "2026-06-30"]}],
                               "filters": [{"member": "c.s", "operator": "equals", "values": ["paid"]}], "limit": 10}


def test_a_query_must_ask_for_something():
    with pytest.raises(ValueError):
        SemanticQuery(filters=[QueryFilter(member="c.s", operator="set")])


def test_join_conditions_are_rendered_over_aliases_for_the_fan_out_check():
    fx = Cube(name="rent__fx", sql_table="rent.app.fx",
              dimensions=[Dimension(name="currency", sql="{CUBE}.code", type="string", primary_key=True)])
    sql = "{CUBE}.currency = {rent__fx.currency} AND {rent__fx}.month = {CUBE}.month"
    assert _join_condition(sql, cube(), fx) == "a.currency = b.code AND b.month = a.month"
    calculated = fx.model_copy(update={"dimensions": [Dimension(name="currency", sql="upper(code)", type="string")]})
    assert _join_condition(sql, cube(), calculated) is None


def test_pruning_drops_exactly_what_failed():
    joined = cube(joins=[Join(name="rent__fx", relationship="many_to_one", sql="1 = 1")],
                  measures=[Measure(name="good", type="count"), Measure(name="bad", type="count")])
    other = Cube(name="rent__notes", sql_table="rent.app.notes", dimensions=[KEY])
    pruned, notes = prune(model_of(joined, other), [
        Problem(item="m", reason="r", cube="rent__invoices", member="bad"),
        Problem(item="j", reason="r", cube="rent__invoices", join="rent__fx"),
        Problem(item="c", reason="r", cube="rent__notes"),
    ])
    assert [m.name for m in pruned.cube("rent__invoices").measures] == ["good"]
    assert pruned.cube("rent__invoices").joins == []
    assert pruned.cube("rent__notes") is None
    assert len(notes) == 3


def test_only_counts_and_sums_are_marked_as_adding_up_to_a_total():
    from agent.semantic.describe import annotate

    model = DataModel(cubes=[cube(measures=[Measure(name="billed", type="sum", sql="{CUBE}.total"),
                                            Measure(name="count", type="count"),
                                            Measure(name="customers", type="count_distinct", sql="{CUBE}.customer_id"),
                                            Measure(name="average", type="avg", sql="{CUBE}.total")])])
    notes = {name: {"title": name, "type": "number"} for name in
             ("rent__invoices.billed", "rent__invoices.count", "rent__invoices.customers", "rent__invoices.average",
              "elsewhere.unknown")}
    marked = annotate({"measures": notes, "dimensions": {"rent__invoices.id": {}}}, model)
    assert {name: note["additive"] for name, note in marked["measures"].items()} == {
        "rent__invoices.billed": True, "rent__invoices.count": True, "rent__invoices.customers": False,
        "rent__invoices.average": False, "elsewhere.unknown": False}
    assert marked["measures"]["rent__invoices.billed"]["title"] == "rent__invoices.billed"
    assert marked["dimensions"] == {"rent__invoices.id": {}}
