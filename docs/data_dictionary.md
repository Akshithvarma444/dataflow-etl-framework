# Data dictionary

Generated from `config/datasets/*.yaml` by `scripts/generate_data_dictionary.py`. Counts quoted are from the real files, in `reports/source_profile.json`.

Every source column except where marked is read from the file under its exact source name, including misspellings such as `product_name_lenght`, so lineage to the source stays exact.

## Columns on every loaded table

| Column | Type | Null | Meaning |
|---|---|---|---|
| `source_file` | text | no | Source file name, without a path, so it is the same on every machine. |
| `source_row_number` | bigint | no | Record number in that file, from 1, excluding the header. Counts records, not lines, because quoted fields can contain newlines. |
| `first_run_id` | uuid | no | The run that first inserted this row. |
| `last_run_id` | uuid | no | The run that last changed it. An unchanged row keeps its old value. |
| `row_hash` | text | no | SHA-256 of the business columns, used to detect change. |
| `dq_warnings` | text | yes | Semicolon-separated warning rules this row triggered. |
| `created_at` | timestamptz | no | When the row was inserted. |
| `updated_at` | timestamptz | no | When the row was last changed. |


## Operational tables (`ops` schema)

| Table | Grain | Purpose |
|---|---|---|
| `pipeline_runs` | One per execution | Status, mode, data origin, counts, attempts, and failure reason. A rerun adds a row; it never overwrites one. |
| `pipeline_metrics` | One per run, table, and metric | Timings and counts in long format, including a count for each rule. |
| `processed_files` | One per dataset, file, checksum, and contract hash | What was loaded. Written in the load transaction, so it cannot claim a load that rolled back. |
| `rejected_records` | One per rejected source record | Quarantine: `rejection_id`, `run_id`, `last_seen_run_id`, `dataset_name`, `target_table`, `source_file`, `source_row_number`, `original_record` (JSONB, as received), `rejection_reason`, `rule_failures`, `rejected_at`, `resolved_at`, `resolved_by_run_id`. |
| `record_history` | One per superseded or removed row | Earlier values, as JSONB, with the run that replaced them. |


## Dataset `hotel_bookings` (schema `hotel`)

City and resort hotel bookings, 2015-07 to 2017-08.

### `hotel_bookings`

One row per source row. The source has no booking ID.

- **Grain:** One booking record per source row.
- **Primary key:** `record_id`
- **Source file pattern:** `hotel_bookings*.csv`
- **Duplicate policy:** flag
- **Load strategy:** replace_source

| Column | Type | Null | Origin | Meaning |
|---|---|---|---|---|
| `hotel` | text | no | source | Hotel type. |
| `is_canceled` | integer | no | source | 1 if canceled or no-show, else 0. |
| `lead_time` | integer | no | source | Days between booking and arrival. |
| `arrival_date_year` | integer | no | source | Arrival year. |
| `arrival_date_month` | text | no | source | Arrival month name. |
| `arrival_date_week_number` | integer | no | source | Source week number. Not ISO: differs on 64,054 rows, so not validated. |
| `arrival_date_day_of_month` | integer | no | source | Arrival day. |
| `stays_in_weekend_nights` | integer | no | source | Weekend nights booked. |
| `stays_in_week_nights` | integer | no | source | Week nights booked. |
| `adults` | integer | no | source | Adults. |
| `children` | integer | yes | source | Children. Literal NA in 4 rows means unknown; stays null. |
| `babies` | integer | no | source | Babies. |
| `meal` | text | no | source | Meal code as supplied. |
| `country` | text | yes | source | Country code as supplied, mostly ISO-3. Literal NULL in 488 rows. |
| `market_segment` | text | no | source | Market segment. |
| `distribution_channel` | text | no | source | Booking channel. |
| `is_repeated_guest` | integer | no | source | 1 if a repeat guest. |
| `previous_cancellations` | integer | no | source | Earlier cancellations by this guest. |
| `previous_bookings_not_canceled` | integer | no | source | Earlier completed bookings. |
| `reserved_room_type` | text | no | source | Reserved room code. |
| `assigned_room_type` | text | no | source | Assigned room code. |
| `booking_changes` | integer | no | source | Amendments before check-in or cancellation. |
| `deposit_type` | text | no | source | Deposit type. |
| `agent` | text | yes | source | Anonymized agent ID. Literal NULL in 16,340 rows. |
| `company` | text | yes | source | Anonymized company ID. Literal NULL in 112,593 rows. |
| `days_in_waiting_list` | integer | no | source | Days on the waiting list. |
| `customer_type` | text | no | source | Customer type. |
| `adr` | numeric(14,2) | no | source | Average daily rate, in the source's unstated currency. |
| `required_car_parking_spaces` | integer | no | source | Parking spaces requested. |
| `total_of_special_requests` | integer | no | source | Special requests. |
| `reservation_status` | text | no | source | Final status. |
| `reservation_status_date` | date | no | source | Date of the final status. |
| `record_id` | text | no | derived | SHA-256 of source file name and row number. An ingestion ID, not a business key. |
| `record_fingerprint` | text | no | derived | SHA-256 of all source values, for exact-duplicate detection. |
| `duplicate_occurrence` | integer | no | derived | 1 for the first occurrence of a fingerprint in a file, then 2, 3... |
| `arrival_date` | date | no | derived | Built from the arrival year, month name, and day. |
| `booking_date` | date | no | derived | arrival_date minus lead_time. |
| `total_nights` | integer | no | derived | Weekend plus week nights. 0 is a day-use stay. |
| `total_guests` | integer | no | derived | Adults + children + babies, unknown children counted as 0. |
| `meal_plan` | text | no | derived | meal with Undefined mapped to SC: the source paper treats both as no meal package. |
| `estimated_booking_value` | numeric(14,2) | no | derived | adr * total_nights, what the stay was priced at, including canceled bookings. Not revenue. |
| `realized_booking_value` | numeric(14,2) | no | derived | estimated_booking_value if not canceled, else 0. Ignores deposits and fees. |

| Rule | Severity | Check | Note |
|---|---|---|---|
| `negative_adr` | error | range on adr |  |
| `invalid_is_canceled` | error | range on is_canceled |  |
| `invalid_is_repeated_guest` | error | range on is_repeated_guest |  |
| `negative_lead_time` | error | range on lead_time |  |
| `negative_weekend_nights` | error | range on stays_in_weekend_nights |  |
| `negative_week_nights` | error | range on stays_in_week_nights |  |
| `negative_adults` | error | range on adults |  |
| `negative_children` | error | range on children |  |
| `negative_babies` | error | range on babies |  |
| `implausible_lead_time` | error | range on lead_time | Over ten years; the real maximum is 737. |
| `implausible_weekend_nights` | error | range on stays_in_weekend_nights | Over ten years; the real maximum is 19. |
| `implausible_week_nights` | error | range on stays_in_week_nights | Over ten years; the real maximum is 50. |
| `unknown_hotel` | error | allowed_values on hotel |  |
| `unknown_reservation_status` | error | allowed_values on reservation_status |  |
| `invalid_arrival_date` | error | custom on custom logic | Arrival year, month, and day do not form a real date. |
| `zero_guests` | error | custom on custom logic | A booking for nobody (180 real rows). |
| `cancel_status_mismatch` | error | custom on custom logic | is_canceled disagrees with reservation_status (0 real rows). |
| `zero_night_stay` | warning | custom on custom logic | Day use: 680 of 715 real rows checked out, all at ADR 0, so legitimate. |
| `high_adr` | warning | range on adr | Extreme rate (1 real row, 5400). |
| `large_party` | warning | custom on custom logic | More than 10 guests on one booking. |
| `checkout_date_mismatch` | warning | custom on custom logic | Check-out status date is not arrival + nights (27 real rows). |
| `unknown_meal` | warning | allowed_values on meal |  |
| `country_code_format` | warning | regex on country |  |

## Dataset `olist` (schema `olist`)

Brazilian marketplace orders, 2016-09 to 2018-10.

### `geolocation`

Coordinates seen for a zip prefix. The source has no key.

- **Grain:** One row per distinct combination of the five source values.
- **Primary key:** `geolocation_fingerprint`
- **Source file pattern:** `olist_geolocation_dataset*.csv`
- **Duplicate policy:** collapse
- **Load strategy:** upsert

| Column | Type | Null | Origin | Meaning |
|---|---|---|---|---|
| `geolocation_zip_code_prefix` | text | no | source | First five zip digits. Text: 245,733 rows have leading zeros. |
| `geolocation_lat` | double precision | no | source | Latitude. |
| `geolocation_lng` | double precision | no | source | Longitude. |
| `geolocation_city` | text | no | source | City as supplied, with mixed accents. |
| `geolocation_state` | text | no | source | State code. |
| `geolocation_fingerprint` | text | no | derived | SHA-256 of the five source values. |
| `duplicate_count` | integer | no | derived | How many source rows this row represents. |
| `city_normalized` | text | no | derived | Lowercase city without accents: sao paulo and são paulo match. |

| Rule | Severity | Check | Note |
|---|---|---|---|
| `invalid_zip_prefix` | error | regex on geolocation_zip_code_prefix |  |
| `invalid_state` | error | regex on geolocation_state |  |
| `invalid_latitude` | error | range on geolocation_lat |  |
| `invalid_longitude` | error | range on geolocation_lng |  |
| `latitude_outside_brazil` | warning | range on geolocation_lat | Valid coordinate, implausible for Brazil. |
| `longitude_outside_brazil` | warning | range on geolocation_lng | Valid coordinate, implausible for Brazil. |

### `product_category_translations`

Portuguese category names with their English translations.

- **Grain:** One row per Portuguese category name.
- **Primary key:** `product_category_name`
- **Source file pattern:** `product_category_name_translation*.csv`
- **Duplicate policy:** reject
- **Load strategy:** upsert

| Column | Type | Null | Origin | Meaning |
|---|---|---|---|---|
| `product_category_name` | text | no | source | Portuguese category name. |
| `product_category_name_english` | text | no | source | English category name. |

### `customers`

A customer identity per order. customer_unique_id identifies the person.

- **Grain:** One row per customer_id, which is one per order.
- **Primary key:** `customer_id`
- **Source file pattern:** `olist_customers_dataset*.csv`
- **Duplicate policy:** reject
- **Load strategy:** upsert
- **Reference:** `customer_zip_code_prefix` to `geolocation.geolocation_zip_code_prefix` (soft reference, reported as a warning)

| Column | Type | Null | Origin | Meaning |
|---|---|---|---|---|
| `customer_id` | text | no | source | Per-order customer key. |
| `customer_unique_id` | text | no | source | The person. Not unique here: 3,345 repeats. |
| `customer_zip_code_prefix` | text | no | source | First five zip digits. |
| `customer_city` | text | no | source | City, trimmed and lowercased. |
| `customer_state` | text | no | source | State code. |

| Rule | Severity | Check | Note |
|---|---|---|---|
| `invalid_zip_prefix` | error | regex on customer_zip_code_prefix |  |
| `invalid_state` | error | regex on customer_state |  |

### `sellers`

Marketplace sellers.

- **Grain:** One row per seller.
- **Primary key:** `seller_id`
- **Source file pattern:** `olist_sellers_dataset*.csv`
- **Duplicate policy:** reject
- **Load strategy:** upsert
- **Reference:** `seller_zip_code_prefix` to `geolocation.geolocation_zip_code_prefix` (soft reference, reported as a warning)

| Column | Type | Null | Origin | Meaning |
|---|---|---|---|---|
| `seller_id` | text | no | source | Seller key. |
| `seller_zip_code_prefix` | text | no | source | First five zip digits. |
| `seller_city` | text | no | source | City, trimmed and lowercased. |
| `seller_state` | text | no | source | State code. |

| Rule | Severity | Check | Note |
|---|---|---|---|
| `invalid_zip_prefix` | error | regex on seller_zip_code_prefix |  |
| `invalid_state` | error | regex on seller_state |  |

### `products`

Product catalog. 610 real rows have no category or name attributes.

- **Grain:** One row per product.
- **Primary key:** `product_id`
- **Source file pattern:** `olist_products_dataset*.csv`
- **Duplicate policy:** reject
- **Load strategy:** upsert
- **Reference:** `product_category_name` to `product_category_translations.product_category_name` (soft reference, reported as a warning)

| Column | Type | Null | Origin | Meaning |
|---|---|---|---|---|
| `product_id` | text | no | source | Product key. |
| `product_category_name` | text | yes | source | Portuguese category. Null in 610 real rows. |
| `product_name_lenght` | integer | yes | source | Name length. Misspelled in the source; kept so lineage is exact. |
| `product_description_lenght` | integer | yes | source | Description length. Misspelled in the source. |
| `product_photos_qty` | integer | yes | source | Photo count. |
| `product_weight_g` | integer | yes | source | Weight in grams. 0 in 4 real rows, flagged. |
| `product_length_cm` | integer | yes | source | Length. |
| `product_height_cm` | integer | yes | source | Height. |
| `product_width_cm` | integer | yes | source | Width. |

| Rule | Severity | Check | Note |
|---|---|---|---|
| `negative_weight` | error | range on product_weight_g |  |
| `negative_length` | error | range on product_length_cm |  |
| `negative_height` | error | range on product_height_cm |  |
| `negative_width` | error | range on product_width_cm |  |
| `zero_weight` | warning | range on product_weight_g |  |

### `orders`

Orders with their lifecycle timestamps. Timezone is not stated by the source.

- **Grain:** One row per order.
- **Primary key:** `order_id`
- **Source file pattern:** `olist_orders_dataset*.csv`
- **Duplicate policy:** reject
- **Load strategy:** upsert, version column `source_event_ts`
- **Reference:** `customer_id` to `customers.customer_id` (foreign key, enforced)

| Column | Type | Null | Origin | Meaning |
|---|---|---|---|---|
| `order_id` | text | no | source | Order key. |
| `customer_id` | text | no | source | References customers. |
| `order_status` | text | no | source | Order status, lowercased. |
| `order_purchase_timestamp` | timestamp | no | source | Purchase time. |
| `order_approved_at` | timestamp | yes | source | Payment approval. Null in 160 real rows. |
| `order_delivered_carrier_date` | timestamp | yes | source | Handed to carrier. Null in 1,783 real rows. |
| `order_delivered_customer_date` | timestamp | yes | source | Delivered. Null in 2,965 real rows. |
| `order_estimated_delivery_date` | timestamp | no | source | Promised delivery date. |
| `source_event_ts` | timestamp | no | derived | Latest lifecycle timestamp, the version that blocks out-of-order overwrites. |
| `delivery_days` | numeric(14,2) | yes | derived | Days from purchase to delivery. Null if undelivered. |
| `is_late_delivery` | integer | yes | derived | 1 if delivered after the promised date. Null if undelivered. |

| Rule | Severity | Check | Note |
|---|---|---|---|
| `unknown_order_status` | error | allowed_values on order_status |  |
| `approved_before_purchase` | warning | compare on order_approved_at >= order_purchase_timestamp |  |
| `carrier_before_purchase` | warning | compare on order_delivered_carrier_date >= order_purchase_timestamp | 166 real rows; source clock noise, order still real. |
| `delivered_before_carrier` | warning | compare on order_delivered_customer_date >= order_delivered_carrier_date | 23 real rows; source clock noise, order still real. |
| `delivered_without_date` | warning | custom on custom logic | Status delivered with no delivery date (8 real rows). |

### `order_items`

Order lines. The source has no quantity column; each row is one unit.

- **Grain:** One row per order and item sequence.
- **Primary key:** `order_id`, `order_item_id`
- **Source file pattern:** `olist_order_items_dataset*.csv`
- **Duplicate policy:** reject
- **Load strategy:** upsert
- **Reference:** `order_id` to `orders.order_id` (foreign key, enforced)
- **Reference:** `product_id` to `products.product_id` (foreign key, enforced)
- **Reference:** `seller_id` to `sellers.seller_id` (foreign key, enforced)

| Column | Type | Null | Origin | Meaning |
|---|---|---|---|---|
| `order_id` | text | no | source | References orders. |
| `order_item_id` | integer | no | source | Item sequence within the order. |
| `product_id` | text | no | source | References products. |
| `seller_id` | text | no | source | References sellers. |
| `shipping_limit_date` | timestamp | no | source | Seller shipping deadline. |
| `price` | numeric(14,2) | no | source | Unit price. |
| `freight_value` | numeric(14,2) | no | source | Freight for this item. |
| `item_total` | numeric(14,2) | no | derived | price + freight_value. |

| Rule | Severity | Check | Note |
|---|---|---|---|
| `invalid_item_sequence` | error | range on order_item_id |  |
| `non_positive_price` | error | range on price |  |
| `negative_freight` | error | range on freight_value |  |

### `payments`

Payment components of an order.

- **Grain:** One row per order and payment sequence.
- **Primary key:** `order_id`, `payment_sequential`
- **Source file pattern:** `olist_order_payments_dataset*.csv`
- **Duplicate policy:** reject
- **Load strategy:** upsert
- **Reference:** `order_id` to `orders.order_id` (foreign key, enforced)

| Column | Type | Null | Origin | Meaning |
|---|---|---|---|---|
| `order_id` | text | no | source | References orders. |
| `payment_sequential` | integer | no | source | Payment sequence. 80 real orders have gaps, so not required to be contiguous. |
| `payment_type` | text | no | source | Payment method. |
| `payment_installments` | integer | no | source | Installments. 0 in 2 real rows, flagged. |
| `payment_value` | numeric(14,2) | no | source | Amount paid. 0 in 9 real rows, flagged. |

| Rule | Severity | Check | Note |
|---|---|---|---|
| `invalid_payment_sequence` | error | range on payment_sequential |  |
| `negative_payment` | error | range on payment_value |  |
| `negative_installments` | error | range on payment_installments |  |
| `unknown_payment_type` | error | allowed_values on payment_type |  |
| `zero_payment` | warning | range on payment_value |  |
| `zero_installments` | warning | range on payment_installments |  |
| `undefined_payment_type` | warning | allowed_values on payment_type |  |

### `reviews`

Customer reviews. review_id alone repeats 814 times and order_id 551 times.

- **Grain:** One row per review and order, verified unique.
- **Primary key:** `review_id`, `order_id`
- **Source file pattern:** `olist_order_reviews_dataset*.csv`
- **Duplicate policy:** reject
- **Load strategy:** upsert
- **Reference:** `order_id` to `orders.order_id` (foreign key, enforced)

| Column | Type | Null | Origin | Meaning |
|---|---|---|---|---|
| `review_id` | text | no | source | Review key, shared across orders in 814 real rows. |
| `order_id` | text | no | source | References orders. |
| `review_score` | integer | no | source | Score from 1 to 5. |
| `review_comment_title` | text | yes | source | Title. Null in 87,656 real rows. |
| `review_comment_message` | text | yes | source | Comment, may contain newlines. Null in 58,247 real rows. |
| `review_creation_date` | timestamp | no | source | When the survey was sent. |
| `review_answer_timestamp` | timestamp | no | source | When it was answered. |
| `has_comment` | integer | no | derived | 1 if a comment or title was written. |

| Rule | Severity | Check | Note |
|---|---|---|---|
| `invalid_review_score` | error | range on review_score |  |
| `answered_before_created` | warning | compare on review_answer_timestamp >= review_creation_date |  |

### `order_summary` (derived)

Order-level totals, rebuilt from the published tables in the load transaction.

- **Grain:** One row per order, including orders with no items (775 real) or payments (1 real).
- **Primary key:** `order_id`

| Column | Type | Null | Origin | Meaning |
|---|---|---|---|---|
| `order_id` | text | no | derived | Order key. |
| `customer_id` | text | no | derived | Customer key. |
| `order_status` | text | no | derived | Order status. |
| `order_purchase_timestamp` | timestamp | no | derived | Purchase time. |
| `item_count` | integer | no | derived | Order lines, which equals units because each row is one unit. |
| `items_total` | numeric(14,2) | no | derived | Sum of price. |
| `freight_total` | numeric(14,2) | no | derived | Sum of freight. |
| `order_total` | numeric(14,2) | no | derived | items_total + freight_total. |
| `payment_count` | integer | no | derived | Payment components. |
| `payment_total` | numeric(14,2) | no | derived | Sum of payments. |
| `payment_difference` | numeric(14,2) | no | derived | payment_total - order_total. |
| `payment_matches_order` | integer | yes | derived | 1 if within 0.05. Null if items or payments are missing. Differs on 260 real orders: unexplained by the source, so reported rather than rejected. |
