"""Hotel booking derived fields and the cross-field rules YAML cannot express."""

import hashlib

import pandas as pd

from src.core.constants import SOURCE_FILE, SOURCE_ROW
from src.transformers.base import BaseTransformer

MONTHS = {
    name: number
    for number, name in enumerate(
        ["January", "February", "March", "April", "May", "June", "July", "August",
         "September", "October", "November", "December"],
        start=1,
    )
}
LARGE_PARTY = 10
MAX_PLAUSIBLE_DAYS = 3660  # matches the implausible_* rules in the contract


def _arrival_date(frame: pd.DataFrame) -> pd.Series:
    """Build the date from its parts; impossible dates such as 30 February become null."""
    return pd.to_datetime(
        {
            "year": frame["arrival_date_year"].astype("float64"),
            "month": frame["arrival_date_month"].map(MONTHS).astype("float64"),
            "day": frame["arrival_date_day_of_month"].astype("float64"),
        },
        errors="coerce",
    )


def _total_nights(frame: pd.DataFrame) -> pd.Series:
    return frame["stays_in_weekend_nights"] + frame["stays_in_week_nights"]


def _total_guests(frame: pd.DataFrame) -> pd.Series:
    # Documented rule: unknown children (4 real rows) count as 0, the column stays null.
    return frame["adults"] + frame["children"].fillna(0) + frame["babies"]


class HotelTransformer(BaseTransformer):
    def transform_hotel_bookings(self, frame: pd.DataFrame) -> pd.DataFrame:
        frame["record_id"] = [
            hashlib.sha256(f"{name}:{row}".encode("utf-8")).hexdigest()
            for name, row in zip(frame[SOURCE_FILE], frame[SOURCE_ROW])
        ]
        frame["country"] = frame["country"].astype(object).map(
            lambda value: value.strip().upper() if isinstance(value, str) else None
        ).astype(object)
        frame["arrival_date"] = _arrival_date(frame)
        frame["booking_date"] = frame["arrival_date"] - pd.to_timedelta(frame["lead_time"].astype("int64"), unit="D")
        frame["total_nights"] = _total_nights(frame)
        frame["total_guests"] = _total_guests(frame)
        frame["meal_plan"] = frame["meal"].replace({"Undefined": "SC"})
        value = (frame["adr"] * frame["total_nights"].astype("float64")).round(2)
        frame["estimated_booking_value"] = value
        frame["realized_booking_value"] = value.where(frame["is_canceled"].astype("int64") == 0, 0.0)
        return frame

    def rule_invalid_arrival_date(self, frame: pd.DataFrame) -> pd.Series:
        return _arrival_date(frame).isna()

    def rule_zero_guests(self, frame: pd.DataFrame) -> pd.Series:
        return _total_guests(frame) == 0

    def rule_cancel_status_mismatch(self, frame: pd.DataFrame) -> pd.Series:
        canceled_status = frame["reservation_status"].isin(["Canceled", "No-Show"])
        known = frame["is_canceled"].notna() & frame["reservation_status"].notna()
        return known & ((frame["is_canceled"] == 1).fillna(False) != canceled_status)

    def rule_zero_night_stay(self, frame: pd.DataFrame) -> pd.Series:
        return _total_nights(frame) == 0

    def rule_large_party(self, frame: pd.DataFrame) -> pd.Series:
        return _total_guests(frame) > LARGE_PARTY

    def rule_checkout_date_mismatch(self, frame: pd.DataFrame) -> pd.Series:
        nights = _total_nights(frame).astype("float64")
        # Values rejected by the implausible_* rules would otherwise overflow a date here.
        nights = nights.where(nights.abs() <= MAX_PLAUSIBLE_DAYS)
        expected = _arrival_date(frame) + pd.to_timedelta(nights, unit="D")
        return (frame["reservation_status"] == "Check-Out") & (frame["reservation_status_date"] != expected)
