DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'driftsql_olist_reader'
    ) THEN
        CREATE ROLE driftsql_olist_reader NOLOGIN;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'driftsql_orders_limited'
    ) THEN
        CREATE ROLE driftsql_orders_limited NOLOGIN;
    END IF;
END
$$;

ALTER ROLE driftsql_olist_reader NOLOGIN;
ALTER ROLE driftsql_orders_limited NOLOGIN;

REVOKE ALL PRIVILEGES ON SCHEMA olist_raw
    FROM driftsql_olist_reader, driftsql_orders_limited;
GRANT USAGE ON SCHEMA olist_raw
    TO driftsql_olist_reader, driftsql_orders_limited;

REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA olist_raw
    FROM driftsql_olist_reader, driftsql_orders_limited;

GRANT SELECT ON TABLE
    olist_raw.customers,
    olist_raw.geolocation,
    olist_raw.order_items,
    olist_raw.order_payments,
    olist_raw.order_reviews,
    olist_raw.orders,
    olist_raw.products,
    olist_raw.sellers,
    olist_raw.product_category_name_translation
TO driftsql_olist_reader;

GRANT SELECT (order_id, order_status)
    ON TABLE olist_raw.orders
    TO driftsql_orders_limited;