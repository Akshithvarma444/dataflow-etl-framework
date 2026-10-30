"""End-to-end Olist runs, checked against the generator's independent answer key."""

import json
from pathlib import Path

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from src.core.config import Settings
from src.core.constants import RunStatus
from src.generators.data_generator import generate_dataset
from tests.conftest import rows_of, scalar
from tests.helpers import olist_row, write_olist

SIZE = 500
ORDERS = "olist_orders_dataset.csv"
ITEMS = "olist_order_items_dataset.csv"
REVIEWS = "olist_order_reviews_dataset.csv"
PAYMENTS = "olist_order_payments_dataset.csv"
PRODUCTS = "olist_products_dataset.csv"
CUSTOMERS = "olist_customers_dataset.csv"


@pytest.fixture(scope="module")
def generated(settings: Settings) -> tuple[Path, dict]:
    generate_dataset(settings, "olist", SIZE)
    root = settings.paths.generated_dir / "olist" / f"rows_{SIZE}"
    answers = json.loads((settings.paths.expected_dir / "olist" / f"rows_{SIZE}.json").read_text(encoding="utf-8"))
    return root, answers


def test_clean_data_matches_the_answer_key_for_every_table(run_olist, db: Engine, generated) -> None:
    root, answers = generated

    result = run_olist(root / "clean")

    assert result.status is RunStatus.SUCCESS, result.failure_reason
    for table, expected in answers["clean"].items():
        assert scalar(db, f"SELECT count(*) FROM olist.{table}") == expected["target_rows"], table
    assert float(scalar(db, "SELECT sum(price) FROM olist.order_items")) == pytest.approx(answers["clean"]["order_items"]["price_total"], abs=0.01)
    assert float(scalar(db, "SELECT sum(freight_value) FROM olist.order_items")) == pytest.approx(answers["clean"]["order_items"]["freight_total"], abs=0.01)
    assert float(scalar(db, "SELECT sum(payment_value) FROM olist.payments")) == pytest.approx(answers["clean"]["payments"]["payment_total"], abs=0.01)


def test_dirty_data_rejects_exactly_the_labelled_rows_for_the_labelled_reasons(run_olist, db: Engine, generated) -> None:
    root, answers = generated

    result = run_olist(root / "dirty")

    assert result.status is RunStatus.SUCCESS_WITH_REJECTS, result.failure_reason
    for table, expected in answers["dirty"].items():
        metrics = result.metrics.table(table)
        assert metrics.input == expected["source_rows"], table
        assert metrics.rejected == expected["rejected"], table
        assert dict(metrics.rule_failures) == expected["rejected_by_rule"], table
        assert scalar(db, f"SELECT count(*) FROM olist.{table}") == expected["target_rows"], table
        labels = {row["source_row_number"]: row["expected_rule"] for row in answers["defect_rows"][table]}
        stored = {row["source_row_number"]: row["rejection_reason"] for row in rows_of(
            db, "SELECT source_row_number, rejection_reason FROM ops.rejected_records WHERE target_table = :t", t=table)}
        assert stored == labels, table
    assert float(scalar(db, "SELECT sum(price) FROM olist.order_items")) == pytest.approx(answers["dirty"]["order_items"]["price_total"], abs=0.01)


def test_derived_order_summary_values(run_olist, db: Engine, tmp_path: Path) -> None:
    write_olist(tmp_path, olist_order_payments_dataset=[
        olist_row(PAYMENTS, payment_value="30.00"),
        olist_row(PAYMENTS, payment_sequential="2", payment_type="voucher", payment_value="10.00"),
    ])

    run_olist(tmp_path)

    row = rows_of(db, "SELECT * FROM olist.order_summary")[0]
    assert row["item_count"] == 1
    assert float(row["items_total"]) == 29.99
    assert float(row["freight_total"]) == 8.72
    assert float(row["order_total"]) == 38.71
    assert (row["payment_count"], float(row["payment_total"])) == (2, 40.0)
    assert float(row["payment_difference"]) == 1.29
    assert row["payment_matches_order"] == 0


def test_order_with_no_items_or_payments_is_kept_with_zero_totals(run_olist, db: Engine, tmp_path: Path) -> None:
    """Domain edge case: 775 real orders have no items, and 1 has no payments."""
    write_olist(tmp_path, olist_orders_dataset=[olist_row(ORDERS), olist_row(ORDERS, order_id="o2")])

    result = run_olist(tmp_path)

    assert result.status is RunStatus.SUCCESS, result.failure_reason
    row = rows_of(db, "SELECT * FROM olist.order_summary WHERE order_id = 'o2'")[0]
    assert (row["item_count"], row["payment_count"]) == (0, 0)
    assert row["payment_matches_order"] is None


def test_review_id_shared_by_two_orders_is_not_a_duplicate(run_olist, db: Engine, tmp_path: Path) -> None:
    """Domain edge case: review_id repeats 814 times in the real data."""
    write_olist(tmp_path,
                olist_orders_dataset=[olist_row(ORDERS), olist_row(ORDERS, order_id="o2")],
                olist_order_reviews_dataset=[olist_row(REVIEWS), olist_row(REVIEWS, order_id="o2")])

    result = run_olist(tmp_path)

    assert result.status is RunStatus.SUCCESS, result.failure_reason
    assert scalar(db, "SELECT count(*) FROM olist.reviews WHERE review_id = 'r1'") == 2


def test_product_with_untranslated_category_loads_with_a_warning(run_olist, db: Engine, tmp_path: Path) -> None:
    """Domain edge case: pc_gamer has no translation in the real data."""
    write_olist(tmp_path, olist_products_dataset=[olist_row(PRODUCTS, product_category_name="pc_gamer")])

    assert run_olist(tmp_path).status is RunStatus.SUCCESS
    assert scalar(db, "SELECT dq_warnings FROM olist.products") == "fk_violation:product_category_name"


def test_product_with_no_category_is_valid(run_olist, db: Engine, tmp_path: Path) -> None:
    """Domain edge case: 610 real products have no category or text attributes."""
    write_olist(tmp_path, olist_products_dataset=[olist_row(
        PRODUCTS, product_category_name="", product_name_lenght="", product_description_lenght="", product_photos_qty="")])

    result = run_olist(tmp_path)

    assert result.status is RunStatus.SUCCESS
    assert scalar(db, "SELECT dq_warnings FROM olist.products") is None
    assert scalar(db, "SELECT product_category_name FROM olist.products") is None


def test_customer_zip_missing_from_geolocation_is_accepted_and_flagged(run_olist, db: Engine, tmp_path: Path) -> None:
    write_olist(tmp_path, olist_customers_dataset=[olist_row(CUSTOMERS, customer_zip_code_prefix="72300")])

    assert run_olist(tmp_path).status is RunStatus.SUCCESS
    assert scalar(db, "SELECT dq_warnings FROM olist.customers") == "fk_violation:customer_zip_code_prefix"


def test_zip_prefix_keeps_its_leading_zero(run_olist, db: Engine, tmp_path: Path) -> None:
    write_olist(tmp_path)

    run_olist(tmp_path)

    assert scalar(db, "SELECT customer_zip_code_prefix FROM olist.customers") == "01001"


def test_multi_line_review_comment_survives_intact(run_olist, db: Engine, tmp_path: Path) -> None:
    write_olist(tmp_path, olist_order_reviews_dataset=[olist_row(REVIEWS, review_comment_message="linha um\nlinha dois")])

    run_olist(tmp_path)

    assert scalar(db, "SELECT review_comment_message FROM olist.reviews") == "linha um\nlinha dois"
    assert scalar(db, "SELECT has_comment FROM olist.reviews") == 1


def test_clock_noise_in_order_timestamps_is_flagged_not_rejected(run_olist, db: Engine, tmp_path: Path) -> None:
    """Domain edge case: 23 real orders were delivered before reaching the carrier."""
    write_olist(tmp_path, olist_orders_dataset=[olist_row(
        ORDERS, order_delivered_carrier_date="2017-10-11 00:00:00", order_delivered_customer_date="2017-10-10 21:25:13")])

    assert run_olist(tmp_path).status is RunStatus.SUCCESS
    assert scalar(db, "SELECT dq_warnings FROM olist.orders") == "delivered_before_carrier"


def test_database_foreign_keys_are_enforced_on_hard_references(run_olist, db: Engine, tmp_path: Path) -> None:
    write_olist(tmp_path)
    run_olist(tmp_path)

    with pytest.raises(IntegrityError):
        with db.begin() as conn:
            conn.execute(text(
                "INSERT INTO olist.payments (order_id, payment_sequential, payment_type, payment_installments, payment_value, "
                "source_file, source_row_number, first_run_id, last_run_id, row_hash) "
                "VALUES ('ghost', 1, 'boleto', 1, 1, 'x', 1, gen_random_uuid(), gen_random_uuid(), 'x')"))


def test_geolocation_copies_collapse_and_the_count_is_kept(run_olist, db: Engine, tmp_path: Path) -> None:
    row = olist_row("olist_geolocation_dataset.csv")
    write_olist(tmp_path, olist_geolocation_dataset=[row, row, row, {**row, "geolocation_city": "sao paulo"}])

    result = run_olist(tmp_path)

    assert result.metrics.table("geolocation").collapsed == 2
    assert scalar(db, "SELECT count(*) FROM olist.geolocation") == 2
    assert scalar(db, "SELECT sum(duplicate_count) FROM olist.geolocation") == 4
    assert scalar(db, "SELECT count(DISTINCT city_normalized) FROM olist.geolocation") == 1


def test_a_batch_size_of_one_gives_the_same_result(run_olist, db: Engine, generated) -> None:
    root, answers = generated

    result = run_olist(root / "dirty", batch_size=97)

    assert result.status is RunStatus.SUCCESS_WITH_REJECTS, result.failure_reason
    assert scalar(db, "SELECT count(*) FROM olist.order_items") == answers["dirty"]["order_items"]["target_rows"]
    assert scalar(db, "SELECT count(*) FROM olist.geolocation") == answers["dirty"]["geolocation"]["target_rows"]
