# Design decisions and trade-offs

Each entry states the decision, why it was made, and what it costs.

## Structure

**D1. Additions to the suggested repository layout.**
`data/expected/` keeps test-oracle answers apart from input data, as the brief requires.
`scripts/` holds tooling that is not part of the framework. `tests/unit/test_config.py` tests
the config module. `record_history` is an extra table (D7). `pyproject.toml` holds pytest and
coverage settings only.

**D2. Separate PostgreSQL schemas (`olist`, `hotel`, `ops`).**
Table names such as `reviews` stay readable, and datasets cannot collide.

**D3. Raw staging is logical, not a database table.**
Chunks stay text-typed in memory with lineage columns attached, and the immutable source file
plus its checksum is the raw record. Bad rows keep their original values in quarantine as JSONB.
*Cost:* no SQL access to the raw values of accepted rows. *Why:* persisted raw tables would
store each row twice for no requirement in this brief. A persisted stage is a contained future
addition in the loader.

## Validation

**D4. Row-level validation is vectorized pandas, not pydantic.**
Pydantic validates the configuration. Building a model per row would be the slowest part of a
100,000+ row run, and reported failures per record are just as achievable with masks.

**D5. All values are read as text, then coerced.**
Letting pandas infer types silently turns `"abc"` in a numeric column into a failed read or a
whole column of objects. Reading text first lets one bad value reject one row, with its original
value preserved. `csv.reader` handles the file so that a row with the wrong field count is
quarantined and not fatal, and so row numbers remain correct when quoted fields contain newlines.
`source_row_number` is therefore the data record's ordinal, not the physical line number.

**D6. Two severities and three duplicate policies, chosen per table in YAML.**
`error` quarantines and `warning` loads with a flag, so legitimate oddities are not rejected.
Duplicates are `reject` (a key that must be unique), `collapse` (rows that carry no extra
information, with a count kept), or `flag` (no reliable key exists, so a duplicate may be a real
separate record). This is the core of the metadata-driven design: the same engine gives each
source the behavior that is correct for it. Collapsed and flagged rows appear in reconciliation,
so nothing is silently dropped.

## Idempotency and recovery

**D7. Upsert compares a row hash and archives what it replaces.**
Each batch is copied into a temporary table. Rows whose hash changed are copied to
`record_history`, then `INSERT ... ON CONFLICT DO UPDATE ... WHERE row_hash IS DISTINCT FROM`
applies. `RETURNING (xmax = 0)` separates inserted from updated, and the remainder is skipped.
A replay therefore changes nothing, and a correction keeps its previous value.

**D8. The skip key is dataset, file, checksum, and contract hash.**
Skipping is per file, never global, so a new or corrected file always processes. Including a hash
of the YAML contract means a changed rule re-validates old files, which a checksum alone would
miss. Each rerun still creates a run record, with status `skipped` if nothing needed doing.

**D9. One transaction per dataset run.**
Target rows, history, quarantine, and `processed_files` commit together, which makes retry simple:
after a failure nothing was published, so rerunning is always safe. *Cost:* a long transaction
on the largest run, and a failure in one file discards the others in that run. This is acceptable
for batch loads at this scale and much easier to reason about than per-file partial commits.

**D10. Only transient database errors are retried.**
Connection-level failures retry with exponential backoff. Constraint and data errors would fail
identically every time, so they fail the run immediately with the reason recorded.

**D11. Two load strategies, because the sources differ.**
Olist tables have keys, so `upsert` treats files as incremental and never deletes. Hotel bookings
have no key, so `record_id` is derived from the logical source name and row position, as the
brief specifies, and `replace_source` treats a file as a snapshot of its own rows: rows gone from a
changed file are archived to `record_history` and deleted. Using the logical name instead of the
checksum in the ID is deliberate, since a checksum-based ID would turn every corrected file into a
full set of duplicate bookings. *Cost:* inserting a row mid-file shifts every later position, which
reports those rows as updated. The end state is still correct, and the churn is a documented limit
of having no business key.

**D12. Out-of-order protection uses a version column where one exists.**
When a contract names a `version_column`, an upsert only applies if the incoming version is not
older than the stored one, so a late-delivered old file cannot overwrite newer data. Such rows are
counted as stale. Tables without a usable version are last-write-wins, which is documented per table.

**D13. Late parents: rejected children are replayable.**
A child whose parent has not arrived is quarantined with `fk_violation`. `rejected_records` retains
the original record and a `resolved_at` column, so children can be retried after the parent arrives,
with no need to reprocess a file unrelated to the fix.

## Environment

**D14. Default host port 5434, bound to loopback.**
5432 and 5433 are commonly taken on developer machines, and loopback binding keeps the database
off the network. `POSTGRES_PORT` changes it.

**D15. Dependencies are pinned to exact versions** that were installed and tested, so a clean
environment gets the same behavior.
