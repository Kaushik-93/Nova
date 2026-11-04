"""The rental platform data: the generator, and what's loaded."""

import collections
import os
from datetime import date, timedelta

import psycopg
import pytest

from seed.rental_generate import INJECTED_NOTE, generate

EARLY_END = date(2026, 6, 30)
LATE_END = date(2026, 9, 30)
# Rentals last up to 21 days, and payment and repairs settle within about 90.
CLOSED = EARLY_END - timedelta(days=120)


@pytest.fixture(scope="module")
def data():
    return generate(EARLY_END)


def test_generation_is_repeatable(data):
    again = generate(EARLY_END)
    assert data.agreements == again.agreements
    assert data.invoices == again.invoices
    assert data.damages == again.damages


def test_closed_periods_do_not_change_when_seeding_later(data):
    later = generate(LATE_END)
    keep = lambda rows, index: [r for r in rows if r[index].date() <= CLOSED]  # noqa: E731
    assert keep(data.agreements, 8) == keep(later.agreements, 8)
    closed_invoices = lambda rows: [r for r in rows if r[6] <= CLOSED]  # noqa: E731
    assert closed_invoices(data.invoices) == closed_invoices(later.invoices)
    assert [d for d in data.damages if d[4] <= CLOSED and d[11]] == [d for d in later.damages if d[4] <= CLOSED and d[11]]


def test_there_are_many_tenants_of_very_different_sizes(data):
    assert len(data.tenants) >= 30
    sizes = collections.Counter(a[1] for a in data.agreements)
    assert max(sizes.values()) > 10 * min(sizes.values())


def test_every_tenant_has_vehicles_agreements_invoices_and_customers(data):
    for rows, index in ((data.vehicles, 1), (data.agreements, 1), (data.invoices, 1), (data.customers, 1)):
        assert {row[index] for row in rows} == {tenant[0] for tenant in data.tenants}


def test_a_vehicle_is_never_rented_twice_at_once(data):
    by_vehicle = collections.defaultdict(list)
    for agreement in data.agreements:
        if agreement[11] in ("completed", "active"):
            by_vehicle[agreement[3]].append((agreement[8], agreement[10] or agreement[9]))
    for periods in by_vehicle.values():
        periods.sort()
        assert all(earlier[1] <= later[0] for earlier, later in zip(periods, periods[1:]))


def test_an_agreement_stays_within_its_tenant(data):
    vehicle_tenant = {v[0]: v[1] for v in data.vehicles}
    customer_tenant = {c[0]: c[1] for c in data.customers}
    agreement_tenant = {a[0]: a[1] for a in data.agreements}
    for agreement in data.agreements:
        assert vehicle_tenant[agreement[3]] == agreement[1]
        assert customer_tenant[agreement[4]] == agreement[1]
    assert all(agreement_tenant[i[2]] == i[1] for i in data.invoices)
    assert all(d[3] is None or agreement_tenant[d[3]] == d[1] for d in data.damages)


def test_invoices_add_up(data):
    lines = collections.Counter()
    for line in data.invoice_lines:
        lines[line[1]] += line[6]
    for invoice in data.invoices:
        assert lines[invoice[0]] == invoice[11]
        assert invoice[11] + invoice[12] == invoice[13]


def test_no_tenant_acts_after_it_left(data):
    left = {t[0]: t[8] for t in data.tenants if t[8]}
    assert left
    assert all(a[8].date() <= left[a[1]] for a in data.agreements if a[1] in left)


def test_every_documented_trap_is_present(data):
    currencies = {t[3] for t in data.tenants}
    assert currencies >= {"USD", "EUR", "GBP", "CAD"}
    assert any(t[9] for t in data.tenants), "demo tenants"
    assert any(i[9] == "void" for i in data.invoices), "void invoices"
    assert any(i[4] == "credit_note" and i[13] < 0 for i in data.invoices), "credit notes"
    per_agreement = collections.Counter(i[2] for i in data.invoices)
    assert any(count > 1 for count in per_agreement.values()), "several invoices per rental"
    assert any(a[16] is not None for a in data.agreements), "soft deletes"
    assert {a[11] for a in data.agreements} >= {"completed", "cancelled", "no_show", "active"}
    fleet = collections.Counter(v[3] for v in data.vehicles)
    assert any(count > 1 for count in fleet.values()), "fleet numbers repeat across tenants"
    assert any(d[3] is None for d in data.damages), "damage with no rental"
    assert any(d[10] is None for d in data.damages), "damage still being repaired"
    assert any(d[15] == INJECTED_NOTE for d in data.damages), "prompt injection fixture"


def test_demo_tenants_would_visibly_distort_revenue(data):
    demo = {t[0] for t in data.tenants if t[9]}
    fake = sum(i[11] for i in data.invoices if i[1] in demo)
    real = sum(i[11] for i in data.invoices if i[1] not in demo)
    assert fake > real * 0.3


# --- What's loaded -------------------------------------------------------------

def rental(user="nova_ro"):
    secret = "DEMO_RO_PASSWORD" if user == "nova_ro" else "DEMO_OWNER_PASSWORD"
    return psycopg.connect(host="demo-postgres", dbname="rental", user=user, password=os.environ[secret])


@pytest.fixture(scope="module")
def loaded():
    with rental("shop_owner") as conn:
        return generate(conn.execute("SELECT end_day FROM _seed.meta").fetchone()[0])


@pytest.mark.parametrize("table, attribute", [
    ("tenants", "tenants"), ("vehicles", "vehicles"), ("rental_agreements", "agreements"), ("invoices", "invoices"),
    ("invoice_lines", "invoice_lines"), ("vehicle_damages", "damages"), ("fx_rates", "fx_rates"),
])
def test_tables_match_the_generator(loaded, table, attribute):
    with rental() as conn:
        assert conn.execute(f"SELECT COUNT(*) FROM rental.{table}").fetchone()[0] == len(getattr(loaded, attribute))


@pytest.mark.parametrize("statement", [
    "INSERT INTO rental.tenants (tenant_id) VALUES (999)",
    "UPDATE rental.invoices SET status = 'void'",
    "DELETE FROM rental.vehicle_damages",
    "DROP TABLE rental.invoices",
])
def test_the_read_only_user_cannot_write(statement):
    with rental() as conn, pytest.raises(psycopg.errors.Error):
        conn.execute(statement)


def test_trino_reads_each_tenants_invoices(trino_cursor, loaded):
    trino_cursor.execute("""
        SELECT t.tenant_name, COUNT(*) AS invoices
        FROM demo_rental.rental.invoices i JOIN demo_rental.rental.tenants t ON t.tenant_id = i.tenant_id
        GROUP BY 1""")
    assert len(trino_cursor.fetchall()) == len(loaded.tenants)
