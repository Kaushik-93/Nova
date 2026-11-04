CREATE INDEX orders_order_ts_idx ON shop.orders (order_ts);
CREATE INDEX orders_customer_idx ON shop.orders (customer_id);
CREATE INDEX orders_utm_idx ON shop.orders (utm_campaign);
CREATE INDEX order_items_order_idx ON shop.order_items (order_id);
CREATE INDEX order_items_product_idx ON shop.order_items (product_id);
CREATE INDEX refunds_order_idx ON shop.refunds (order_id);

COMMENT ON TABLE shop.orders IS 'One row per customer order. Soft-deleted orders have deleted_at set.';
COMMENT ON COLUMN shop.orders.order_ts IS 'When the order was placed, in UTC.';
COMMENT ON COLUMN shop.orders.utm_campaign IS 'Attribution tag, lower-case and hyphenated.';
