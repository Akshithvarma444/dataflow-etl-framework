# Design decisions and trade-offs

Each entry states the decision, why it was made, and what it costs.

## Structure

**D1. Additions to the suggested repository layout.**
`data/expected/` keeps test-oracle answers apart from input data, as the brief requires.
`scripts/` holds tooling that is not part of the framework. `src/core/contract.py` holds the contract
models, `src/validators/validator.py` and `result.py` tie the validators together. Extra test files
cover config, the CLI, and the generator. `record_history` is an extra table (D7). `pyproject.toml` holds
pytest and coverage settings only.

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

**D13. Late parents: rejected children recover automatically.**
A child whose parent has not arrived is quarantined with `fk_violation`. Each run that processes a file
then retries open rejections whose only failure is a missing parent, and marks those that now pass with
`resolved_at`. Rows also rejected for another reason stay in quarantine. Nothing else needs reprocessing.

**D16. Reconciliation runs before commit and can block it, and says when it cannot compare.**
Source totals come from raw text before validation, independent of transformation. A difference raises an
error and rolls back, so a transformation bug cannot publish wrong numbers. A total can only be compared when
the target rows attributed to this run's files are exactly the rows it accepted. After a partial re-delivery
they are not, and the status is `not_comparable` with the reason. An earlier version reported such cases as
"explained", but a review showed that one skipped row could excuse a difference of any size.
*Cost:* those runs have no aggregate check, only the row-count identity and the SQL checks. They say so
plainly in the report. `scripts/verify_real_data.py` adds a second check using none of the pipeline's validation, transformation, or loading code.

**D17. A zero-byte file fails, a header-only file is an empty batch, and large deletions are refused.**
A zero-byte file suggests a broken delivery, so it stops the run. A header-only file is a valid delivery with
nothing in it: it succeeds with zero rows and deletes nothing. A new version that would remove more than
`max_delete_fraction` (default 50%) of a file's loaded rows fails the run, because it looks like a truncated or
mostly invalid delivery, and `--full-refresh` is the way to say it is intended. *Cost:* a genuinely much smaller
file needs that explicit step.

**D18. Restoring an earlier file version is treated as a change.**
The skip check compares with the *latest* load of a file, not any earlier one. Otherwise, loading version A, then
B, then A again would skip the last step and leave B's data in place.

**D19. A missing required column stops the run. A missing optional one does not.**
Every row would fail without a required column, which is a broken delivery rather than a data-quality issue,
and filling the quarantine with every row would hide that. Unexpected columns are ignored and reported by
default, with `on_unexpected_column: fail` available per table. Rejected rows keep them in `original_record`.

## Environment

**D14. Default host port 5434, bound to loopback.**
5432 and 5433 are commonly taken on developer machines, and loopback binding keeps the database
off the network. `POSTGRES_PORT` changes it.

**D20. Processing a file closes its open rejections, and recovery never overwrites.**
A new version of a file replaces what the old version said, so its open rejections close as `superseded` before
it is read. Anything still wrong is rejected again, which reopens it. Recovery only inserts: if the key has since
arrived another way, that newer row wins and the rejection closes as `superseded`. Both rules exist because a
review showed that a stale quarantined row could otherwise overwrite a correction. Closed rejections are kept as a
record of what was received.

**D21. Values are rejected rather than altered to fit.**
A decimal with more places than `numeric(14,2)` holds is rejected, because PostgreSQL would round it without an
error. Hotel stays and lead times over ten years are rejected, because they would overflow date arithmetic. The
real maximums are 19 weekend nights, 50 week nights, and a 737-day lead time.

**D22. Collapsed tables accept one source file.**
A `duplicate_count` describes one file. Across several, neither overwriting nor summing would be right, since a
rerun would then double-count. Rather than store a wrong number, a second file fails the run with a clear message.

**D15. Dependencies are pinned to exact versions** that were installed and tested, so a clean
environment gets the same behavior.
