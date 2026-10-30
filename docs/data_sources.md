# Data sources

| | Olist | Hotel Booking Demand |
|---|---|---|
| Kaggle identifier | `olistbr/brazilian-ecommerce` | `jessemostipak/hotel-booking-demand` |
| URL | https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce | https://www.kaggle.com/datasets/jessemostipak/hotel-booking-demand |
| Licence shown on Kaggle | CC BY-NC-SA 4.0 | CC BY 4.0 |
| Creator on Kaggle | Francisco Magioli, for Olist | Jesse Mostipak |
| Original source | Olist Store, anonymized commercial data | Antonio, Almeida & Nunes (2019), *Hotel booking demand datasets*, Data in Brief 22 |
| Files | 9 CSVs | 1 CSV |
| Rows | 1,550,922 across all files | 119,390 |

Licences were read from Kaggle's dataset metadata on 2026-10-30. Check them again before any use
beyond this assessment.

## What this means for this repository

- **Raw data is not committed.** `data/raw/` is gitignored, and you download the files yourself.
- **Olist is non-commercial and share-alike.** This project uses it for a technical assessment and
  publishes aggregate results with attribution. Using it in a commercial product would need a separate
  review. This is an engineering reading of the licence, not legal advice.
- **Committed samples are fully synthetic.** `data/samples/` comes from the generator. No row is copied
  from either dataset, though the column names match the sources. Synthetic directories contain a
  `_manifest.json`, and runs over them record `data_origin = synthetic` in `pipeline_runs`.

Attribution: Olist, *Brazilian E-Commerce Public Dataset by Olist*, CC BY-NC-SA 4.0. Antonio, N., Almeida,
A., & Nunes, L. (2019), *Hotel booking demand datasets*, Data in Brief 22, 41-49, via Jesse Mostipak on
Kaggle, CC BY 4.0.

## Downloading

**No account needed.** Kaggle's public download endpoint worked without credentials when this was built:

```bash
curl -L -o olist.zip https://www.kaggle.com/api/v1/datasets/download/olistbr/brazilian-ecommerce
curl -L -o hotel.zip https://www.kaggle.com/api/v1/datasets/download/jessemostipak/hotel-booking-demand
unzip olist.zip -d data/raw/olist
unzip hotel.zip -d data/raw/hotel_bookings
```

**With the Kaggle CLI.** Put your API token at `~/.kaggle/kaggle.json`, never in this repository:

```bash
pip install kaggle
kaggle datasets download olistbr/brazilian-ecommerce -p data/raw/olist --unzip
kaggle datasets download jessemostipak/hotel-booking-demand -p data/raw/hotel_bookings --unzip
```

**Manually.** Download from the URLs above and unzip into the same directories. Any directory also works
with `--source-dir`.

## What profiling found

`scripts/profile_sources.py` wrote `reports/source_profile.json`. These findings shaped the design.

### Olist

| Finding | Count | Decision |
|---|---|---|
| `review_id` is not unique | 814 repeats | Key is `review_id + order_id`, verified to have 0 duplicates |
| `order_id` is not unique in reviews | 551 repeats | Same key |
| `customer_id` is per order, `customer_unique_id` is the person | 3,345 repeats of the latter | `customer_id` is the key |
| Geolocation has no key | 261,831 exact copies of 1,000,163 rows | Collapse, keeping a count |
| Customer or seller zip missing from geolocation | 278 and 7 rows | Soft reference, warning |
| Product category missing from translations | 13 products, 2 categories | Soft reference, warning |
| Products with no category or text attributes | 610 | Valid, columns nullable |
| Orders with no items, payments, or reviews | 775, 1, and 768 | Valid; `order_summary` keeps them with zeros |
| Null lifecycle timestamps | 160, 1,783, 2,965 | Nullable; the order may not have got there |
| Carrier date before purchase | 166 | Warning |
| Delivered before reaching carrier | 23 | Warning |
| Status delivered with no delivery date | 8 | Warning |
| Payment differs from items + freight by over 0.05 | 260 orders | Reported in `order_summary`, not rejected |
| Payment with value 0 or 0 installments | 9 and 2 | Warning |
| Coordinates outside Brazil | 42 rows | Warning |
| City spelled with and without accents | `são paulo` and `sao paulo` | `city_normalized` |
| Zip prefixes with leading zeros | 245,733 rows | Stored as text |
| Comments containing newlines | 3,852 | Row numbers count records, not lines |
| No quantity column on order items | | Each row is one unit; `item_count` in `order_summary` |
| Misspelled source columns | `product_name_lenght`, `product_description_lenght` | Names kept for exact lineage |

### Hotel bookings

| Finding | Count | Decision |
|---|---|---|
| No booking ID | | Derived `record_id`, not presented as a business key |
| Exact duplicate rows | 31,994 | Loaded and flagged, because they may be real separate bookings |
| Literal `NULL` in agent, company, country | 16,340, 112,593, 488 | Null tokens per column |
| Literal `NA` in children | 4 | Null token for that column only |
| Zero guests | 180 | Rejected: a booking for nobody |
| Negative ADR | 1 (-6.38) | Rejected |
| Extreme ADR | 1 (5400) | Warning |
| Zero nights | 715, of which 680 checked out, all at ADR 0 | Warning: legitimate day use |
| ADR of 0 | 1,959 | Valid: 680 are complementary bookings |
| No adults but children | 223 | Valid |
| Check-out date is not arrival + nights | 27 | Warning |
| `arrival_date_week_number` differs from ISO week | 64,054 | Not validated: evidently a different definition |
| Canceled flag against status | 0 disagreements | Enforced as a rule |
