"""Olist derived fields and normalization. All rules are deterministic and documented."""

import unicodedata

import pandas as pd

from src.transformers.base import BaseTransformer


def _strip_accents(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _normalize_city(cities: pd.Series) -> pd.Series:
    return cities.astype(object).str.strip().str.lower()


class OlistTransformer(BaseTransformer):
    def transform_geolocation(self, frame: pd.DataFrame) -> pd.DataFrame:
        lowered = _normalize_city(frame["geolocation_city"])
        # A mapping over distinct cities: about 8,000 values across a million rows.
        mapping = {city: _strip_accents(city) for city in lowered.unique()}
        frame["city_normalized"] = lowered.map(mapping)
        return frame

    def transform_customers(self, frame: pd.DataFrame) -> pd.DataFrame:
        frame["customer_city"] = _normalize_city(frame["customer_city"])
        return frame

    def transform_sellers(self, frame: pd.DataFrame) -> pd.DataFrame:
        frame["seller_city"] = _normalize_city(frame["seller_city"])
        return frame

    def transform_orders(self, frame: pd.DataFrame) -> pd.DataFrame:
        frame["order_status"] = frame["order_status"].str.strip().str.lower()
        lifecycle = ["order_purchase_timestamp", "order_approved_at",
                     "order_delivered_carrier_date", "order_delivered_customer_date"]
        frame["source_event_ts"] = frame[lifecycle].max(axis=1)
        delivered = frame["order_delivered_customer_date"]
        elapsed = delivered - frame["order_purchase_timestamp"]
        frame["delivery_days"] = (elapsed.dt.total_seconds() / 86400).round(2)
        # The promise is a date at midnight, so delivery any time that day is on time.
        late = delivered.dt.normalize() > frame["order_estimated_delivery_date"]
        frame["is_late_delivery"] = late.astype("Int64").where(delivered.notna())
        return frame

    def transform_order_items(self, frame: pd.DataFrame) -> pd.DataFrame:
        frame["item_total"] = (frame["price"] + frame["freight_value"]).round(2)
        return frame

    def transform_reviews(self, frame: pd.DataFrame) -> pd.DataFrame:
        written = frame["review_comment_message"].notna() | frame["review_comment_title"].notna()
        frame["has_comment"] = written.astype("Int64")
        return frame

    def rule_delivered_without_date(self, frame: pd.DataFrame) -> pd.Series:
        return (frame["order_status"] == "delivered") & frame["order_delivered_customer_date"].isna()
