"""Reproducible synthetic data, using each contract's real column names.

Layout for ``rows`` records (100 rows go under data/samples, the rest under data/generated):

    <dataset>/rows_<n>/clean/         valid baseline, including boundary values and duplicates
    <dataset>/rows_<n>/dirty/         the baseline with labelled defects mixed in
    <dataset>/rows_<n>/late/          records that arrive after the baseline
    <dataset>/rows_<n>/out_of_order/  different versions of records, delivered in the wrong order
    <dataset>/rows_<n>/variants/      schema drift: extra column, missing required column

Answers live apart from inputs, in data/expected/<dataset>/rows_<n>.json. They are computed
here with plain pandas over the generated rows, not by the pipeline, so they can catch it being wrong.
Every directory has a _manifest.json that marks its contents as synthetic.
"""

import csv
import hashlib
import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from faker import Faker

from src.core.config import Settings
from src.core.contract import DatasetContract, load_contract
from src.core.exceptions import ConfigurationError

MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August",
               "September", "October", "November", "December"]
EPOCH = datetime(2017, 1, 1)
CATEGORIES = {
    "beleza_saude": "health_beauty", "informatica_acessorios": "computers_accessories",
    "automotivo": "auto", "cama_mesa_banho": "bed_bath_table", "moveis_decoracao": "furniture_decor",
    "esporte_lazer": "sports_leisure", "perfumaria": "perfumery", "utilidades_domesticas": "housewares",
    "telefonia": "telephony", "relogios_presentes": "watches_gifts",
}
UNTRANSLATED_CATEGORY = "pc_gamer"  # also has no translation in the real data


def _stable_id(kind: str, number: int) -> str:
    return hashlib.md5(f"{kind}-{number}".encode()).hexdigest()


def _timestamp(moment: datetime | None) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S") if moment else ""


def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def _write_manifest(directory: Path, dataset: str, rows: int, seed: int) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {"origin": "synthetic", "dataset": dataset, "rows": rows, "seed": seed}
    (directory / "_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _mix_defects(clean: list[list[str]], defects: list[tuple[list[str], str]],
                 rng: np.random.Generator) -> tuple[list[list[str]], list[dict]]:
    """Insert defects at seeded positions. Returns the rows and each defect's row number and rule."""
    total = len(clean) + len(defects)
    positions = set(rng.choice(total, size=len(defects), replace=False).tolist())
    shuffled = iter(rng.permutation(len(defects)).tolist())
    remaining = iter(clean)
    rows: list[list[str]] = []
    labels: list[dict] = []
    for position in range(total):
        if position in positions:
            row, rule = defects[next(shuffled)]
            labels.append({"source_row_number": position + 1, "expected_rule": rule})
            rows.append(row)
        else:
            rows.append(next(remaining))
    return rows, labels


# ---- hotel bookings -------------------------------------------------------------------


def _hotel_rows(count: int, rng: np.random.Generator, countries: list[str], start: datetime, span_days: int,
                columns: list[str]) -> list[list[str]]:
    rows = []
    for _ in range(count):
        arrival = start + timedelta(days=int(rng.integers(0, span_days)))
        canceled = int(rng.random() < 0.37)
        weekend, week = int(rng.integers(0, 3)), int(rng.integers(0, 6))
        lead_time = int(rng.integers(0, 400))
        roll = rng.random()
        adr = round(float(rng.uniform(40, 250)), 2)
        if weekend + week == 0 or roll < 0.01:  # day-use and complimentary stays are priced at 0
            adr = 0.0
        if canceled:
            status = "No-Show" if rng.random() < 0.03 else "Canceled"
            status_date = arrival - timedelta(days=int(rng.integers(0, lead_time + 1)))
        else:
            status = "Check-Out"
            status_date = arrival + timedelta(days=weekend + week)
        room = str(rng.choice(list("ABCDEFG")))
        values = {
            "hotel": str(rng.choice(["City Hotel", "Resort Hotel"], p=[0.66, 0.34])),
            "is_canceled": str(canceled),
            "lead_time": str(lead_time),
            "arrival_date_year": str(arrival.year),
            "arrival_date_month": MONTH_NAMES[arrival.month - 1],
            "arrival_date_week_number": str(arrival.isocalendar()[1]),
            "arrival_date_day_of_month": str(arrival.day),
            "stays_in_weekend_nights": str(weekend),
            "stays_in_week_nights": str(week),
            "adults": str(int(rng.choice([1, 2, 3], p=[0.2, 0.75, 0.05]))),
            "children": "NA" if rng.random() < 0.002 else str(int(rng.choice([0, 1, 2], p=[0.93, 0.04, 0.03]))),
            "babies": str(int(rng.choice([0, 1], p=[0.99, 0.01]))),
            "meal": str(rng.choice(["BB", "HB", "SC", "FB", "Undefined"], p=[0.77, 0.12, 0.09, 0.01, 0.01])),
            "country": "NULL" if rng.random() < 0.005 else str(rng.choice(countries)),
            "market_segment": str(rng.choice(["Online TA", "Offline TA/TO", "Groups", "Direct", "Corporate"])),
            "distribution_channel": str(rng.choice(["TA/TO", "Direct", "Corporate", "GDS"])),
            "is_repeated_guest": str(int(rng.random() < 0.03)),
            "previous_cancellations": str(int(rng.choice([0, 1], p=[0.95, 0.05]))),
            "previous_bookings_not_canceled": str(int(rng.choice([0, 1], p=[0.97, 0.03]))),
            "reserved_room_type": room,
            "assigned_room_type": room,
            "booking_changes": str(int(rng.integers(0, 3))),
            "deposit_type": str(rng.choice(["No Deposit", "Non Refund", "Refundable"], p=[0.87, 0.12, 0.01])),
            "agent": "NULL" if rng.random() < 0.14 else str(int(rng.integers(1, 500))),
            "company": "NULL" if rng.random() < 0.94 else str(int(rng.integers(1, 500))),
            "days_in_waiting_list": str(int(rng.choice([0, 5], p=[0.97, 0.03]))),
            "customer_type": str(rng.choice(["Transient", "Transient-Party", "Contract", "Group"])),
            "adr": f"{adr:.2f}",
            "required_car_parking_spaces": str(int(rng.random() < 0.06)),
            "total_of_special_requests": str(int(rng.integers(0, 3))),
            "reservation_status": status,
            "reservation_status_date": status_date.strftime("%Y-%m-%d"),
        }
        rows.append([values[column] for column in columns])
    return rows


def _hotel_oracle(columns: list[str], clean: list[list[str]]) -> dict:
    """Expected results, computed independently of the pipeline."""
    frame = pd.DataFrame(clean, columns=columns)
    number = lambda name: pd.to_numeric(frame[name], errors="coerce")
    nights = number("stays_in_weekend_nights") + number("stays_in_week_nights")
    guests = number("adults") + number("children").fillna(0) + number("babies")
    return {
        "target_rows": len(frame),
        "flagged_duplicates": int(frame.duplicated().sum()),
        "zero_night_stays": int((nights == 0).sum()),
        "adr_total": round(float(number("adr").sum()), 2),
        "cancellations": int(number("is_canceled").sum()),
        "guests_total": int(guests.sum()),
        "nights_total": int(nights.sum()),
        "estimated_value_total": round(float((number("adr") * nights).round(2).sum()), 2),
    }


def _generate_hotel(contract: DatasetContract, root: Path, rows: int, seed: int) -> dict:
    rng = np.random.default_rng(seed)
    faker = Faker()
    faker.seed_instance(seed)
    countries = sorted({faker.country_code(representation="alpha-3") for _ in range(40)} | {"PRT", "NAM"})
    table = contract.table("hotel_bookings")
    columns = table.source_column_names
    position = {name: index for index, name in enumerate(columns)}
    start, span = datetime(2015, 7, 1), 790

    unique_count = max(1, int(rows * 0.95))
    clean = _hotel_rows(unique_count, rng, countries, start, span, columns)
    # Exact copies are normal in this source: with no booking ID they may be real bookings.
    for index in rng.integers(0, unique_count, size=rows - unique_count):
        clean.append(list(clean[int(index)]))
    # Boundary values that must be accepted.
    boundary = list(clean[0])
    boundary[position["lead_time"]] = "0"
    boundary[position["adr"]] = "1000.00"
    clean[0] = boundary
    if len(clean) > 1:
        day_use = list(clean[1])
        day_use[position["stays_in_weekend_nights"]] = "0"
        day_use[position["stays_in_week_nights"]] = "0"
        day_use[position["adr"]] = "0.00"
        day_use[position["is_canceled"]] = "0"
        day_use[position["reservation_status"]] = "Check-Out"
        arrival = datetime(int(day_use[position["arrival_date_year"]]),
                           MONTH_NAMES.index(day_use[position["arrival_date_month"]]) + 1,
                           int(day_use[position["arrival_date_day_of_month"]]))
        day_use[position["reservation_status_date"]] = arrival.strftime("%Y-%m-%d")
        clean[1] = day_use

    def mutated(**changes: str) -> list[str]:
        row = list(clean[int(rng.integers(0, len(clean)))])
        for name, value in changes.items():
            row[position[name]] = value
        return row

    per_kind = max(1, rows // 100)
    defects: list[tuple[list[str], str]] = []
    for _ in range(per_kind):
        defects += [
            (mutated()[:5], "malformed_row"),
            (mutated(adr="-6.38"), "negative_adr"),
            (mutated(adults="0", children="0", babies="0"), "zero_guests"),
            (mutated(hotel=""), "missing_required:hotel"),
            (mutated(lead_time="abc"), "invalid_type:lead_time"),
            (mutated(arrival_date_month="February", arrival_date_day_of_month="30"), "invalid_arrival_date"),
            (mutated(reservation_status="Unknown"), "unknown_reservation_status"),
            (mutated(is_canceled="1", reservation_status="Check-Out"), "cancel_status_mismatch"),
        ]

    base = root / "rows_{}".format(rows)
    dirty, labels = _mix_defects(clean, defects, rng)
    rules = Counter(label["expected_rule"] for label in labels)

    _write_csv(base / "clean" / "hotel_bookings.csv", columns, clean)
    _write_csv(base / "dirty" / "hotel_bookings.csv", columns, dirty)
    late = _hotel_rows(max(1, rows // 50), rng, countries, datetime(2016, 1, 1), 60, columns)
    _write_csv(base / "late" / "hotel_bookings_late.csv", columns, late)
    shuffled = [clean[int(i)] for i in rng.permutation(len(clean))]
    _write_csv(base / "out_of_order" / "hotel_bookings.csv", columns, shuffled)
    _write_csv(base / "variants" / "extra_column" / "hotel_bookings.csv", [*columns, "loyalty_tier"],
               [[*row, "gold"] for row in clean])
    dropped = position["adr"]
    _write_csv(base / "variants" / "missing_required_column" / "hotel_bookings.csv",
               [c for c in columns if c != "adr"], [row[:dropped] + row[dropped + 1:] for row in clean])
    for name in ("clean", "dirty", "late", "out_of_order", "variants/extra_column", "variants/missing_required_column"):
        _write_manifest(base / name, contract.dataset, rows, seed)

    clean_answers = _hotel_oracle(columns, clean)
    return {
        "dataset": contract.dataset, "rows": rows, "seed": seed,
        "clean": {"hotel_bookings": {"source_rows": len(clean), "rejected": 0, **clean_answers}},
        "dirty": {"hotel_bookings": {
            "source_rows": len(dirty), "rejected": len(labels), "rejected_by_rule": dict(rules),
            "malformed": rules["malformed_row"], **clean_answers,
        }},
        "defect_rows": {"hotel_bookings": labels},
        "late": {"hotel_bookings": _hotel_oracle(columns, late)},
    }


# ---- olist ----------------------------------------------------------------------------


def _generate_olist(contract: DatasetContract, root: Path, orders: int, seed: int) -> dict:
    rng = np.random.default_rng(seed)
    faker = Faker("pt_BR")
    faker.seed_instance(seed)
    cities = sorted({faker.city().lower() for _ in range(40)} | {"sao paulo", "são paulo"})
    states = ["SP", "RJ", "MG", "RS", "PR", "BA", "SC"]

    def pick(pool: list[str]) -> str:
        # rng.choice would convert the whole list to an array on every call.
        return pool[int(rng.integers(0, len(pool)))]

    zip_pool = [f"{int(z):05d}" for z in rng.choice(np.arange(1000, 99999), size=max(20, orders // 5), replace=False)]
    unknown_zip = "99999"

    # Geolocation: no key, many exact copies, and a coordinate that is valid but not in Brazil.
    geolocation = []
    for _ in range(int(orders * 1.4) + 5):
        geolocation.append([pick(zip_pool), repr(round(float(rng.uniform(-33, 5)), 8)),
                            repr(round(float(rng.uniform(-73, -35)), 8)), pick(cities), pick(states)])
    geolocation += [list(geolocation[int(i)]) for i in rng.integers(0, len(geolocation), size=int(orders * 0.6) + 2)]
    geolocation.append([zip_pool[0], "45.06593318", "-101.46676645", cities[0], "SP"])

    translations = [[pt, en] for pt, en in CATEGORIES.items()]
    categories = list(CATEGORIES) + [UNTRANSLATED_CATEGORY]

    sellers = [[_stable_id("seller", i), pick(zip_pool), pick(cities), pick(states)]
               for i in range(max(5, orders // 30))]
    products = []
    for i in range(max(10, orders // 3)):
        if rng.random() < 0.02:  # the real data has products with no attributes at all
            products.append([_stable_id("product", i), "", "", "", "", "300", "20", "10", "15"])
        else:
            products.append([_stable_id("product", i), pick(categories), str(int(rng.integers(5, 76))),
                             str(int(rng.integers(4, 3992))), str(int(rng.integers(1, 20))), str(int(rng.integers(1, 30000))),
                             str(int(rng.integers(7, 105))), str(int(rng.integers(2, 105))), str(int(rng.integers(6, 118)))])

    customers, order_rows, items, payments, reviews = [], [], [], [], []
    shared_review = _stable_id("review", 0)
    for i in range(orders):
        customer_id, order_id = _stable_id("customer", i), _stable_id("order", i)
        unique_id = _stable_id("person", int(rng.integers(0, max(1, int(orders * 0.97)))))
        customer_zip = unknown_zip if rng.random() < 0.003 else pick(zip_pool)
        customers.append([customer_id, unique_id, customer_zip, pick(cities), pick(states)])

        purchase = EPOCH + timedelta(minutes=int(rng.integers(0, 60 * 24 * 600)))
        status = str(rng.choice(["delivered", "shipped", "canceled", "invoiced"], p=[0.94, 0.03, 0.02, 0.01]))
        approved = purchase + timedelta(hours=int(rng.integers(1, 48)))
        carrier = approved + timedelta(days=int(rng.integers(1, 5))) if status in ("delivered", "shipped") else None
        delivered = carrier + timedelta(days=int(rng.integers(1, 20))) if status == "delivered" else None
        estimated = purchase.replace(hour=0, minute=0, second=0) + timedelta(days=int(rng.integers(8, 30)))
        order_rows.append([order_id, customer_id, status, _timestamp(purchase), _timestamp(approved),
                           _timestamp(carrier), _timestamp(delivered), _timestamp(estimated)])

        order_total = 0.0
        if rng.random() >= 0.01:  # 775 real orders have no items
            for sequence in range(1, int(rng.choice([1, 2, 3], p=[0.85, 0.1, 0.05])) + 1):
                price, freight = round(float(rng.uniform(5, 500)), 2), round(float(rng.uniform(0, 60)), 2)
                order_total += price + freight
                items.append([order_id, str(sequence), products[int(rng.integers(0, len(products)))][0],
                              sellers[int(rng.integers(0, len(sellers)))][0], _timestamp(purchase + timedelta(days=5)),
                              f"{price:.2f}", f"{freight:.2f}"])
        order_total = round(order_total, 2)
        if i != 0:  # one real order has no payment
            if order_total > 20 and rng.random() < 0.05:
                voucher = round(order_total / 2, 2)
                payments.append([order_id, "1", "voucher", "1", f"{voucher:.2f}"])
                payments.append([order_id, "2", "credit_card", "3", f"{order_total - voucher:.2f}"])
            else:
                kind = str(rng.choice(["credit_card", "boleto", "debit_card"]))
                payments.append([order_id, "1", kind, str(int(rng.integers(1, 12))), f"{order_total:.2f}"])
        if rng.random() < 0.99:
            answered = purchase + timedelta(days=int(rng.integers(1, 20)), hours=3)
            comment = str(rng.choice(["", "Recebi bem antes do prazo", "Produto ótimo,\nchegou rápido", "Não chegou"]))
            # Boundary: one review ID shared by two orders, as in 814 real rows.
            review_id = shared_review if i < 2 else _stable_id("review", i)
            reviews.append([review_id, order_id, str(int(rng.integers(1, 6))), "", comment,
                            _timestamp(answered.replace(hour=0, minute=0, second=0)), _timestamp(answered)])

    tables = {
        "geolocation": geolocation, "product_category_translations": translations, "customers": customers,
        "sellers": sellers, "products": products, "orders": order_rows, "order_items": items,
        "payments": payments, "reviews": reviews,
    }
    file_names = {
        "geolocation": "olist_geolocation_dataset.csv", "product_category_translations": "product_category_name_translation.csv",
        "customers": "olist_customers_dataset.csv", "sellers": "olist_sellers_dataset.csv",
        "products": "olist_products_dataset.csv", "orders": "olist_orders_dataset.csv",
        "order_items": "olist_order_items_dataset.csv", "payments": "olist_order_payments_dataset.csv",
        "reviews": "olist_order_reviews_dataset.csv",
    }
    headers = {name: contract.table(name).source_column_names for name in tables}

    ghost = "f" * 32  # a key that exists in no parent table
    new_order = lambda n: _stable_id("defect-order", n)
    per_kind = max(1, orders // 100)
    defects: dict[str, list[tuple[list[str], str]]] = {name: [] for name in tables}
    for n in range(per_kind):
        purchase = _timestamp(EPOCH)
        order = lambda status="delivered", customer=customers[n % len(customers)][0], when=purchase: [
            new_order(next_id()), customer, status, when, "", "", "", purchase]
        counter = iter(range(n * 10, n * 10 + 10))
        next_id = lambda: next(counter)
        product_row = products[n % len(products)]
        seller_row = sellers[n % len(sellers)]
        defects["orders"] += [
            (order(status="lost"), "unknown_order_status"),
            (order(customer=""), "missing_required:customer_id"),
            (order(customer=ghost), "fk_violation:customer_id"),
            (order(when="2018-13-45 99:00:00"), "invalid_type:order_purchase_timestamp"),
            (list(order_rows[n % orders]), "duplicate_key"),
            (["only", "three", "fields"], "malformed_row"),
        ]
        item_base = [_stable_id("defect-item", n), "1", product_row[0], seller_row[0], purchase, "10.00", "1.00"]
        real_order = order_rows[n % orders][0]
        defects["order_items"] += [
            ([real_order, "90", product_row[0], seller_row[0], purchase, "0.00", "1.00"], "non_positive_price"),
            ([ghost, "1", product_row[0], seller_row[0], purchase, "10.00", "1.00"], "fk_violation:order_id"),
            ([real_order, "91", ghost, seller_row[0], purchase, "10.00", "1.00"], "fk_violation:product_id"),
            (item_base[:3], "malformed_row"),
        ]
        if items:
            defects["order_items"].append((list(items[n % len(items)]), "duplicate_key"))
        defects["payments"] += [
            ([real_order, "90", "credit_card", "1", "-5.00"], "negative_payment"),
            ([ghost, "1", "credit_card", "1", "5.00"], "fk_violation:order_id"),
            ([real_order, "91", "bitcoin", "1", "5.00"], "unknown_payment_type"),
        ]
        defects["reviews"] += [
            ([_stable_id("defect-review", n), real_order, "6", "", "", purchase, purchase], "invalid_review_score"),
            ([_stable_id("defect-review2", n), ghost, "5", "", "", purchase, purchase], "fk_violation:order_id"),
        ]
        defects["customers"].append(([_stable_id("defect-customer", n), "x", "abc", "x", "SP"], "invalid_zip_prefix"))
        defects["sellers"].append(([_stable_id("defect-seller", n), "12345", "x", "Sao Paulo"], "invalid_state"))
        defects["products"].append(([_stable_id("defect-product", n), "perfumaria", "1", "1", "1", "-1", "1", "1", "1"], "negative_weight"))
        defects["geolocation"].append((["12345", "95.0", "-46.0", "x", "SP"], "invalid_latitude"))

    base = root / f"rows_{orders}"
    answers = {"clean": {}, "dirty": {}, "defect_rows": {}}
    for name, rows in tables.items():
        _write_csv(base / "clean" / file_names[name], headers[name], rows)
        dirty, labels = _mix_defects(rows, defects[name], rng)
        _write_csv(base / "dirty" / file_names[name], headers[name], dirty)
        frame = pd.DataFrame(rows, columns=headers[name])
        unique_rows = len(frame.drop_duplicates())
        common = {"target_rows": unique_rows, "collapsed": len(frame) - unique_rows}
        if name == "order_items":
            common.update(price_total=round(float(pd.to_numeric(frame["price"]).sum()), 2),
                          freight_total=round(float(pd.to_numeric(frame["freight_value"]).sum()), 2))
        if name == "payments":
            common.update(payment_total=round(float(pd.to_numeric(frame["payment_value"]).sum()), 2))
        answers["clean"][name] = {"source_rows": len(rows), "rejected": 0, **common}
        rules = Counter(label["expected_rule"] for label in labels)
        answers["dirty"][name] = {"source_rows": len(dirty), "rejected": len(labels),
                                  "rejected_by_rule": dict(rules), "malformed": rules["malformed_row"], **common}
        answers["defect_rows"][name] = labels
    answers["clean"]["order_summary"] = {"target_rows": orders}

    # Late arrival: order lines are delivered before the order and customer they belong to.
    late_customer = _stable_id("late-customer", 0)
    late_order = _stable_id("late-order", 0)
    late_time = _timestamp(EPOCH + timedelta(days=700))
    _write_csv(base / "late" / "step1" / "olist_order_items_dataset_late.csv", headers["order_items"],
               [[late_order, "1", products[0][0], sellers[0][0], late_time, "42.50", "7.50"]])
    _write_csv(base / "late" / "step2" / "olist_customers_dataset_late.csv", headers["customers"],
               [[late_customer, _stable_id("person", 0), zip_pool[0], "curitiba", "PR"]])
    _write_csv(base / "late" / "step2" / "olist_orders_dataset_late.csv", headers["orders"],
               [[late_order, late_customer, "invoiced", late_time, "", "", "", late_time]])

    # Out of order: the newer version of an order arrives first, then the older one.
    version = order_rows[0]
    newer = list(version)
    newer[2] = "delivered"
    newer[6] = _timestamp(EPOCH + timedelta(days=800))
    older = list(version)
    older[2] = "created"
    older[4] = older[5] = older[6] = ""
    _write_csv(base / "out_of_order" / "newer" / "olist_orders_dataset_update.csv", headers["orders"], [newer])
    _write_csv(base / "out_of_order" / "older" / "olist_orders_dataset_resend.csv", headers["orders"], [older])

    _write_csv(base / "variants" / "extra_column" / file_names["sellers"], [*headers["sellers"], "seller_rating"],
               [[*row, "5"] for row in sellers])
    _write_csv(base / "variants" / "missing_required_column" / file_names["sellers"],
               headers["sellers"][:-1], [row[:-1] for row in sellers])
    for name in ("clean", "dirty", "late/step1", "late/step2", "out_of_order/newer", "out_of_order/older",
                 "variants/extra_column", "variants/missing_required_column"):
        _write_manifest(base / name, contract.dataset, orders, seed)

    return {"dataset": contract.dataset, "rows": orders, "seed": seed,
            "late": {"order_id": late_order, "item_total": 50.0},
            "out_of_order": {"order_id": version[0], "final_status": "delivered"},
            **answers}


_GENERATORS = {"hotel_bookings": _generate_hotel, "olist": _generate_olist}


def generate_dataset(settings: Settings, dataset: str, rows: int, seed: int | None = None) -> str:
    """Generate one dataset at one size and return a one-line summary."""
    if rows <= 0:
        raise ConfigurationError("--rows must be positive")
    generator = _GENERATORS.get(dataset)
    if generator is None:
        raise ConfigurationError(f"No generator for {dataset}")
    contract = load_contract(settings.paths.datasets_config_dir / f"{dataset}.yaml")
    seed = settings.generator.seed if seed is None else seed
    base = settings.paths.samples_dir if rows <= 100 else settings.paths.generated_dir
    root = base / dataset
    answers = generator(contract, root, rows, seed)
    expected = settings.paths.expected_dir / dataset / f"rows_{rows}.json"
    expected.parent.mkdir(parents=True, exist_ok=True)
    expected.write_text(json.dumps(answers, indent=2), encoding="utf-8")
    return f"Generated {dataset} at {rows} rows, seed {seed}: {root / f'rows_{rows}'}; answers in {expected}"
