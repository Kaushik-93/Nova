-- A multi-tenant vehicle rental platform. Each tenant is a rental company.
-- Money is integer cents in the tenant's own currency; timestamps are UTC.
DROP SCHEMA IF EXISTS rental CASCADE;
CREATE SCHEMA rental;

CREATE TABLE rental.tenants (
    tenant_id    integer PRIMARY KEY,
    tenant_name  text NOT NULL UNIQUE,
    country_code char(2) NOT NULL,
    currency     char(3) NOT NULL,
    timezone     text NOT NULL,
    plan         text NOT NULL,
    status       text NOT NULL,
    onboarded_on date NOT NULL,
    offboarded_on date,
    is_demo      boolean NOT NULL DEFAULT false
);

CREATE TABLE rental.branches (
    branch_id   integer PRIMARY KEY,
    tenant_id   integer NOT NULL REFERENCES rental.tenants,
    branch_name text NOT NULL,
    city        text NOT NULL,
    branch_type text NOT NULL
);

CREATE TABLE rental.vehicles (
    vehicle_id       integer PRIMARY KEY,
    tenant_id        integer NOT NULL REFERENCES rental.tenants,
    home_branch_id   integer NOT NULL REFERENCES rental.branches,
    fleet_number     text NOT NULL,          -- unique only within a tenant
    plate            text NOT NULL UNIQUE,
    make             text NOT NULL,
    model            text NOT NULL,
    model_year       integer NOT NULL,
    vehicle_class    text NOT NULL,
    daily_rate_cents integer NOT NULL,
    acquired_on      date NOT NULL,
    retired_on       date,
    status           text NOT NULL
);

CREATE TABLE rental.customers (
    customer_id  integer PRIMARY KEY,
    tenant_id    integer NOT NULL REFERENCES rental.tenants,
    full_name    text NOT NULL,
    email        text NOT NULL,
    is_corporate boolean NOT NULL,
    created_on   date NOT NULL
);

CREATE TABLE rental.rental_agreements (
    agreement_id      bigint PRIMARY KEY,
    tenant_id         integer NOT NULL REFERENCES rental.tenants,
    agreement_number  text NOT NULL UNIQUE,
    vehicle_id        integer NOT NULL REFERENCES rental.vehicles,
    customer_id       integer NOT NULL REFERENCES rental.customers,
    pickup_branch_id  integer NOT NULL REFERENCES rental.branches,
    return_branch_id  integer NOT NULL REFERENCES rental.branches,
    booked_at         timestamptz NOT NULL,
    start_ts          timestamptz NOT NULL,
    planned_end_ts    timestamptz NOT NULL,
    returned_ts       timestamptz,
    status            text NOT NULL,
    daily_rate_cents  integer NOT NULL,
    odometer_out_km   integer,
    odometer_in_km    integer,
    booking_channel   text NOT NULL,
    deleted_at        timestamptz
);

CREATE TABLE rental.invoices (
    invoice_id          bigint PRIMARY KEY,
    tenant_id           integer NOT NULL REFERENCES rental.tenants,
    agreement_id        bigint NOT NULL REFERENCES rental.rental_agreements,
    invoice_number      text NOT NULL UNIQUE,
    invoice_type        text NOT NULL,      -- invoice or credit_note
    credits_invoice_id  bigint,
    issued_on           date NOT NULL,
    due_on              date NOT NULL,
    paid_on             date,
    status              text NOT NULL,
    currency            char(3) NOT NULL,
    subtotal_cents      bigint NOT NULL,    -- negative on a credit note
    tax_cents           bigint NOT NULL,
    total_cents         bigint NOT NULL
);

-- Deliberately undeclared: invoice_lines -> invoices has no FK.
CREATE TABLE rental.invoice_lines (
    invoice_line_id bigint PRIMARY KEY,
    invoice_id      bigint NOT NULL,
    line_type       text NOT NULL,
    description     text NOT NULL,
    quantity        integer NOT NULL,
    unit_cents      bigint NOT NULL,
    amount_cents    bigint NOT NULL
);

-- agreement_id is NULL when damage was found between rentals, and undeclared.
CREATE TABLE rental.vehicle_damages (
    damage_id                 bigint PRIMARY KEY,
    tenant_id                 integer NOT NULL REFERENCES rental.tenants,
    vehicle_id                integer NOT NULL REFERENCES rental.vehicles,
    agreement_id              bigint,
    reported_on               date NOT NULL,
    damage_type               text NOT NULL,
    location_on_vehicle       text NOT NULL,
    severity                  text NOT NULL,
    status                    text NOT NULL,
    estimated_cost_cents      bigint NOT NULL,
    final_cost_cents          bigint,        -- NULL until repaired
    repaired_on               date,
    charged_to_customer       boolean NOT NULL,
    insurance_claimed         boolean NOT NULL,
    insurance_recovered_cents bigint NOT NULL DEFAULT 0,
    notes                     text
);

-- What one unit of a currency was worth in US dollars, by month.
CREATE TABLE rental.fx_rates (
    rate_month  date NOT NULL,
    currency    char(3) NOT NULL,
    usd_per_unit numeric(10, 6) NOT NULL,
    PRIMARY KEY (rate_month, currency)
);
