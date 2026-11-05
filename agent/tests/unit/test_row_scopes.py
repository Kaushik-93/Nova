"""Per-tenant security in the SQL layer. These run the guard's output through sqlglot, and the
real proof against Trino is in tests/integration/test_tenant_security.py."""

import pytest
from sqlglot import exp, parse_one

from agent.harness.errors import SqlRejected
from agent.harness.guardrails import RowScope, SqlPolicy, guard, scope_clause

ORDERS = "demo_rental.rental.invoices"
VEHICLES = "demo_rental.rental.vehicles"
LINES = "demo_rental.rental.invoice_lines"
FX = "demo_rental.rental.fx_rates"
SCOPE = RowScope(column="tenant_id", values=("7",), numeric=True, scoped_tables=frozenset({ORDERS, VEHICLES}),
                 shared_tables=frozenset({FX}))
POLICY = SqlPolicy(allowed_catalogs=frozenset({"demo_rental", "demo_shop"}), row_scopes={"demo_rental": SCOPE}, max_rows=100)


def scoped(sql: str) -> str:
    return guard(sql, POLICY).sql


def every_scoped_read_is_filtered(sql: str) -> bool:
    """Each read of a scoped table sits directly inside a SELECT with the tenant condition."""
    tree = parse_one(sql, read="trino")
    for table in tree.find_all(exp.Table):
        qualified = f"{table.catalog}.{table.db}.{table.name}"
        if qualified not in SCOPE.scoped_tables:
            continue
        select = table.find_ancestor(exp.Select)
        where = select.args.get("where") if select else None
        if where is None or "tenant_id IN (7)" not in where.sql(dialect="trino"):
            return False
    return True


@pytest.mark.parametrize("sql", [
    f"SELECT * FROM {ORDERS}",
    f"SELECT * FROM {ORDERS} AS i",
    f"SELECT * FROM {ORDERS} i JOIN {VEHICLES} v ON v.tenant_id = i.tenant_id",
    f"SELECT * FROM {ORDERS}, {VEHICLES}",
    f"SELECT * FROM {ORDERS} a JOIN {ORDERS} b ON a.invoice_id = b.invoice_id",
    f"WITH x AS (SELECT * FROM {ORDERS}) SELECT * FROM x",
    f"WITH x AS (SELECT * FROM {ORDERS}), y AS (SELECT * FROM x JOIN {VEHICLES} v ON true) SELECT * FROM y",
    f"SELECT * FROM (SELECT * FROM {ORDERS}) t",
    f"SELECT (SELECT COUNT(*) FROM {ORDERS}) AS n",
    f"SELECT 1 WHERE EXISTS (SELECT 1 FROM {ORDERS})",
    f"SELECT 1 FROM {VEHICLES} WHERE tenant_id IN (SELECT tenant_id FROM {ORDERS})",
    f"SELECT tenant_id FROM {ORDERS} UNION ALL SELECT tenant_id FROM {VEHICLES}",
], ids=["plain", "alias", "join", "comma join", "self join", "cte", "chained cte", "subquery", "scalar subquery", "exists",
        "in subquery", "union"])
def test_every_way_of_reading_a_table_is_scoped(sql):
    assert every_scoped_read_is_filtered(scoped(sql))


@pytest.mark.parametrize("attack", [
    f"SELECT * FROM {ORDERS} WHERE tenant_id = 7 OR 1 = 1",
    f"SELECT * FROM {ORDERS} WHERE 1 = 1 OR tenant_id <> 7",
    f"SELECT * FROM {ORDERS} WHERE tenant_id = 8",
    f"SELECT * FROM {ORDERS} WHERE tenant_id IN (1, 2, 3, 4, 5, 6, 8)",
    f"SELECT * FROM {ORDERS} /* tenant_id IN (7) */ WHERE 1 = 1 --",
], ids=["or true", "or inverse", "another tenant", "many tenants", "comment"])
def test_a_where_clause_cannot_widen_the_scope(attack):
    # The model's own WHERE sits outside the subquery, so it can only narrow the rows.
    output = scoped(attack)
    assert every_scoped_read_is_filtered(output)
    inner = parse_one(output, read="trino").find(exp.Subquery)
    assert "tenant_id IN (7)" in inner.sql(dialect="trino")


def test_a_cte_cannot_impersonate_a_scoped_table():
    output = scoped(f"WITH invoices AS (SELECT * FROM {ORDERS}) SELECT * FROM invoices")
    assert every_scoped_read_is_filtered(output)


def test_a_table_that_cannot_be_scoped_is_refused():
    with pytest.raises(SqlRejected, match="can't be limited to your data"):
        scoped(f"SELECT * FROM {LINES}")


@pytest.mark.parametrize("sql", [
    f"SELECT * FROM {ORDERS} i JOIN {LINES} l ON l.invoice_id = i.invoice_id",
    f"WITH x AS (SELECT * FROM {LINES}) SELECT * FROM x",
    f"SELECT * FROM (SELECT * FROM {LINES}) t",
], ids=["join", "cte", "subquery"])
def test_an_unscopable_table_is_refused_however_it_is_reached(sql):
    with pytest.raises(SqlRejected):
        scoped(sql)


def test_a_shared_table_stays_readable():
    assert FX in scoped(f"SELECT * FROM {FX}") or "fx_rates" in scoped(f"SELECT * FROM {FX}")
    assert "tenant_id IN" not in scoped(f"SELECT * FROM {FX}")


def test_aliases_still_work_after_the_rewrite():
    output = scoped(f"SELECT i.status, v.make FROM {ORDERS} i JOIN {VEHICLES} v ON v.tenant_id = i.tenant_id")
    assert "AS i" in output and "AS v" in output


def test_an_unaliased_table_keeps_its_name():
    assert "AS invoices" in scoped(f"SELECT invoices.status FROM {ORDERS}")


def test_other_catalogs_are_left_alone():
    assert "tenant_id" not in scoped("SELECT * FROM demo_shop.shop.orders")


def test_a_scoped_catalog_needs_a_scope_for_every_table():
    bare = SqlPolicy(allowed_catalogs=frozenset({"demo_rental"}), row_scopes={"demo_rental": RowScope(column="tenant_id", values=("7",), numeric=True)})
    with pytest.raises(SqlRejected):
        guard(f"SELECT * FROM {ORDERS}", bare)


def test_case_is_ignored_like_trino_does():
    output = scoped('SELECT * FROM "DEMO_RENTAL"."RENTAL"."INVOICES"')
    assert "tenant_id IN (7)" in output


def test_no_scope_means_no_rewrite():
    open_policy = SqlPolicy(allowed_catalogs=frozenset({"demo_rental"}))
    assert "tenant_id IN" not in guard(f"SELECT * FROM {LINES}", open_policy).sql


def test_a_scoped_dataset_keeps_its_scope_without_a_limit():
    output = guard(f"SELECT * FROM {ORDERS}", POLICY, add_limit=False).sql
    assert "tenant_id IN (7)" in output and "LIMIT" not in output


# --- The clause itself ---------------------------------------------------------

def test_numeric_values_are_numbers():
    assert scope_clause("tenant_id", ("1", "2"), numeric=True) == "tenant_id IN (1, 2)"


def test_text_values_are_quoted():
    assert scope_clause("tenant_name", ("Harbor City",), numeric=False) == "tenant_name IN ('Harbor City')"


@pytest.mark.parametrize("value", ["O'Brien", "x' OR '1'='1", "'; DROP TABLE t; --"])
def test_quotes_in_values_cannot_break_out(value):
    clause = scope_clause("tenant_name", (value,), numeric=False)
    tree = parse_one(f"SELECT 1 FROM t WHERE {clause}", read="trino")
    assert [literal.this for literal in tree.find_all(exp.Literal) if literal.is_string] == [value]
    assert not tree.find(exp.Or)


@pytest.mark.parametrize("value", ["1 OR 1=1", "1; DROP TABLE t", "abc"])
def test_a_numeric_scope_refuses_anything_but_a_number(value):
    with pytest.raises(ValueError):
        scope_clause("tenant_id", (value,), numeric=True)


@pytest.mark.parametrize("column", ["tenant_id OR 1=1", "a b", "x;y", "", '"quoted"'])
def test_a_column_must_be_a_plain_identifier(column):
    with pytest.raises(SqlRejected):
        scope_clause(column, ("1",), numeric=True)


def test_an_empty_scope_matches_nothing():
    assert scope_clause("tenant_id", (), numeric=True) == "1 = 0"
