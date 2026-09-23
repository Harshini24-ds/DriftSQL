# DriftSQL Olist Data Model

## Database Identity

- database_id: `olist_primary`
- Database engine: PostgreSQL 18.6
- PostgreSQL database: `driftsql`
- Raw data schema: `olist_raw`
- Primary dataset: Olist Brazilian E-Commerce

The schema and relationships documented below were verified against the
actual PostgreSQL data loaded during DATA-002.

---

## Source Tables

1. customers
2. geolocation
3. order_items
4. order_payments
5. order_reviews
6. orders
7. products
8. sellers
9. product_category_name_translation

---

## Verified Primary-Key Candidates

### customers

Primary-key candidate:

`customer_id`

Verification:

- total rows: 99,441
- non-null customer_id: 99,441
- distinct customer_id: 99,441

Result: VERIFIED

---

### orders

Primary-key candidate:

`order_id`

Verification:

- total rows: 99,441
- non-null order_id: 99,441
- distinct order_id: 99,441

Result: VERIFIED

---

### products

Primary-key candidate:

`product_id`

Verification:

- total rows: 32,951
- non-null product_id: 32,951
- distinct product_id: 32,951

Result: VERIFIED

---

### sellers

Primary-key candidate:

`seller_id`

Verification:

- total rows: 3,095
- non-null seller_id: 3,095
- distinct seller_id: 3,095

Result: VERIFIED

---

## Verified Composite-Key Candidates

### order_items

Candidate:

`(order_id, order_item_id)`

- total rows: 112,650
- non-null key rows: 112,650
- distinct combinations: 112,650

Result: VERIFIED

---

### order_payments

Candidate:

`(order_id, payment_sequential)`

- total rows: 103,886
- non-null key rows: 103,886
- distinct combinations: 103,886

Result: VERIFIED

---

### order_reviews

Candidate:

`(review_id, order_id)`

- total rows: 99,224
- non-null key rows: 99,224
- distinct combinations: 99,224

Result: VERIFIED

`review_id` alone must not be assumed to be unique.

---

## Verified Strict Relationships

The following relationships were validated against the actual PostgreSQL
data. Every relationship produced zero orphan child rows.

1. `orders.customer_id`
   -> `customers.customer_id`

2. `order_items.order_id`
   -> `orders.order_id`

3. `order_payments.order_id`
   -> `orders.order_id`

4. `order_reviews.order_id`
   -> `orders.order_id`

5. `order_items.product_id`
   -> `products.product_id`

6. `order_items.seller_id`
   -> `sellers.seller_id`

These six relationships form the initial authoritative structural backbone
for DriftSQL join reasoning.

---

## Non-Strict Lookup Relationships

The following relationships are useful for lookup or supporting evidence,
but they are NOT strict foreign-key relationships.

### Product category translation

`products.product_category_name`
-> `product_category_name_translation.product_category_name`

Unmatched product rows: 13

Classification: NON-STRICT LOOKUP

---

### Customer geolocation

`customers.customer_zip_code_prefix`
-> `geolocation.geolocation_zip_code_prefix`

Unmatched customer rows: 278

Classification: NON-STRICT LOOKUP

---

### Seller geolocation

`sellers.seller_zip_code_prefix`
-> `geolocation.geolocation_zip_code_prefix`

Unmatched seller rows: 7

Classification: NON-STRICT LOOKUP

---

## Join-Reasoning Policy

Strict structural paths may initially use only verified authoritative
relationships.

Example verified path:

`customers`
-> `orders`
-> `order_items`
-> `products`

Lookup relationships involving geolocation or category translation must be
represented separately and must not be presented as guaranteed foreign-key
relationships.

---

## Data Preservation Rules

- Original Olist CSV files remain immutable.
- Normal Olist transactional rows must not be modified for experiments.
- Evidence faults must be introduced in controlled evidence artifacts or
  dedicated schema-drift copies.
- No relationship may be inferred as authoritative solely from matching
  column names.
- PostgreSQL catalog information becomes the highest-authority source for
  structural schema facts after verified constraints are established.

---

## DATA-003 Status

- Primary-key candidates: VERIFIED
- Composite-key candidates: VERIFIED
- Core relationships: VERIFIED
- Non-strict lookup relationships: DOCUMENTED
- Database adapter: PENDING
- Adapter tests: PENDING
