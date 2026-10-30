"""Check published tables against the raw files, sharing no code with the pipeline.

The pipeline reconciles itself, but a bug in shared code could fool that. This script
recomputes expected results with plain pandas from the raw CSVs and compares them to SQL
results. Output: reports/independent_verification.json.

Usage:
    python scripts/verify_real_data.py
"""

import json
import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.config import load_settings  # noqa: E402
from src.database.connection import create_db_engine  # noqa: E402

RAW = ROOT / "data" / "raw"
TOLERANCE = 0.01
MONTHS = "January February March April May June July August September October November December".split()


def read(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8")


def hotel_expectations() -> dict:
    frame = read(RAW / "hotel_bookings" / "hotel_bookings.csv")
    number = lambda name: pd.to_numeric(frame[name], errors="coerce")
    guests = number("adults") + number("children").fillna(0) + number("babies")
    nights = number("stays_in_weekend_nights") + number("stays_in_week_nights")
    rejected = (number("adr") < 0) | (guests == 0)
    kept = frame[~rejected]
    kept_nights, kept_guests = nights[~rejected], guests[~rejected]
    adr = pd.to_numeric(kept["adr"])
    return {
        "SELECT count(*) FROM hotel.hotel_bookings": len(kept),
        "SELECT count(*) FROM ops.rejected_records WHERE dataset_name = 'hotel_bookings' AND resolved_at IS NULL": int(rejected.sum()),
        "SELECT sum(is_canceled) FROM hotel.hotel_bookings": int(pd.to_numeric(kept["is_canceled"]).sum()),
        "SELECT sum(total_guests) FROM hotel.hotel_bookings": int(kept_guests.sum()),
        "SELECT sum(total_nights) FROM hotel.hotel_bookings": int(kept_nights.sum()),
        "SELECT sum(adr) FROM hotel.hotel_bookings": round(float(adr.sum()), 2),
        "SELECT sum(estimated_booking_value) FROM hotel.hotel_bookings": round(float((adr * kept_nights).round(2).sum()), 2),
        "SELECT count(*) FROM hotel.hotel_bookings WHERE duplicate_occurrence > 1": int(frame[frame.index.isin(kept.index)].duplicated().sum()),
        "SELECT count(*) FROM hotel.hotel_bookings WHERE total_nights = 0": int((kept_nights == 0).sum()),
    }


def olist_expectations() -> dict:
    folder = RAW / "olist"
    orders = read(folder / "olist_orders_dataset.csv")
    items = read(folder / "olist_order_items_dataset.csv")
    payments = read(folder / "olist_order_payments_dataset.csv")
    reviews = read(folder / "olist_order_reviews_dataset.csv")
    geolocation = read(folder / "olist_geolocation_dataset.csv")
    price, freight = pd.to_numeric(items["price"]), pd.to_numeric(items["freight_value"])
    paid = pd.to_numeric(payments["payment_value"])
    by_order = pd.DataFrame({"order_id": items["order_id"], "total": price + freight}).groupby("order_id")["total"].sum()
    paid_by_order = pd.DataFrame({"order_id": payments["order_id"], "paid": paid}).groupby("order_id")["paid"].sum()
    joined = pd.concat([by_order, paid_by_order], axis=1, join="inner")
    return {
        "SELECT count(*) FROM olist.orders": len(orders),
        "SELECT count(DISTINCT order_id) FROM olist.order_items": items["order_id"].nunique(),
        "SELECT count(*) FROM olist.order_items": len(items),
        "SELECT count(*) FROM olist.payments": len(payments),
        "SELECT count(*) FROM olist.reviews": len(reviews),
        "SELECT count(*) FROM olist.customers": len(read(folder / "olist_customers_dataset.csv")),
        "SELECT count(*) FROM olist.products": len(read(folder / "olist_products_dataset.csv")),
        "SELECT count(*) FROM olist.sellers": len(read(folder / "olist_sellers_dataset.csv")),
        "SELECT count(*) FROM olist.geolocation": len(geolocation.drop_duplicates()),
        "SELECT sum(duplicate_count) FROM olist.geolocation": len(geolocation),
        "SELECT sum(price) FROM olist.order_items": round(float(price.sum()), 2),
        "SELECT sum(freight_value) FROM olist.order_items": round(float(freight.sum()), 2),
        "SELECT sum(payment_value) FROM olist.payments": round(float(paid.sum()), 2),
        "SELECT sum(items_total) FROM olist.order_summary": round(float(price.sum()), 2),
        "SELECT sum(payment_total) FROM olist.order_summary": round(float(paid.sum()), 2),
        "SELECT count(*) FROM olist.order_summary": len(orders),
        "SELECT count(*) FROM olist.order_summary WHERE item_count = 0": len(orders) - items["order_id"].nunique(),
        "SELECT count(*) FROM olist.order_summary WHERE payment_matches_order = 0": int(((joined["paid"] - joined["total"]).round(2).abs() > 0.05).sum()),
    }


def main() -> int:
    engine = create_db_engine(load_settings().database)
    results = []
    with engine.connect() as conn:
        for dataset, expectations in (("hotel_bookings", hotel_expectations()), ("olist", olist_expectations())):
            for sql, expected in expectations.items():
                actual = conn.execute(text(sql)).scalar()
                actual = float(actual) if actual is not None else None
                results.append({
                    "dataset": dataset, "query": sql, "expected_from_raw_file": expected, "actual_in_database": actual,
                    "match": actual is not None and abs(actual - expected) <= TOLERANCE,
                })
    engine.dispose()
    failures = [row for row in results if not row["match"]]
    output = ROOT / "reports" / "independent_verification.json"
    output.write_text(json.dumps({"checks": len(results), "failed": len(failures), "results": results}, indent=2), encoding="utf-8")
    print(f"{len(results) - len(failures)} of {len(results)} checks match. Wrote {output}")
    for row in failures:
        print(f"MISMATCH {row['query']}: expected {row['expected_from_raw_file']}, found {row['actual_in_database']}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
