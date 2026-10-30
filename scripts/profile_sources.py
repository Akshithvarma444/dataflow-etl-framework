"""Profile raw source files and write reports/source_profile.json.

Everything in the schema and rules is based on this output, so that keys and
nullability are verified against the data, not assumed.

Usage:
    python scripts/profile_sources.py
"""

import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
OUTPUT = ROOT / "reports" / "source_profile.json"

CANDIDATE_KEYS = {
    "olist_customers_dataset.csv": [["customer_id"], ["customer_unique_id"]],
    "olist_geolocation_dataset.csv": [["geolocation_zip_code_prefix"]],
    "olist_order_items_dataset.csv": [["order_id", "order_item_id"]],
    "olist_order_payments_dataset.csv": [["order_id", "payment_sequential"]],
    "olist_order_reviews_dataset.csv": [["review_id"], ["order_id"], ["review_id", "order_id"]],
    "olist_orders_dataset.csv": [["order_id"]],
    "olist_products_dataset.csv": [["product_id"]],
    "olist_sellers_dataset.csv": [["seller_id"]],
    "product_category_name_translation.csv": [["product_category_name"]],
}

REFERENCES = [
    ("orders.customer_id", "olist_orders_dataset.csv", "customer_id", "olist_customers_dataset.csv", "customer_id"),
    ("items.order_id", "olist_order_items_dataset.csv", "order_id", "olist_orders_dataset.csv", "order_id"),
    ("items.product_id", "olist_order_items_dataset.csv", "product_id", "olist_products_dataset.csv", "product_id"),
    ("items.seller_id", "olist_order_items_dataset.csv", "seller_id", "olist_sellers_dataset.csv", "seller_id"),
    ("payments.order_id", "olist_order_payments_dataset.csv", "order_id", "olist_orders_dataset.csv", "order_id"),
    ("reviews.order_id", "olist_order_reviews_dataset.csv", "order_id", "olist_orders_dataset.csv", "order_id"),
    ("orders_without_items", "olist_orders_dataset.csv", "order_id", "olist_order_items_dataset.csv", "order_id"),
    ("orders_without_payments", "olist_orders_dataset.csv", "order_id", "olist_order_payments_dataset.csv", "order_id"),
    ("orders_without_reviews", "olist_orders_dataset.csv", "order_id", "olist_order_reviews_dataset.csv", "order_id"),
    ("products.category", "olist_products_dataset.csv", "product_category_name", "product_category_name_translation.csv", "product_category_name"),
    ("customers.zip", "olist_customers_dataset.csv", "customer_zip_code_prefix", "olist_geolocation_dataset.csv", "geolocation_zip_code_prefix"),
    ("sellers.zip", "olist_sellers_dataset.csv", "seller_zip_code_prefix", "olist_geolocation_dataset.csv", "geolocation_zip_code_prefix"),
]


def _column_profile(series: pd.Series) -> dict:
    text = series.dropna()
    numeric = pd.to_numeric(text, errors="coerce")
    profile = {
        "null_count": int(series.isna().sum()),
        "distinct": int(text.nunique()),
        "max_length": int(text.str.len().max()) if len(text) else 0,
    }
    if len(text) and numeric.notna().all():
        profile.update(min=float(numeric.min()), max=float(numeric.max()))
    else:
        profile["sample"] = text.drop_duplicates().head(6).tolist()
    if 0 < profile["distinct"] <= 15:
        profile["value_counts"] = {str(k): int(v) for k, v in text.value_counts().items()}
    return profile


def profile_file(path: Path) -> tuple[dict, pd.DataFrame]:
    # keep_default_na=False would hide real blanks, so only "" counts as null here;
    # literal tokens such as "NULL" stay visible as values.
    frame = pd.read_csv(path, dtype=str, na_values=[""], keep_default_na=False, encoding="utf-8")
    result = {
        "rows": len(frame),
        "exact_duplicate_rows": int(frame.duplicated().sum()),
        "columns": {column: _column_profile(frame[column]) for column in frame.columns},
    }
    keys = CANDIDATE_KEYS.get(path.name, [])
    result["candidate_keys"] = {
        "+".join(key): {
            "duplicate_rows": int(frame.duplicated(subset=key).sum()),
            "null_rows": int(frame[key].isna().any(axis=1).sum()),
        }
        for key in keys
    }
    return result, frame


def main() -> None:
    profile: dict = {"files": {}, "references": {}}
    frames: dict[str, pd.DataFrame] = {}
    for path in sorted(RAW.glob("*/*.csv")):
        profile["files"][path.name], frames[path.name] = profile_file(path)

    for name, child, child_col, parent, parent_col in REFERENCES:
        if child in frames and parent in frames:
            values = frames[child][child_col].dropna()
            missing = values[~values.isin(set(frames[parent][parent_col].dropna()))]
            profile["references"][name] = {
                "orphan_rows": len(missing),
                "orphan_distinct": int(missing.nunique()),
                "sample": missing.drop_duplicates().head(5).tolist(),
            }

    OUTPUT.write_text(json.dumps(profile, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    main()
