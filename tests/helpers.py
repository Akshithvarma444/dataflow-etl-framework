"""Small builders for test input files. Values are valid unless a test overrides them."""

import csv
from pathlib import Path

HOTEL_ROW = {
    "hotel": "City Hotel", "is_canceled": "0", "lead_time": "10", "arrival_date_year": "2016",
    "arrival_date_month": "March", "arrival_date_week_number": "10", "arrival_date_day_of_month": "5",
    "stays_in_weekend_nights": "1", "stays_in_week_nights": "2", "adults": "2", "children": "1", "babies": "0",
    "meal": "BB", "country": "PRT", "market_segment": "Direct", "distribution_channel": "Direct",
    "is_repeated_guest": "0", "previous_cancellations": "0", "previous_bookings_not_canceled": "0",
    "reserved_room_type": "A", "assigned_room_type": "A", "booking_changes": "0", "deposit_type": "No Deposit",
    "agent": "NULL", "company": "NULL", "days_in_waiting_list": "0", "customer_type": "Transient", "adr": "100.50",
    "required_car_parking_spaces": "0", "total_of_special_requests": "0", "reservation_status": "Check-Out",
    "reservation_status_date": "2016-03-08",
}

OLIST_FILES = {
    "olist_geolocation_dataset.csv": [{
        "geolocation_zip_code_prefix": "01001", "geolocation_lat": "-23.5", "geolocation_lng": "-46.6",
        "geolocation_city": "são paulo", "geolocation_state": "SP"}],
    "product_category_name_translation.csv": [{
        "product_category_name": "perfumaria", "product_category_name_english": "perfumery"}],
    "olist_customers_dataset.csv": [{
        "customer_id": "c1", "customer_unique_id": "u1", "customer_zip_code_prefix": "01001",
        "customer_city": " Sao Paulo ", "customer_state": "SP"}],
    "olist_sellers_dataset.csv": [{
        "seller_id": "s1", "seller_zip_code_prefix": "01001", "seller_city": "sao paulo", "seller_state": "SP"}],
    "olist_products_dataset.csv": [{
        "product_id": "p1", "product_category_name": "perfumaria", "product_name_lenght": "40",
        "product_description_lenght": "287", "product_photos_qty": "1", "product_weight_g": "225",
        "product_length_cm": "16", "product_height_cm": "10", "product_width_cm": "14"}],
    "olist_orders_dataset.csv": [{
        "order_id": "o1", "customer_id": "c1", "order_status": "delivered",
        "order_purchase_timestamp": "2017-10-02 10:56:33", "order_approved_at": "2017-10-02 11:07:15",
        "order_delivered_carrier_date": "2017-10-04 19:55:00", "order_delivered_customer_date": "2017-10-10 21:25:13",
        "order_estimated_delivery_date": "2017-10-18 00:00:00"}],
    "olist_order_items_dataset.csv": [{
        "order_id": "o1", "order_item_id": "1", "product_id": "p1", "seller_id": "s1",
        "shipping_limit_date": "2017-10-06 11:07:15", "price": "29.99", "freight_value": "8.72"}],
    "olist_order_payments_dataset.csv": [{
        "order_id": "o1", "payment_sequential": "1", "payment_type": "credit_card",
        "payment_installments": "1", "payment_value": "38.71"}],
    "olist_order_reviews_dataset.csv": [{
        "review_id": "r1", "order_id": "o1", "review_score": "4", "review_comment_title": "",
        "review_comment_message": "Bom", "review_creation_date": "2017-10-11 00:00:00",
        "review_answer_timestamp": "2017-10-12 03:43:48"}],
}


def write_rows(path: Path, rows: list[dict], header: list[str] | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = header or list(rows[0])
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=header, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return path


def hotel_row(**overrides: str) -> dict:
    return {**HOTEL_ROW, **overrides}


def write_hotel(directory: Path, rows: list[dict], name: str = "hotel_bookings.csv") -> Path:
    return write_rows(directory / name, rows, list(HOTEL_ROW))


def write_olist(directory: Path, **replacements: list[dict]) -> Path:
    """Write the complete minimal Olist set. Keys replace a file's rows, by file stem."""
    for name, rows in OLIST_FILES.items():
        stem = name.removesuffix(".csv")
        write_rows(directory / name, replacements.get(stem, rows), list(rows[0]))
    return directory


def olist_row(file_name: str, **overrides: str) -> dict:
    return {**OLIST_FILES[file_name][0], **overrides}
