"""The evals rely on closed periods never changing, so that is tested directly."""

from datetime import date, timedelta

from seed.generate import (FIRST_DAY, INJECTED_DESCRIPTION, INJECTED_PRODUCT_ID, UNMATCHED_UTMS, generate, utm_for)
from tests.conftest import revenue_cents

EARLY_END = date(2026, 6, 30)
LATE_END = date(2026, 9, 30)
# Refunds can land up to 22 days after the order, and statuses settle after 5.
CLOSED = EARLY_END - timedelta(days=30)


def test_generation_is_repeatable():
    first, second = generate(EARLY_END), generate(EARLY_END)
    assert first.orders == second.orders
    assert first.order_items == second.order_items
    assert first.ad_spend == second.ad_spend


def test_closed_periods_do_not_change_when_seeding_later():
    early, late = generate(EARLY_END), generate(LATE_END)
    closed_ids = {o[0] for o in early.orders if o[4].date() <= CLOSED}

    assert [o for o in early.orders if o[0] in closed_ids] == [o for o in late.orders if o[0] in closed_ids]
    assert [i for i in early.order_items if i[1] in closed_ids] == [i for i in late.order_items if i[1] in closed_ids]
    assert ([s for s in early.ad_spend if s[1] <= CLOSED] == [s for s in late.ad_spend if s[1] <= CLOSED])
    assert revenue_cents(early, FIRST_DAY, CLOSED) == revenue_cents(late, FIRST_DAY, CLOSED)


def test_data_reaches_the_end_day():
    data = generate(EARLY_END)
    assert max(o[4].date() for o in data.orders) == EARLY_END
    assert max(s[1] for s in data.ad_spend) == EARLY_END


def test_recent_orders_are_still_in_flight():
    data = generate(EARLY_END)
    last_day = [o for o in data.orders if o[4].date() == EARLY_END]
    assert {o[5] for o in last_day} <= {"paid", "cancelled"}


def test_every_documented_trap_is_present():
    data = generate(EARLY_END)
    product = next(p for p in data.products if p[0] == INJECTED_PRODUCT_ID)

    assert product[4] == INJECTED_DESCRIPTION
    assert any(o[11] is not None for o in data.orders), "soft deletes"
    assert any(c[7] for c in data.customers), "test customers"
    assert any(o[7] in UNMATCHED_UTMS for o in data.orders), "unmatched attribution tags"
    assert len({c[1] for c in data.customers}) < len(data.customers), "duplicate emails"
    assert any(s[3] == 2 for s in data.ad_spend), "restated spend rows"
    assert any(0 < r[3] for r in data.refunds), "refunds"
    partially_refunded = {r[1] for r in data.refunds}
    statuses = {o[5] for o in data.orders if o[0] in partially_refunded}
    assert "delivered" in statuses, "partial refunds leave the status as delivered"


def test_test_orders_really_would_distort_revenue():
    data = generate(EARLY_END)
    test_customers = {c[0] for c in data.customers if c[7]}
    all_orders = {o[0] for o in data.orders if o[5] != "cancelled" and o[11] is None}
    with_test_orders = sum(q * p for _, oid, _, q, p in data.order_items if oid in all_orders)
    without = revenue_cents(data, FIRST_DAY, EARLY_END)
    assert with_test_orders - without > without * 0.02
    assert test_customers


def test_campaign_codes_need_a_format_transformation_to_join():
    data = generate(EARLY_END)
    codes = {c[1] for c in data.campaigns}
    tagged = [o[7] for o in data.orders if o[7]]

    assert not codes & set(tagged), "raw values must not match without transforming"
    transformed = sum(1 for utm in tagged if utm.upper().replace("-", "_") in codes)
    assert 0.9 < transformed / len(tagged) < 1.0
    assert all(utm_for(code).upper().replace("-", "_") == code for code in codes)
