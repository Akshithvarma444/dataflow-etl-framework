"""Validation logic on in-memory data, one behavior per test."""

from pathlib import Path

import pandas as pd
import pytest

from src.core.constants import DuplicatePolicy
from src.core.contract import ColumnSpec, DuplicateSpec, ReferenceSpec, RuleSpec, SourceSpec, TableContract
from src.core.exceptions import ConfigurationError, SchemaDriftError
from src.extractors.base import MalformedRow, RawBatch, SourceFile
from src.validators.schema_validator import check_columns
from src.validators.validator import RecordValidator

SOURCE = SourceFile(Path("t.csv"), "t.csv", "checksum", 1, "utf-8")


def table(**overrides) -> TableContract:
    base = dict(
        name="t",
        source=SourceSpec(pattern="*.csv"),
        columns=(
            ColumnSpec(name="id", type="text", required=True),
            ColumnSpec(name="amount", type="decimal"),
            ColumnSpec(name="qty", type="integer"),
            ColumnSpec(name="at", type="timestamp"),
            ColumnSpec(name="ref", type="text"),
        ),
        primary_key=("id",),
    )
    return TableContract(**{**base, **overrides})


def raw(rows: list[dict]) -> RawBatch:
    columns = ["id", "amount", "qty", "at", "ref"]
    frame = pd.DataFrame([[row.get(c, "") for c in columns] for row in rows], columns=columns, dtype=object)
    frame["source_row_number"] = pd.array(range(1, len(rows) + 1), dtype="int64")
    return RawBatch(frame)


def validate(rows: list[dict], contract: TableContract | None = None, parents: dict | None = None, custom=None):
    validator = RecordValidator("ds", contract or table(), SOURCE, parents or {}, custom or (lambda *_: None))
    return validator.validate(raw(rows))


def reasons(result) -> dict[int, str]:
    return {record["source_row_number"]: record["rejection_reason"] for record in result.rejected}


def test_valid_row_is_typed_and_carries_lineage() -> None:
    result = validate([{"id": "a", "amount": "1.50", "qty": "3", "at": "2020-01-02 03:04:05"}])

    row = result.valid.iloc[0]
    assert (row["amount"], row["qty"]) == (1.5, 3)
    assert row["at"] == pd.Timestamp("2020-01-02 03:04:05")
    assert (row["source_file"], row["source_row_number"]) == ("t.csv", 1)
    assert result.rejected == []


@pytest.mark.parametrize(
    ("field", "value"),
    [("amount", "abc"), ("amount", "nan"), ("amount", "inf"), ("amount", "1e20"), ("qty", "1.5"),
     ("qty", "99999999999"), ("at", "2018-02-30 00:00:00"), ("at", "yesterday")],
)
def test_unusable_value_rejects_only_its_row(field: str, value: str) -> None:
    result = validate([{"id": "good"}, {"id": "bad", field: value}])

    assert reasons(result) == {2: f"invalid_type:{field}"}
    assert result.valid["id"].tolist() == ["good"]


def test_rejected_record_keeps_its_original_text_and_trace_fields() -> None:
    result = validate([{"id": "bad", "amount": "abc", "qty": "007"}])

    record = result.rejected[0]
    assert record["original_record"]["amount"] == "abc"
    assert record["original_record"]["qty"] == "007"
    assert (record["source_file"], record["source_row_number"], record["target_table"]) == ("t.csv", 1, "t")


def test_every_failure_on_a_record_is_reported() -> None:
    contract = table(rules=(RuleSpec(name="too_many", check="range", column="qty", max=5),))

    result = validate([{"id": "", "amount": "abc", "qty": "9"}], contract)

    assert reasons(result)[1] == "invalid_type:amount; missing_required:id; too_many"
    assert len(result.rejected[0]["rule_failures"]) == 3


def test_missing_key_rows_are_counted() -> None:
    assert validate([{"id": ""}]).missing_key_rows == 1


@pytest.mark.parametrize(
    ("rule", "row", "fails"),
    [
        (RuleSpec(name="r", check="range", column="qty", min=1), {"qty": "0"}, True),
        (RuleSpec(name="r", check="range", column="qty", min=1), {"qty": "1"}, False),
        (RuleSpec(name="r", check="range", column="amount", min=0, min_exclusive=True), {"amount": "0"}, True),
        (RuleSpec(name="r", check="range", column="qty", max=5), {"qty": "5"}, False),
        (RuleSpec(name="r", check="range", column="qty", min=1), {}, False),  # null passes
        (RuleSpec(name="r", check="allowed_values", column="ref", values=("x",)), {"ref": "y"}, True),
        (RuleSpec(name="r", check="allowed_values", column="ref", values=("x",)), {"ref": "x"}, False),
        (RuleSpec(name="r", check="regex", column="ref", pattern=r"\d{2}"), {"ref": "123"}, True),
        (RuleSpec(name="r", check="regex", column="ref", pattern=r"\d{2}"), {"ref": "12"}, False),
    ],
)
def test_declarative_rule_checks(rule: RuleSpec, row: dict, fails: bool) -> None:
    result = validate([{"id": "a", **row}], table(rules=(rule,)))

    assert bool(result.rejected) is fails


def test_compare_rule_ignores_rows_with_a_null_side() -> None:
    contract = table(
        columns=(ColumnSpec(name="id", type="text", required=True), ColumnSpec(name="amount", type="decimal"),
                 ColumnSpec(name="qty", type="integer")),
        rules=(RuleSpec(name="r", check="compare", left="amount", op=">=", right="qty"),),
    )

    result = validate([{"id": "a", "amount": "1", "qty": "2"}, {"id": "b", "amount": "", "qty": "2"}], contract)

    assert reasons(result) == {1: "r"}


def test_warning_rule_loads_the_record_and_flags_it() -> None:
    contract = table(rules=(RuleSpec(name="odd", check="range", column="qty", max=5, severity="warning"),))

    result = validate([{"id": "a", "qty": "9"}, {"id": "b", "qty": "1"}], contract)

    assert result.rejected == []
    assert result.valid["dq_warnings"].tolist() == ["odd", None]
    assert result.warnings["odd"] == 1


def test_missing_reference_rejects_when_error_and_flags_when_warning() -> None:
    hard = table(references=(ReferenceSpec(column="ref", parent_table="p", parent_column="k"),))
    soft = table(references=(ReferenceSpec(column="ref", parent_table="p", parent_column="k", on_missing="warning"),))
    rows = [{"id": "a", "ref": "known"}, {"id": "b", "ref": "ghost"}, {"id": "c"}]

    assert reasons(validate(rows, hard, {"ref": {"known"}})) == {2: "fk_violation:ref"}
    flagged = validate(rows, soft, {"ref": {"known"}})
    assert flagged.rejected == []
    assert flagged.valid["dq_warnings"].tolist() == [None, "fk_violation:ref", None]


def test_duplicate_key_rejects_later_row_even_in_a_later_batch() -> None:
    validator = RecordValidator("ds", table(), SOURCE, {}, lambda *_: None)

    first = validator.validate(raw([{"id": "a"}]))
    second = validator.validate(raw([{"id": "a"}, {"id": "b"}]))

    assert first.rejected == []
    assert second.duplicate_key_rows == 1
    assert second.valid["id"].tolist() == ["b"]


def test_row_failing_validation_does_not_claim_the_key() -> None:
    result = validate([{"id": "a", "amount": "bad"}, {"id": "a", "amount": "1"}])

    assert reasons(result) == {1: "invalid_type:amount"}
    assert result.valid["amount"].tolist() == [1.0]


def test_collapse_policy_keeps_one_row_without_rejecting_copies() -> None:
    contract = table(
        columns=(ColumnSpec(name="id", type="text", required=True),),
        derived_columns=(ColumnSpec(name="fp", type="text"), ColumnSpec(name="n", type="integer")),
        primary_key=("fp",),
        duplicates=DuplicateSpec(policy=DuplicatePolicy.COLLAPSE, fingerprint_column="fp", count_column="n"),
    )
    validator = RecordValidator("ds", contract, SOURCE, {}, lambda *_: None)

    result = validator.validate(raw([{"id": "a"}, {"id": "a"}, {"id": "b"}]))

    assert result.rejected == []
    assert result.collapsed == 1
    assert result.valid["id"].tolist() == ["a", "b"]
    assert sorted(validator.duplicates.counts.values()) == [1, 2]


def test_flag_policy_keeps_all_rows_and_numbers_each_occurrence() -> None:
    contract = table(
        columns=(ColumnSpec(name="id", type="text", required=True),),
        derived_columns=(ColumnSpec(name="fp", type="text"), ColumnSpec(name="occ", type="integer")),
        primary_key=("fp",),
        duplicates=DuplicateSpec(policy=DuplicatePolicy.FLAG, fingerprint_column="fp", occurrence_column="occ"),
    )

    result = validate([{"id": "a"}, {"id": "a"}, {"id": "b"}], contract)

    assert result.valid["occ"].tolist() == [1, 2, 1]
    assert result.duplicates_flagged == 1


def test_per_column_null_token_is_not_applied_to_other_columns() -> None:
    contract = table(columns=(
        ColumnSpec(name="id", type="text", required=True),
        ColumnSpec(name="amount", type="decimal", null_tokens=("", "NA")),
        ColumnSpec(name="ref", type="text"),
    ))

    result = validate([{"id": "NA", "amount": "NA", "ref": "NA"}], contract)

    row = result.valid.iloc[0]
    assert row["id"] == "NA"  # a valid value, as Namibia is in a country column
    assert pd.isna(row["amount"])
    assert row["ref"] == "NA"


def test_malformed_rows_become_rejections_with_their_raw_fields() -> None:
    validator = RecordValidator("ds", table(), SOURCE, {}, lambda *_: None)
    batch = raw([{"id": "a"}])
    batch.malformed.append(MalformedRow(2, 3, ["x", "y"], "malformed_row: expected 5 fields, found 2"))

    result = validator.validate(batch)

    record = result.rejected[0]
    assert record["rejection_reason"] == "malformed_row"
    assert record["original_record"] == {"_raw": ["x", "y"], "_line_number": 3}
    assert result.rule_failures["malformed_row"] == 1


def test_rejection_id_is_deterministic_across_runs() -> None:
    first = validate([{"id": "", "amount": "x"}]).rejected[0]["rejection_id"]
    second = validate([{"id": "", "amount": "x"}]).rejected[0]["rejection_id"]

    assert first == second


def test_missing_required_column_is_schema_drift() -> None:
    with pytest.raises(SchemaDriftError, match=r"missing required columns: \['id'\]"):
        check_columns(["amount", "qty", "at", "ref"], table(), "t.csv")


def test_missing_optional_column_is_reported_and_reads_as_null() -> None:
    notes = check_columns(["id", "qty", "at", "ref"], table(), "t.csv")
    assert notes["missing_optional"] == ["amount"]

    frame = pd.DataFrame({"id": ["a"], "source_row_number": pd.array([1], dtype="int64")}, dtype=object)
    frame["source_row_number"] = frame["source_row_number"].astype("int64")
    result = RecordValidator("ds", table(), SOURCE, {}, lambda *_: None).validate(RawBatch(frame))
    assert result.rejected == []
    assert pd.isna(result.valid["amount"].iloc[0])


def test_unexpected_column_is_ignored_by_default_and_fatal_when_configured() -> None:
    columns = ["id", "amount", "qty", "at", "ref", "surprise"]

    assert check_columns(columns, table(), "t.csv")["unexpected"] == ["surprise"]

    strict = table(source=SourceSpec(pattern="*.csv", on_unexpected_column="fail"))
    with pytest.raises(SchemaDriftError, match="unexpected columns"):
        check_columns(columns, strict, "t.csv")


def test_contract_rejects_rule_on_unknown_column() -> None:
    with pytest.raises(ValueError, match="unknown columns"):
        table(rules=(RuleSpec(name="r", check="range", column="nope", min=0),))


def test_custom_rule_with_no_implementation_is_a_configuration_error() -> None:
    from src.transformers.base import BaseTransformer

    with pytest.raises(ConfigurationError, match="no custom rule"):
        BaseTransformer().custom_rule("absent", pd.DataFrame())
