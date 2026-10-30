-- Queries a target analytical consumer or pipeline operator would run.
-- scripts/run_sample_queries.py runs them and writes reports/sample_queries.md.

-- name: Run history, most recent first
SELECT dataset_name, status, run_mode, data_origin, input_count, valid_count, rejected_count,
       inserted_count, updated_count, skipped_count, attempts, finished_at - started_at AS duration
FROM ops.pipeline_runs ORDER BY started_at DESC LIMIT 10;

-- name: Open quarantine by dataset, table, and reason
SELECT dataset_name, target_table, rejection_reason, count(*) AS records
FROM ops.rejected_records WHERE resolved_at IS NULL
GROUP BY 1, 2, 3 ORDER BY records DESC LIMIT 10;

-- name: Data-quality warnings on loaded hotel bookings
SELECT dq_warnings, count(*) AS bookings
FROM hotel.hotel_bookings WHERE dq_warnings IS NOT NULL
GROUP BY 1 ORDER BY bookings DESC LIMIT 10;

-- name: Cancellation rate by hotel
SELECT hotel, count(*) AS bookings, sum(is_canceled) AS canceled,
       round(100.0 * sum(is_canceled) / count(*), 1) AS cancel_pct,
       round(avg(adr), 2) AS avg_adr
FROM hotel.hotel_bookings GROUP BY 1 ORDER BY 1;

-- name: Cancellation rate by deposit type
SELECT deposit_type, count(*) AS bookings, round(100.0 * sum(is_canceled) / count(*), 1) AS cancel_pct
FROM hotel.hotel_bookings GROUP BY 1 ORDER BY bookings DESC;

-- name: Estimated versus realized booking value by year
SELECT arrival_date_year AS year, sum(estimated_booking_value) AS estimated_value,
       sum(realized_booking_value) AS realized_value
FROM hotel.hotel_bookings GROUP BY 1 ORDER BY 1;

-- name: Exact-duplicate impact on hotel cancellation rate
SELECT CASE WHEN duplicate_occurrence = 1 THEN 'first occurrence' ELSE 'exact copy' END AS row_kind,
       count(*) AS bookings, round(100.0 * sum(is_canceled) / count(*), 1) AS cancel_pct
FROM hotel.hotel_bookings GROUP BY 1 ORDER BY 1;

-- name: Monthly Olist revenue, delivered orders
SELECT date_trunc('month', order_purchase_timestamp)::date AS month, count(*) AS orders,
       sum(items_total) AS items_revenue, sum(freight_total) AS freight
FROM olist.order_summary WHERE order_status = 'delivered'
GROUP BY 1 ORDER BY 1 DESC LIMIT 6;

-- name: Olist payment value by type
SELECT payment_type, count(*) AS payments, sum(payment_value) AS total_value
FROM olist.payments GROUP BY 1 ORDER BY total_value DESC;

-- name: Olist orders where payment and order total disagree
SELECT coalesce(payment_matches_order::text, 'not comparable') AS payment_matches_order, count(*) AS orders,
       sum(payment_difference) AS total_difference
FROM olist.order_summary GROUP BY 1 ORDER BY 1;

-- name: Olist on-time delivery and review score
SELECT o.is_late_delivery, count(*) AS reviews, round(avg(r.review_score), 2) AS avg_score,
       round(avg(o.delivery_days), 1) AS avg_delivery_days
FROM olist.orders o JOIN olist.reviews r USING (order_id)
WHERE o.is_late_delivery IS NOT NULL GROUP BY 1 ORDER BY 1;

-- name: Top Olist categories by revenue, in English
SELECT coalesce(t.product_category_name_english, p.product_category_name, '(none)') AS category,
       count(*) AS units, sum(i.price) AS revenue
FROM olist.order_items i JOIN olist.products p USING (product_id)
LEFT JOIN olist.product_category_translations t USING (product_category_name)
GROUP BY 1 ORDER BY revenue DESC LIMIT 5;
