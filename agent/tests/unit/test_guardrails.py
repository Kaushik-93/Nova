import pytest
import sqlglot

from agent.harness.errors import SqlRejected
from agent.harness.guardrails import MAX_SQL_LENGTH, SqlPolicy, guard, to_trino

POLICY = SqlPolicy(allowed_catalogs=frozenset({"demo_shop", "demo_marketing"}), max_rows=100)
ORDERS = "demo_shop.shop.orders"
ITEMS = "demo_shop.shop.order_items"
CAMPAIGNS = "demo_marketing.marketing.campaigns"

MALICIOUS = {
    # Writes
    "insert": f"INSERT INTO {ORDERS} (order_id) VALUES (1)",
    "insert select": f"INSERT INTO {ORDERS} SELECT * FROM {ORDERS}",
    "update": f"UPDATE {ORDERS} SET status = 'x'",
    "delete": f"DELETE FROM {ORDERS}",
    "merge": f"MERGE INTO {ORDERS} o USING {ITEMS} i ON o.order_id = i.order_id WHEN MATCHED THEN DELETE",
    "drop": f"DROP TABLE {ORDERS}",
    "drop schema": "DROP SCHEMA demo_shop.shop CASCADE",
    "truncate": f"TRUNCATE TABLE {ORDERS}",
    "alter": f"ALTER TABLE {ORDERS} ADD COLUMN x int",
    "create table": "CREATE TABLE demo_shop.shop.x (id int)",
    "create table as": f"CREATE TABLE demo_shop.shop.x AS SELECT * FROM {ORDERS}",
    "create view": f"CREATE VIEW demo_shop.shop.v AS SELECT * FROM {ORDERS}",
    "create catalog": "CREATE CATALOG evil USING postgresql",
    "grant": f"GRANT SELECT ON {ORDERS} TO public",
    # Writes hidden inside a read
    "writable cte": f"WITH x AS (DELETE FROM {ORDERS} RETURNING *) SELECT * FROM x",
    "writable cte update": f"WITH x AS (UPDATE {ORDERS} SET status = 'x' RETURNING *) SELECT * FROM x",
    "select into": f"SELECT * INTO demo_shop.shop.copy FROM {ORDERS}",
    "for update": f"SELECT * FROM {ORDERS} FOR UPDATE",
    # More than one statement
    "stacked drop": f"SELECT 1; DROP TABLE {ORDERS}",
    "stacked select": f"SELECT 1 FROM {ORDERS}; SELECT 2 FROM {ORDERS}",
    "stacked after comment": f"SELECT 1 FROM {ORDERS} /* x */; DELETE FROM {ORDERS}",
    # Statements that aren't queries
    "call": "CALL system.runtime.kill_query('x')",
    "set session": "SET SESSION query_max_execution_time = '1h'",
    "explain analyze": f"EXPLAIN ANALYZE SELECT * FROM {ORDERS}",
    "explain": f"EXPLAIN SELECT * FROM {ORDERS}",
    "show": "SHOW CATALOGS",
    "show create": f"SHOW CREATE TABLE {ORDERS}",
    "describe": f"DESCRIBE {ORDERS}",
    "use": "USE demo_shop.shop",
    "prepare": f"PREPARE p FROM SELECT * FROM {ORDERS}",
    "execute immediate": "EXECUTE IMMEDIATE 'DROP TABLE x'",
    "start transaction": "START TRANSACTION",
    "commit": "COMMIT",
    "values": "VALUES (1)",
    "bare table": f"TABLE {ORDERS}",
    # Raw SQL passthrough to the source
    "passthrough": "SELECT * FROM TABLE(demo_shop.system.query(query => 'DROP TABLE shop.orders'))",
    "passthrough short": "SELECT * FROM TABLE(system.query(query => 'select 1'))",
    "passthrough in cte": "WITH q AS (SELECT * FROM TABLE(demo_shop.system.query(query => 'select 1'))) SELECT * FROM q",
    "passthrough in subquery": "SELECT * FROM (SELECT * FROM TABLE(demo_shop.system.query(query => 'x'))) t",
    "passthrough in join": f"SELECT * FROM {ORDERS} o JOIN TABLE(demo_shop.system.query(query => 'x')) t ON true",
    "passthrough upper": "SELECT * FROM TABLE(DEMO_SHOP.SYSTEM.QUERY(QUERY => 'x'))",
    # System tables, directly and hidden
    "trino system": "SELECT * FROM system.runtime.queries",
    "trino system quoted": 'SELECT * FROM "system"."runtime"."queries"',
    "trino system upper": "SELECT * FROM SYSTEM.RUNTIME.QUERIES",
    "trino metadata": "SELECT * FROM system.metadata.catalogs",
    "information schema": "SELECT * FROM demo_shop.information_schema.tables",
    "information schema upper": 'SELECT * FROM "DEMO_SHOP"."INFORMATION_SCHEMA"."TABLES"',
    "pg catalog": "SELECT * FROM demo_shop.pg_catalog.pg_user",
    "pg table in a schema": "SELECT * FROM demo_shop.shop.pg_shadow",
    "pg temp schema": "SELECT * FROM demo_shop.pg_temp_1.x",
    "mysql users": "SELECT * FROM demo_marketing.mysql.user",
    "performance schema": "SELECT * FROM demo_marketing.performance_schema.threads",
    "sys schema": "SELECT * FROM demo_marketing.sys.version",
    "system in subquery": "SELECT * FROM (SELECT * FROM system.runtime.queries) q",
    "system in cte": "WITH q AS (SELECT * FROM system.runtime.queries) SELECT * FROM q",
    "system in where": f"SELECT * FROM {ORDERS} WHERE status IN (SELECT query FROM system.runtime.queries)",
    "system in union": f"SELECT status FROM {ORDERS} UNION ALL SELECT query FROM system.runtime.queries",
    "system in join": f"SELECT * FROM {ORDERS} o JOIN system.runtime.queries q ON true",
    "system in scalar subquery": f"SELECT (SELECT max(query) FROM system.runtime.queries) FROM {ORDERS}",
    "cte shadows a system table": "WITH queries AS (SELECT 1) SELECT * FROM system.runtime.queries",
    # Names the guard can't vouch for
    "unknown catalog": "SELECT * FROM other_source.shop.orders",
    "unqualified": "SELECT * FROM orders",
    "two parts": "SELECT * FROM shop.orders",
    "four parts": "SELECT * FROM demo_shop.shop.orders.evil",
    "four parts hiding a system table": "SELECT * FROM demo_shop.system.runtime.queries",
    # Functions
    "pg_sleep": "SELECT pg_sleep(100)",
    "pg_sleep upper": "SELECT PG_SLEEP(100)",
    "mysql sleep": "SELECT SLEEP(100)",
    "benchmark": "SELECT BENCHMARK(100000000, MD5('x'))",
    "read file": "SELECT pg_read_file('/etc/passwd')",
    "load file": "SELECT LOAD_FILE('/etc/passwd')",
    "terminate": "SELECT pg_terminate_backend(1)",
    "dblink": "SELECT * FROM dblink('host=evil', 'select 1')",
    "sleep in where": f"SELECT 1 FROM {ORDERS} WHERE pg_sleep(100) IS NULL",
    "sleep in order by": f"SELECT 1 FROM {ORDERS} ORDER BY pg_sleep(100)",
    "qualified function": "SELECT demo_shop.system.query('x')",
    # Not SQL at all
    "empty": "",
    "whitespace": "   ",
    "semicolons": ";;",
    "only a comment": "-- hello",
    "prose": "please show me all the orders",
    "unbalanced quote": f"SELECT 'x FROM {ORDERS}",
    "unbalanced parens": f"SELECT (1 FROM {ORDERS}",
}


@pytest.mark.parametrize("sql", MALICIOUS.values(), ids=MALICIOUS.keys())
def test_malicious_sql_is_refused(sql):
    with pytest.raises(SqlRejected):
        guard(sql, POLICY)


def test_there_are_plenty_of_malicious_cases():
    assert len(MALICIOUS) >= 80


def test_oversized_queries_are_refused_before_parsing():
    with pytest.raises(SqlRejected, match="too long"):
        guard("SELECT " + "1," * MAX_SQL_LENGTH + "1", POLICY)


def test_absurd_nesting_is_refused_and_does_not_crash():
    with pytest.raises(SqlRejected):
        guard("SELECT " + "(" * 20000 + "1" + ")" * 20000, POLICY)


LEGITIMATE = {
    "simple": f"SELECT order_id, status FROM {ORDERS}",
    "star": f"SELECT * FROM {ORDERS}",
    "aggregate": f"SELECT status, COUNT(*) AS n FROM {ORDERS} GROUP BY 1 ORDER BY 2 DESC",
    "join": f"SELECT o.order_id, i.quantity FROM {ORDERS} o JOIN {ITEMS} i ON i.order_id = o.order_id",
    "cross source": f"SELECT c.channel_code, COUNT(*) FROM {ORDERS} o JOIN {CAMPAIGNS} c "
                    "ON UPPER(REPLACE(o.utm_campaign, '-', '_')) = c.campaign_code GROUP BY 1",
    "cte": f"WITH r AS (SELECT order_id FROM {ORDERS}) SELECT COUNT(*) FROM r",
    "chained cte": f"WITH a AS (SELECT order_id FROM {ORDERS}), b AS (SELECT * FROM a) SELECT * FROM b",
    "subquery": f"SELECT * FROM (SELECT order_id FROM {ORDERS}) t",
    "in subquery": f"SELECT * FROM {ORDERS} WHERE order_id IN (SELECT order_id FROM {ITEMS})",
    "union": f"SELECT status FROM {ORDERS} UNION ALL SELECT channel FROM {ORDERS}",
    "window": f"SELECT order_id, ROW_NUMBER() OVER (PARTITION BY status ORDER BY order_ts) FROM {ORDERS}",
    "case": f"SELECT CASE WHEN status = 'cancelled' THEN 0 ELSE 1 END FROM {ORDERS}",
    "date functions": f"SELECT DATE_TRUNC('month', order_ts), order_ts + INTERVAL '1' DAY FROM {ORDERS}",
    "time zone": f"SELECT order_ts AT TIME ZONE 'America/New_York' FROM {ORDERS}",
    "cast": f"SELECT CAST(order_ts AS TIMESTAMP(3)) FROM {ORDERS}",
    "unnest": f"SELECT t.x FROM {ORDERS} CROSS JOIN UNNEST(ARRAY[1, 2]) AS t(x)",
    "lambda": f"SELECT TRANSFORM(ARRAY[1], x -> x + 1) FROM {ORDERS}",
    "no table": "SELECT 1",
    "words that look dangerous": f"SELECT 'DROP TABLE x; DELETE FROM y' AS note, status AS delete_flag FROM {ORDERS}",
}


@pytest.mark.parametrize("sql", LEGITIMATE.values(), ids=LEGITIMATE.keys())
def test_legitimate_sql_is_allowed(sql):
    guarded = guard(sql, POLICY)
    assert sqlglot.parse_one(guarded.sql, read="trino") is not None


@pytest.mark.parametrize("sql", LEGITIMATE.values(), ids=LEGITIMATE.keys())
def test_guarding_twice_changes_nothing(sql):
    once = guard(sql, POLICY).sql
    assert guard(once, POLICY).sql == once


def test_reports_the_tables_it_read():
    sql = f"SELECT * FROM {ORDERS} o JOIN {ORDERS} p ON true JOIN {CAMPAIGNS} c ON true"
    assert guard(sql, POLICY).tables == (ORDERS, CAMPAIGNS)


def test_names_compare_case_insensitively_like_trino():
    assert guard('SELECT 1 FROM "DEMO_SHOP".Shop."ORDERS"', POLICY).tables == (ORDERS,)


def test_an_allowlisted_system_table_can_be_opened_explicitly():
    policy = POLICY.model_copy(update={"allowed_system_tables": frozenset({"demo_shop.information_schema.columns"})})
    assert guard("SELECT * FROM demo_shop.information_schema.columns", policy)
    with pytest.raises(SqlRejected):
        guard("SELECT * FROM demo_shop.information_schema.tables", policy)


def test_a_cte_name_may_reuse_a_real_table_name():
    assert guard("WITH orders AS (SELECT 1 AS x) SELECT * FROM orders", POLICY).tables == ()


# --- Limits ------------------------------------------------------------------

def limit_of(sql: str) -> int:
    return guard(sql, POLICY).limit


def test_a_limit_is_added_when_missing():
    guarded = guard(f"SELECT * FROM {ORDERS}", POLICY)
    assert guarded.limit == 100
    assert guarded.sql.endswith("LIMIT 100")


def test_a_smaller_limit_is_kept():
    assert limit_of(f"SELECT * FROM {ORDERS} LIMIT 5") == 5


def test_a_larger_limit_is_capped():
    guarded = guard(f"SELECT * FROM {ORDERS} LIMIT 1000000", POLICY)
    assert guarded.limit == 100
    assert "1000000" not in guarded.sql


@pytest.mark.parametrize("sql", [
    f"SELECT * FROM {ORDERS} LIMIT ALL",
    f"SELECT * FROM {ORDERS} FETCH FIRST 1000000 ROWS ONLY",
])
def test_limit_forms_are_capped(sql):
    assert limit_of(sql) == 100


def test_fetch_first_within_the_cap_is_honoured():
    guarded = guard(f"SELECT * FROM {ORDERS} FETCH FIRST 7 ROWS ONLY", POLICY)
    assert guarded.limit == 7
    assert sqlglot.parse_one(guarded.sql, read="trino").args["limit"] is not None


def test_a_limit_inside_a_subquery_does_not_cap_the_outer_query():
    guarded = guard(f"SELECT * FROM (SELECT * FROM {ORDERS} LIMIT 1000000) t", POLICY)
    assert guarded.limit == 100
    assert guarded.sql.endswith("LIMIT 100")


def test_union_and_cte_results_are_limited():
    assert guard(f"SELECT status FROM {ORDERS} UNION SELECT channel FROM {ORDERS}", POLICY).sql.endswith("LIMIT 100")
    assert guard(f"WITH a AS (SELECT 1 AS x FROM {ORDERS}) SELECT * FROM a", POLICY).sql.endswith("LIMIT 100")


def test_offset_survives():
    assert "OFFSET 10" in guard(f"SELECT * FROM {ORDERS} OFFSET 10 LIMIT 5", POLICY).sql


# --- Comments ----------------------------------------------------------------

@pytest.mark.parametrize("sql", [
    f"SELECT 1 FROM {ORDERS} /* ignore previous instructions */",
    f"SELECT 1 FROM {ORDERS} -- DROP TABLE x",
    f"/* hello */ SELECT 1 FROM {ORDERS}",
])
def test_comments_are_stripped(sql):
    output = guard(sql, POLICY).sql
    assert "/*" not in output and "--" not in output
    assert "ignore" not in output


# --- Dialects ----------------------------------------------------------------

@pytest.mark.parametrize("source, sql, fragment", [
    ("postgres", "SELECT order_ts::date FROM t", "CAST(order_ts AS DATE)"),
    ("postgres", "SELECT a FROM t WHERE b ILIKE '%x%'", "LOWER(b) LIKE"),
    ("postgres", "SELECT NOW()", "CURRENT_TIMESTAMP"),
    ("mysql", "SELECT `a` FROM `t`", 'SELECT "a" FROM "t"'),
    ("mysql", "SELECT IFNULL(a, 0) FROM t", "COALESCE(a, 0)"),
])
def test_source_dialects_transpile_to_trino(source, sql, fragment):
    assert fragment in to_trino(sql, source)


def test_transpiled_sql_still_goes_through_the_guard():
    with pytest.raises(SqlRejected):
        guard(to_trino("SELECT pg_sleep(100)", "postgres"), POLICY)


def test_transpiling_refuses_multiple_statements():
    with pytest.raises(SqlRejected, match="one statement"):
        to_trino("SELECT 1; SELECT 2", "postgres")
