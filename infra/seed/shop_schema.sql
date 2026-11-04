-- The e-commerce source. Money is integer cents; timestamps are UTC.
DROP SCHEMA IF EXISTS shop CASCADE;
CREATE SCHEMA shop;

CREATE TABLE shop.regions (
    region_id    integer PRIMARY KEY,
    region_code  text NOT NULL UNIQUE,
    region_name  text NOT NULL,
    country_code char(2) NOT NULL
);

CREATE TABLE shop.products (
    product_id       integer PRIMARY KEY,
    sku              text NOT NULL UNIQUE,
    product_name     text NOT NULL,
    category         text NOT NULL,
    description      text,
    list_price_cents integer NOT NULL,
    unit_cost_cents  integer NOT NULL,
    launched_on      date NOT NULL,
    discontinued_on  date
);

CREATE TABLE shop.customers (
    customer_id integer PRIMARY KEY,
    email       text NOT NULL,          -- not unique: people register twice
    first_name  text NOT NULL,
    last_name   text NOT NULL,
    region_id   integer NOT NULL REFERENCES shop.regions,
    signup_ts   timestamptz NOT NULL,
    email_opt_in boolean NOT NULL,
    is_test     boolean NOT NULL DEFAULT false
);

CREATE TABLE shop.orders (
    order_id       bigint PRIMARY KEY,
    order_number   text NOT NULL UNIQUE,
    customer_id    integer NOT NULL REFERENCES shop.customers,
    region_id      integer NOT NULL REFERENCES shop.regions,
    order_ts       timestamptz NOT NULL,
    status         text NOT NULL,
    channel        text NOT NULL,
    utm_campaign   text,
    shipping_cents integer NOT NULL,
    tax_cents      integer NOT NULL,
    discount_cents integer NOT NULL,
    deleted_at     timestamptz
);

-- Deliberately undeclared: order_items -> orders/products has no FK, so the
-- profiler has to infer it from names and value overlap.
CREATE TABLE shop.order_items (
    order_item_id    bigint PRIMARY KEY,
    order_id         bigint NOT NULL,
    product_id       integer NOT NULL,
    quantity         integer NOT NULL,
    unit_price_cents integer NOT NULL
);

CREATE TABLE shop.refunds (
    refund_id    bigint PRIMARY KEY,
    order_id     bigint NOT NULL REFERENCES shop.orders,
    refund_ts    timestamptz NOT NULL,
    amount_cents integer NOT NULL,
    reason       text NOT NULL
);
