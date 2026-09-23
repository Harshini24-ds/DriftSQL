# DriftSQL DATA-001 — Olist Dataset Audit

## Decision

**DATA-001 status: VERIFIED**

The uploaded archive contains the 9 CSV files listed by the Olist-published Kaggle dataset and totals 52 columns. All files are readable. The six core relational links required for the DriftSQL relational model validate with 100% child-to-parent coverage. No blocker was found for PostgreSQL loading.

Raw files must remain unchanged. Load-time handling should preserve source column names and values while documenting encoding, types, nulls, duplicate geolocation rows, and lookup gaps.

## Source and license

- Dataset: **Brazilian E-Commerce Public Dataset by Olist**
- Publisher page: https://www.kaggle.com/olistbr/brazilian-ecommerce/metadata
- Publisher shown by Kaggle: **Olist and collaborators**
- License shown by Kaggle: **CC BY-NC-SA 4.0**
- Dataset description: real anonymized commercial Olist e-commerce data; ~100k orders from 2016–2018.
- Archive provenance note: the ZIP contents match the Olist Kaggle listing (9 CSVs / 52 columns), but the exact download origin of this specific ZIP cannot be cryptographically proven from the ZIP alone.

## Archive integrity

- Uploaded archive: `archive (2).zip`
- ZIP size: 42.65 MB
- ZIP SHA-256: `967e41e04fc306fe604e2a693f488995a8b41e5047418f8a5c8e4abd6deca784`
- Files: 9
- Uncompressed size: 120.34 MB
- Total columns: 52

## File inventory and profile

| File | Rows | Cols | Exact duplicate rows | Notes |
|---|---:|---:|---:|---|
| `olist_customers_dataset.csv` | 99,441 | 5 | 0 | — |
| `olist_geolocation_dataset.csv` | 1,000,163 | 5 | 261,831 | No natural unique key |
| `olist_order_items_dataset.csv` | 112,650 | 7 | 0 | — |
| `olist_order_payments_dataset.csv` | 103,886 | 5 | 0 | — |
| `olist_order_reviews_dataset.csv` | 99,224 | 7 | 0 | 2 columns contain nulls |
| `olist_orders_dataset.csv` | 99,441 | 8 | 0 | 3 columns contain nulls |
| `olist_products_dataset.csv` | 32,951 | 9 | 0 | 8 columns contain nulls |
| `olist_sellers_dataset.csv` | 3,095 | 4 | 0 | — |
| `product_category_name_translation.csv` | 71 | 2 | 0 | UTF-8 BOM in first header |

## Validated candidate keys

- `olist_customers_dataset.csv`: **(customer_id)** — validated unique/non-null.
- `olist_orders_dataset.csv`: **(order_id)** — validated unique/non-null.
- `olist_products_dataset.csv`: **(product_id)** — validated unique/non-null.
- `olist_sellers_dataset.csv`: **(seller_id)** — validated unique/non-null.
- `product_category_name_translation.csv`: **(product_category_name)** — validated unique/non-null after UTF-8 BOM normalization.
- `olist_order_items_dataset.csv`: **(order_id, order_item_id)** — validated unique/non-null.
- `olist_order_payments_dataset.csv`: **(order_id, payment_sequential)** — validated unique/non-null.
- `olist_order_reviews_dataset.csv`: **(review_id, order_id)** — validated unique/non-null; review_id alone is not unique.
- `olist_geolocation_dataset.csv`: no natural unique key; 261,831 exact duplicate rows.

## Validated relationships from the actual CSV values

| Child | Parent | Row coverage | Missing child rows | Result |
|---|---|---:|---:|---|
| `olist_orders_dataset.csv.customer_id` | `olist_customers_dataset.csv.customer_id` | 100.000% | 0 | STRICT candidate FK |
| `olist_order_items_dataset.csv.order_id` | `olist_orders_dataset.csv.order_id` | 100.000% | 0 | STRICT candidate FK |
| `olist_order_payments_dataset.csv.order_id` | `olist_orders_dataset.csv.order_id` | 100.000% | 0 | STRICT candidate FK |
| `olist_order_reviews_dataset.csv.order_id` | `olist_orders_dataset.csv.order_id` | 100.000% | 0 | STRICT candidate FK |
| `olist_order_items_dataset.csv.product_id` | `olist_products_dataset.csv.product_id` | 100.000% | 0 | STRICT candidate FK |
| `olist_order_items_dataset.csv.seller_id` | `olist_sellers_dataset.csv.seller_id` | 100.000% | 0 | STRICT candidate FK |
| `olist_products_dataset.csv.product_category_name` | `product_category_name_translation.csv.product_category_name` | 99.960% | 13 | Non-strict lookup |
| `olist_customers_dataset.csv.customer_zip_code_prefix` | `olist_geolocation_dataset.csv.geolocation_zip_code_prefix` | 99.720% | 278 | Non-strict lookup |
| `olist_sellers_dataset.csv.seller_zip_code_prefix` | `olist_geolocation_dataset.csv.geolocation_zip_code_prefix` | 99.774% | 7 | Non-strict lookup |

### Core strict relationships

The following six relationships have **100% child-to-parent coverage** and are suitable as the initial relational backbone, subject to PostgreSQL import verification:
- `olist_orders_dataset.csv.customer_id` → `olist_customers_dataset.csv.customer_id`
- `olist_order_items_dataset.csv.order_id` → `olist_orders_dataset.csv.order_id`
- `olist_order_payments_dataset.csv.order_id` → `olist_orders_dataset.csv.order_id`
- `olist_order_reviews_dataset.csv.order_id` → `olist_orders_dataset.csv.order_id`
- `olist_order_items_dataset.csv.product_id` → `olist_products_dataset.csv.product_id`
- `olist_order_items_dataset.csv.seller_id` → `olist_sellers_dataset.csv.seller_id`

## Data-quality findings that must be preserved/documented

- `olist_geolocation_dataset.csv` contains **261,831 exact duplicate rows**. Do not delete them from the raw dataset; decide later whether a derived geolocation view should deduplicate/aggregate.
- Product-category translation is not fully covering the product table: two category values are absent from the translation file: `pc_gamer`, `portateis_cozinha_e_preparadores_de_alimentos`.
- Customer ZIP prefixes: 157 distinct prefixes do not appear in the geolocation table; this is therefore **not** a strict FK.
- Seller ZIP prefixes: 7 distinct prefixes do not appear in the geolocation table; this is therefore **not** a strict FK.
- `product_category_name_translation.csv` begins with a UTF-8 BOM. Read it using UTF-8-with-BOM handling (`utf-8-sig`) or equivalent during import.
- Preserve source spellings such as `product_name_lenght` and `product_description_lenght`; do not silently rename raw columns.
- Expected nulls exist in order delivery/approval timestamps, review comment fields, and some product attributes. These are data facts, not import failures.

## DATA-002 handoff

Proceed to PostgreSQL import with these constraints:

1. Keep the raw CSVs immutable.
2. Import all 9 files reproducibly and verify row counts against this audit.
3. Use source column names exactly in the raw/staging layer.
4. Apply explicit PostgreSQL types only after successful staging/profile validation.
5. Re-validate the six strict relationships after loading.
6. Do not declare geolocation ZIP or product-category translation as strict foreign keys.
7. Generate a schema snapshot/hash only after the PostgreSQL load is verified.

## File checksums

- `olist_customers_dataset.csv` — `983a422239e1712ded753b3bf9ecf47dc73f144d306029dcfa99e70a226883d2`
- `olist_geolocation_dataset.csv` — `b514f6fc991b9566aeba02aa5d67e2c3630f034b60a0e05aa0d082a3b66d88d6`
- `olist_order_items_dataset.csv` — `0bc4d068c4fe38cbb01bd90e8746e3c613fe7b4baef75fab7b0e329701c3e279`
- `olist_order_payments_dataset.csv` — `4f713964f2815dbbaa40b9488268c55aac3627bfce5aa96cf58d1f3616de3cc0`
- `olist_order_reviews_dataset.csv` — `012b61c7593e34f51fa614efdf802b9c7056ce6aae5307ddb93236e7cfc797d7`
- `olist_orders_dataset.csv` — `8df58ef3d2d7e9944010f7beecd9b75367f5588ec6e3c91cec19ae3345ef9ecf`
- `olist_products_dataset.csv` — `3e6569628a17fbc75fd206ee357b59e20364b9afa90f5b6cd5b4d624c58aa9cc`
- `olist_sellers_dataset.csv` — `1f643d2b950373b85735e7794b20986f528d7a000432e7c6f9bcbb44d0846a0e`
- `product_category_name_translation.csv` — `a81f0d1f27b27e7293f761bc79e3ce8f348ee39c4b3ed3e49bde38f478586278`