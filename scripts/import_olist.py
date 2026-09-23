#!/usr/bin/env python3
"""Repeatable DATA-002 loader for the verified Olist CSV archive.

Design constraints:
- raw CSV files are never modified;
- source spellings are preserved in PostgreSQL columns;
- DATA-002 loads data only; PK/FK declarations are deferred to DATA-003;
- every load is followed by exact row-count verification against DATA-001.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

import psycopg
from psycopg import sql

EXPECTED = {
    "olist_customers_dataset.csv": ("customers", 99_441),
    "olist_geolocation_dataset.csv": ("geolocation", 1_000_163),
    "olist_order_items_dataset.csv": ("order_items", 112_650),
    "olist_order_payments_dataset.csv": ("order_payments", 103_886),
    "olist_order_reviews_dataset.csv": ("order_reviews", 99_224),
    "olist_orders_dataset.csv": ("orders", 99_441),
    "olist_products_dataset.csv": ("products", 32_951),
    "olist_sellers_dataset.csv": ("sellers", 3_095),
    "product_category_name_translation.csv": ("product_category_name_translation", 71),
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--dsn", required=True, help="PostgreSQL DSN, e.g. postgresql://user:pass@localhost:5432/driftsql")
    p.add_argument("--data-dir", required=True, type=Path, help="Folder containing the 9 raw Olist CSV files")
    p.add_argument("--schema-sql", required=True, type=Path, help="Path to migrations/001_create_olist_raw.sql")
    return p.parse_args()


def require_files(data_dir: Path) -> None:
    missing = [name for name in EXPECTED if not (data_dir / name).is_file()]
    if missing:
        raise FileNotFoundError("Missing raw Olist files: " + ", ".join(missing))


def apply_schema(conn: psycopg.Connection, schema_sql: Path) -> None:
    with conn.cursor() as cur:
        cur.execute(schema_sql.read_text(encoding="utf-8"))
    conn.commit()


def load_one(conn: psycopg.Connection, csv_path: Path, table: str) -> None:
    with conn.cursor() as cur:
        cur.execute(sql.SQL("TRUNCATE TABLE olist_raw.{}") .format(sql.Identifier(table)))
        copy_stmt = sql.SQL(
            "COPY olist_raw.{} FROM STDIN WITH (FORMAT CSV, HEADER TRUE, NULL '')"
        ).format(sql.Identifier(table))
        with csv_path.open("rb") as f, cur.copy(copy_stmt) as copy:
            while chunk := f.read(1024 * 1024):
                copy.write(chunk)
    conn.commit()


def count_rows(conn: psycopg.Connection, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(sql.SQL("SELECT COUNT(*) FROM olist_raw.{}") .format(sql.Identifier(table)))
        return int(cur.fetchone()[0])


def main() -> int:
    args = parse_args()
    args.data_dir = args.data_dir.resolve()
    args.schema_sql = args.schema_sql.resolve()
    require_files(args.data_dir)
    if not args.schema_sql.is_file():
        raise FileNotFoundError(args.schema_sql)

    with psycopg.connect(args.dsn) as conn:
        apply_schema(conn, args.schema_sql)

        print("Loading verified Olist CSVs into schema olist_raw...")
        for filename, (table, expected) in EXPECTED.items():
            path = args.data_dir / filename
            print(f"  {filename} -> olist_raw.{table}")
            load_one(conn, path, table)
            actual = count_rows(conn, table)
            status = "OK" if actual == expected else "MISMATCH"
            print(f"    rows: {actual:,} / expected {expected:,} [{status}]")
            if actual != expected:
                print("DATA-002 FAILED: row-count mismatch. Stop before DATA-003.", file=sys.stderr)
                return 2

    print("DATA-002 import verification passed: all 9 row counts match DATA-001.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
