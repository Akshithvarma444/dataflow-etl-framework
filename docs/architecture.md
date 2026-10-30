# Architecture

Status: the design below was finalized in Phase 1, before the real data was profiled.
Facts about the datasets marked *(to verify)* are expectations that Phase 2 confirms or
corrects against the actual files. Target columns are finalized from that profile.

## Pipeline flow

The same engine runs every dataset. A dataset is described by a YAML contract plus an
optional transformer module; nothing in the engine knows about Olist or hotels.

```mermaid
flowchart TD
    SRC[Source files: CSV or JSON] --> EXT[Extraction: checksum, row numbers, all values as text]
    EXT --> STAGE[Raw staging: text-typed chunks with lineage columns]
    STAGE --> SCHEMA[Schema validation: columns, types, required fields]
    SCHEMA --> BIZ[Business validation: keys, duplicates, references, rules]
    BIZ -->|valid or warned| TRANS[Transformation: deterministic derived fields]
    BIZ -->|error| QUAR[Quarantine: rejected_records + data/rejected]
    SCHEMA -->|malformed row| QUAR
    SCHEMA -.->|required column missing: fail the file| RUN
    TRANS --> LOAD[PostgreSQL loader: one transaction]
    QUAR --> LOAD
    LOAD --> TGT[Target tables]
    TGT --> REC[Reconciliation]
    REC --> RUN[pipeline_runs, pipeline_metrics, JSON reports]
```

| Stage | Module | Reusable part | Dataset-specific part |
|---|---|---|---|
| Extraction | `src/extractors` | CSV and JSON readers, checksums, row numbering | File names in YAML |
| Validation | `src/validators` | Rule engine and check types | Rules in YAML |
| Transformation | `src/transformers` | Base class, row hash, IDs | `olist_transformer.py`, `hotel_transformer.py` |
| Load | `src/loaders` | Upsert, history capture, counts | Keys and strategy in YAML |
| Observability | `src/monitoring` | Logging, run records, metrics | None |
| Reconciliation | `src/reporting` | Report engine | Aggregates to compare in YAML |

## Execution workflow for one dataset

1. Create a `pipeline_runs` row (`running`) with a new UUID, committed immediately so the
   record survives any later failure.
2. Take a PostgreSQL advisory lock for the dataset. A concurrent run fails fast. Any older
   run still marked `running` must have died, so it is marked `abandoned`.
3. For each source file in foreign-key order, compute its checksum and look it up in
   `processed_files`. An unchanged file already loaded under the same contract is skipped.
4. Extract chunks, validate, and split into valid and rejected. References are checked
   against valid parents from this run plus parents already in the database, so a skipped
   parent file still satisfies its children.
5. Transform valid rows, then open **one transaction** covering target tables, history,
   quarantine rows, and `processed_files`. Any error rolls all of it back, so a file can
   never be recorded as processed when its data was not published.
6. Reconcile, write metrics and reports, and set the final run status in a separate
   transaction, so a failure reason is recorded even when the load rolled back.

## Entity relationships

Three PostgreSQL schemas keep datasets and operations apart. Every target table also carries
the audit columns: `source_file`, `source_row_number`, `first_run_id`, `last_run_id`,
`row_hash`, `created_at`, `updated_at`.

```mermaid
erDiagram
    customers ||--o{ orders : "customer_id"
    orders ||--o{ order_items : "order_id"
    products ||--o{ order_items : "product_id"
    sellers ||--o{ order_items : "seller_id"
    orders ||--o{ payments : "order_id"
    orders ||--o{ reviews : "order_id"
    orders ||--o| order_summary : "derived, one per order"
    product_category_translations |o..o{ products : "soft: category name"
    geolocation }o..o{ customers : "soft: zip prefix"
    geolocation }o..o{ sellers : "soft: zip prefix"

    customers { text customer_id PK
                text customer_unique_id
                text customer_zip_code_prefix }
    orders { text order_id PK
             text customer_id FK
             text order_status
             timestamp order_purchase_timestamp
             timestamp source_event_ts "version guard" }
    order_items { text order_id PK
                  int order_item_id PK
                  text product_id FK
                  text seller_id FK
                  numeric price
                  numeric freight_value
                  numeric item_total "derived" }
    payments { text order_id PK
               int payment_sequential PK
               text payment_type
               numeric payment_value }
    reviews { text review_id PK
              text order_id PK
              int review_score }
    products { text product_id PK
               text product_category_name }
    sellers { text seller_id PK }
    geolocation { text geolocation_fingerprint PK
                  int duplicate_count "collapsed" }
    product_category_translations { text product_category_name PK }
    order_summary { text order_id PK
                    numeric items_total
                    numeric freight_total
                    numeric payment_total
                    numeric payment_difference }
```

Solid lines are enforced foreign keys. Dotted lines are soft references, checked and reported
as warnings, because the source is expected to violate them legitimately *(to verify)*.

```mermaid
erDiagram
    pipeline_runs ||--o{ pipeline_metrics : "run_id"
    pipeline_runs ||--o{ rejected_records : "run_id"
    pipeline_runs ||--o{ processed_files : "run_id"
    pipeline_runs ||--o{ record_history : "superseded_by_run_id"

    pipeline_runs { uuid run_id PK
                    text dataset_name
                    text status
                    text run_mode
                    timestamptz started_at
                    timestamptz finished_at
                    text failure_reason }
    pipeline_metrics { uuid run_id PK
                       text stage PK
                       text table_name PK
                       text metric_name PK
                       numeric metric_value }
    processed_files { text dataset_name PK
                      text source_file PK
                      text checksum PK
                      text contract_hash PK
                      uuid run_id FK
                      bigint row_count }
    rejected_records { text rejection_id PK
                       uuid run_id FK
                       text dataset_name
                       text source_file
                       bigint source_row_number
                       jsonb original_record
                       text rejection_reason
                       jsonb rule_failures
                       timestamptz rejected_at
                       timestamptz resolved_at }
    record_history { bigint history_id PK
                     text target_table
                     jsonb business_key
                     jsonb previous_record
                     uuid superseded_by_run_id FK
                     timestamptz superseded_at }
```

`hotel.hotel_bookings` stands alone. Its grain is one row per source row:

| Column | Role |
|---|---|
| `record_id` (PK) | SHA-256 of logical source name and row number. An ingestion ID, not a business key. |
| `record_fingerprint` | SHA-256 of the source column values, used to detect exact duplicates. |
| `is_exact_duplicate`, `duplicate_group_size` | Duplicate flag, not a rejection (see design decisions). |
| Source columns | As found in the file, typed. Finalized in Phase 2. |
| `total_nights`, `total_guests`, `arrival_date`, `estimated_booking_value` | Derived and documented. |

## Runtime topology

- **Local:** `docker compose up -d db`, then the pipeline in a virtualenv on the host,
  connecting to `127.0.0.1:5434`.
- **Docker:** the same database plus `docker compose run --rm pipeline ...`, connecting to
  `db:5432` on the Compose network. The pipeline is a job with a Compose profile, so
  `docker compose up -d` starts only the database and never launches a run by itself.
