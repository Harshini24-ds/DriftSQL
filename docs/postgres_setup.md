# DriftSQL DATA-002 — PostgreSQL Setup and Olist Import

## Goal
Load the DATA-001-verified Olist CSVs into PostgreSQL without altering the raw files, then prove that every imported row count matches the audit.

## 1. Create the development database
In pgAdmin Query Tool (connected to the default `postgres` database) or in `psql`:

```sql
CREATE DATABASE driftsql;
```

Implementation choice for DATA-002:
- PostgreSQL database: `driftsql`
- raw dataset schema: `olist_raw`

These names are implementation details; `database_id = olist_primary` is introduced in DATA-003.

## 2. Put files in the project

```text
driftsql/
  datasets/raw/olist/        # the 9 original CSVs, unchanged
  docs/dataset_audit.md      # DATA-001 output
  artifacts/dataset_profile.json
  migrations/001_create_olist_raw.sql
  scripts/import_olist.py
```

## 3. Install the loader dependency
Inside the project virtual environment:

```bash
pip install "psycopg[binary]>=3.2,<4"
```

## 4. Run the repeatable import
Windows PowerShell example:

```powershell
python scripts/import_olist.py `
  --dsn "postgresql://postgres:YOUR_PASSWORD@localhost:5432/driftsql" `
  --data-dir "datasets/raw/olist" `
  --schema-sql "migrations/001_create_olist_raw.sql"
```

Do not commit the real password to Git. For normal development we will move credentials to environment variables/config after this local verification step.

## 5. DATA-002 acceptance counts

| table | expected rows |
|---|---:|
| `olist_raw.customers` | 99,441 |
| `olist_raw.geolocation` | 1,000,163 |
| `olist_raw.order_items` | 112,650 |
| `olist_raw.order_payments` | 103,886 |
| `olist_raw.order_reviews` | 99,224 |
| `olist_raw.orders` | 99,441 |
| `olist_raw.products` | 32,951 |
| `olist_raw.sellers` | 3,095 |
| `olist_raw.product_category_name_translation` | 71 |

The import script fails immediately if any count differs.

## DATA-002 boundary
DATA-002 deliberately does **not** add PK/FK constraints. The actual candidate keys/relationships found during DATA-001 are formally revalidated against PostgreSQL during DATA-003 before the generic database adapter and schema snapshot are created.
