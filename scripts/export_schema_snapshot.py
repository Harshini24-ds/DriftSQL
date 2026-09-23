"""Export the verified Olist PostgreSQL schema snapshot for DriftSQL.

DATA-003 / EVID-002 helper.

This script:
- connects to the local ``driftsql`` PostgreSQL database through ``OlistAdapter``;
- reads the live Olist schema/catalog in read-only fashion;
- builds the deterministic schema snapshot/hash;
- writes it to ``artifacts/schema_snapshot.json``;
- immediately re-reads the live catalog and verifies that the saved hash still
  matches.

No database password is stored in this file. If DRIFTSQL_DSN is not set, the
script asks for the PostgreSQL password interactively without echoing it.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
from urllib.parse import quote_plus

from src.data.adapters.olist import OlistAdapter


DEFAULT_OUTPUT = Path("artifacts/schema_snapshot.json")


def build_dsn() -> str:
    """Return a PostgreSQL DSN without hard-coding credentials."""
    configured = os.getenv("DRIFTSQL_DSN")
    if configured:
        return configured

    password = getpass.getpass(
        'PostgreSQL password for local "driftsql" database: '
    )
    encoded_password = quote_plus(password)

    return (
        "postgresql://postgres:"
        f"{encoded_password}@localhost:5432/driftsql"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export the verified Olist schema snapshot/hash."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=(
            "Destination JSON file. "
            "Default: artifacts/schema_snapshot.json"
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    adapter = OlistAdapter(dsn=build_dsn())

    if not adapter.ping():
        raise RuntimeError(
            "Could not connect to the local PostgreSQL driftsql database."
        )

    snapshot = adapter.get_schema_snapshot()

    args.output.parent.mkdir(parents=True, exist_ok=True)

    temp_path = args.output.with_suffix(args.output.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(args.output)

    live_again = adapter.get_schema_snapshot()

    if snapshot["schema_hash"] != live_again["schema_hash"]:
        raise RuntimeError(
            "The live PostgreSQL schema changed while the snapshot "
            "was being exported."
        )

    print("Schema snapshot export PASSED")
    print(f"database_id: {snapshot['database_id']}")
    print(f"schema: {snapshot['schema']}")
    print(f"tables: {len(snapshot['tables'])}")
    print(f"strict_relationships: {len(snapshot['strict_relationships'])}")
    print(f"non_strict_lookups: {len(snapshot['non_strict_lookups'])}")
    print(f"schema_hash: {snapshot['schema_hash']}")
    print(f"saved_to: {args.output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
