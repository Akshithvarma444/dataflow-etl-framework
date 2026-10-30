# Testing strategy

## How to run

```bash
docker compose up -d db                     # integration tests need the database
pytest -v                                   # everything
pytest tests/unit                           # no database needed for most of these
pytest --cov=src --cov-report=term-missing  # with coverage
python scripts/mutation_check.py            # do the tests notice broken code?
python scripts/verify_real_data.py          # compare real runs with the raw files
```

Integration tests only touch `POSTGRES_TEST_DB`, which is created on demand. Each test starts by dropping
the `hotel`, `olist`, and `ops` schemas in that database, and never touches `POSTGRES_DB`. If the database
cannot be reached, those tests are skipped with the reason shown, so check the summary line for skips.

## Four layers of evidence

1. **Unit tests** check one behavior on in-memory data, with expected values worked out by hand.
2. **Integration tests against an answer key.** The generator labels each defect it injects and computes
   expected counts and totals with plain pandas, without using pipeline code, into `data/expected/`. Tests
   assert that the pipeline rejects exactly the labelled rows for exactly the labelled reasons.
3. **Failure injection.** Tests make loads fail midway, and one uses a real PostgreSQL constraint with no
   mocks, then assert that nothing was published and that a rerun completes without duplicates.
4. **Checks on the tests themselves and on real data.** `scripts/mutation_check.py` breaks the code in plausible
   ways and confirms that tests fail. `scripts/verify_real_data.py` compares database results with figures
   recomputed from the raw files, sharing no code with the pipeline.

## Where each required scenario is tested

| Scenario | Test file |
|---|---|
| Happy path | `integration/test_hotel_pipeline.py`, `integration/test_olist_pipeline.py` |
| Empty input | `edge_cases/test_empty_input.py` |
| Malformed CSV | `edge_cases/test_malformed_input.py`, `unit/test_extraction.py` |
| Missing required column | `edge_cases/test_schema_drift.py` |
| Unexpected column | `edge_cases/test_schema_drift.py` |
| Null required values | `edge_cases/test_null_values.py` |
| Duplicate primary key | `edge_cases/test_duplicates.py` |
| Invalid foreign key | `edge_cases/test_malformed_input.py`, `integration/test_olist_pipeline.py` |
| Invalid business values | `edge_cases/test_malformed_input.py`, `integration/*` |
| Database connection failure | `edge_cases/test_retries.py` |
| Partial load failure | `edge_cases/test_retries.py` |
| Retry after failure | `edge_cases/test_retries.py` |
| Reprocessing the same file | `edge_cases/test_idempotency.py` |
| Changed source file | `edge_cases/test_idempotency.py` |
| Late-arriving data | `edge_cases/test_idempotency.py` |
| Out-of-order data | `edge_cases/test_idempotency.py`, `integration/test_postgres_loader.py` |
| Hotel-specific edge cases | `integration/test_hotel_pipeline.py`: day-use stays, zero guests, child-only bookings, per-column `NA`, extreme rates |
| Olist-specific edge cases | `integration/test_olist_pipeline.py`: shared `review_id`, orders with no items, untranslated categories, leading-zero zips, clock noise |

## Known untested scenarios

These are not covered by automated tests:

- **A process killed during a real load.** Recovery is tested by simulating the state it leaves (a `running` row),
  not by killing a process.
- **Two real processes racing.** The lock is tested from one process holding it while another run tries.
- **Network loss midway through a transaction.** Transient errors are injected at the loader. A connection
  that is refused at the start is tested for real.
- **Very large fields and disk exhaustion.** The 10 MB field limit is configured, but not tested at the limit.
- **A stray quote swallowing fewer than 100 lines.** A record spanning more lines than that fails the file, but a
  shorter one can pass as a long field, because valid CSV allows newlines in quotes. See Known limitations.
