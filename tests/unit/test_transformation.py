"""Derived fields, with expected values worked out by hand."""

import pandas as pd
import pytest

from src.transformers.base import load_transformer
from src.core.exceptions import ConfigurationError
from src.transformers.hotel_transformer import HotelTransformer
from src.transformers.olist_transformer import OlistTransformer


def hotel_frame(**overrides) -> pd.DataFrame:
    row = {
        "arrival_date_year": 2016, "arrival_date_month": "March", "arrival_date_day_of_month": 5,
        "lead_time": 10, "stays_in_weekend_nights": 1, "stays_in_week_nights": 2, "adults": 2, "children": 1,
        "babies": 0, "adr": 100.5, "is_canceled": 0, "meal": "BB", "country": "PRT",
        "reservation_status": "Check-Out", "reservation_status_date": pd.Timestamp("2016-03-08"),
        "source_file": "hotel_bookings.csv", "source_row_number": 7,
    }
    frame = pd.DataFrame([{**row, **overrides}])
    for column in ("arrival_date_year", "arrival_date_day_of_month", "lead_time", "stays_in_weekend_nights",
                   "stays_in_week_nights", "adults", "children", "babies", "is_canceled"):
        frame[column] = frame[column].astype("Int64")
    return frame


def hotel(**overrides) -> pd.Series:
    return HotelTransformer().transform_hotel_bookings(hotel_frame(**overrides)).iloc[0]


def test_hotel_derived_fields() -> None:
    row = hotel()

    assert row["arrival_date"] == pd.Timestamp("2016-03-05")
    assert row["booking_date"] == pd.Timestamp("2016-02-24")  # across the leap day: 10 days back
    assert row["total_nights"] == 3
    assert row["total_guests"] == 3
    assert row["estimated_booking_value"] == 301.5
    assert row["realized_booking_value"] == 301.5


def test_canceled_booking_keeps_its_estimate_but_realizes_nothing() -> None:
    row = hotel(is_canceled=1)

    assert row["estimated_booking_value"] == 301.5
    assert row["realized_booking_value"] == 0.0


def test_unknown_children_count_as_zero_guests_but_stay_null() -> None:
    row = hotel(children=None)

    assert row["total_guests"] == 2
    assert pd.isna(row["children"])


def test_day_use_stay_has_zero_nights_and_zero_value() -> None:
    row = hotel(stays_in_weekend_nights=0, stays_in_week_nights=0, adr=0.0)

    assert (row["total_nights"], row["estimated_booking_value"]) == (0, 0.0)


def test_undefined_meal_maps_to_sc_and_the_original_is_kept() -> None:
    row = hotel(meal="Undefined")

    assert (row["meal"], row["meal_plan"]) == ("Undefined", "SC")


def test_record_id_is_stable_and_depends_on_file_and_position() -> None:
    first = hotel()["record_id"]

    assert hotel()["record_id"] == first
    assert hotel(source_row_number=8)["record_id"] != first
    assert hotel(source_file="other.csv")["record_id"] != first


def test_invalid_arrival_date_rule_finds_impossible_dates() -> None:
    frame = hotel_frame(arrival_date_month="February", arrival_date_day_of_month=30)

    assert HotelTransformer().rule_invalid_arrival_date(frame).tolist() == [True]


def test_leap_day_is_a_valid_arrival_date_only_in_a_leap_year() -> None:
    leap = hotel_frame(arrival_date_year=2016, arrival_date_month="February", arrival_date_day_of_month=29)
    other = hotel_frame(arrival_date_year=2017, arrival_date_month="February", arrival_date_day_of_month=29)

    assert HotelTransformer().rule_invalid_arrival_date(leap).tolist() == [False]
    assert HotelTransformer().rule_invalid_arrival_date(other).tolist() == [True]


def test_cancel_status_mismatch_detects_both_directions() -> None:
    rule = HotelTransformer().rule_cancel_status_mismatch

    assert rule(hotel_frame(is_canceled=1, reservation_status="Check-Out")).tolist() == [True]
    assert rule(hotel_frame(is_canceled=0, reservation_status="Canceled")).tolist() == [True]
    assert rule(hotel_frame(is_canceled=1, reservation_status="No-Show")).tolist() == [False]


def order_frame(**overrides) -> pd.DataFrame:
    row = {
        "order_status": " Delivered ",
        "order_purchase_timestamp": pd.Timestamp("2018-01-01 12:00:00"),
        "order_approved_at": pd.Timestamp("2018-01-01 13:00:00"),
        "order_delivered_carrier_date": pd.Timestamp("2018-01-02 12:00:00"),
        "order_delivered_customer_date": pd.Timestamp("2018-01-03 12:00:00"),
        "order_estimated_delivery_date": pd.Timestamp("2018-01-03 00:00:00"),
    }
    frame = pd.DataFrame([{**row, **overrides}])
    for column in row:
        if column != "order_status":
            frame[column] = pd.to_datetime(frame[column])
    return frame


def test_order_fields_from_lifecycle_timestamps() -> None:
    row = OlistTransformer().transform_orders(order_frame()).iloc[0]

    assert row["order_status"] == "delivered"
    assert row["source_event_ts"] == pd.Timestamp("2018-01-03 12:00:00")
    assert row["delivery_days"] == 2.0


def test_delivery_on_the_promised_date_is_not_late_whatever_the_hour() -> None:
    row = OlistTransformer().transform_orders(order_frame()).iloc[0]

    assert row["is_late_delivery"] == 0


def test_delivery_the_day_after_the_promise_is_late() -> None:
    frame = order_frame(order_delivered_customer_date=pd.Timestamp("2018-01-04 00:00:01"))

    assert OlistTransformer().transform_orders(frame).iloc[0]["is_late_delivery"] == 1


def test_undelivered_order_has_null_delivery_fields_and_version_from_other_events() -> None:
    frame = order_frame(order_delivered_customer_date=pd.NaT)

    row = OlistTransformer().transform_orders(frame).iloc[0]

    assert pd.isna(row["delivery_days"]) and pd.isna(row["is_late_delivery"])
    assert row["source_event_ts"] == pd.Timestamp("2018-01-02 12:00:00")


def test_item_total_avoids_float_noise() -> None:
    frame = pd.DataFrame({"price": [0.1], "freight_value": [0.2]})

    assert OlistTransformer().transform_order_items(frame)["item_total"].iloc[0] == 0.3


def test_accented_and_plain_city_names_normalize_to_the_same_value() -> None:
    frame = pd.DataFrame({"geolocation_city": ["são paulo", "sao paulo", " SÃO PAULO "]}, dtype=object)

    result = OlistTransformer().transform_geolocation(frame)

    assert result["city_normalized"].tolist() == ["sao paulo"] * 3
    assert result["geolocation_city"].tolist()[0] == "são paulo"


def test_transformation_is_deterministic() -> None:
    first = HotelTransformer().transform_hotel_bookings(hotel_frame())
    second = HotelTransformer().transform_hotel_bookings(hotel_frame())

    pd.testing.assert_frame_equal(first, second)


@pytest.mark.parametrize("path", ["os:system", "src.transformers.nope:Nope", "src.transformers.base:Absent"])
def test_transformer_path_must_resolve_inside_the_project(path: str) -> None:
    with pytest.raises(ConfigurationError):
        load_transformer(path)
