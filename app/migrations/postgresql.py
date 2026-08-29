"""Explicit PostgreSQL schema migration entrypoint for deployment owners."""

import argparse
import json
import os
from pathlib import Path
from typing import Dict, List, Optional

import psycopg

from app.persistence.postgresql import MIGRATIONS


def apply_migrations(dsn: str, migrations: Optional[List[Path]] = None) -> Dict[str, object]:
    """Apply idempotent migrations under one deployment-scoped advisory lock."""
    if not str(dsn).strip():
        raise RuntimeError("A PostgreSQL migration-owner DSN is required.")
    selected = list(migrations or MIGRATIONS)
    with psycopg.connect(dsn) as connection:
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext('interviewer:postgresql:migrations'))"
        )
        for migration in selected:
            connection.execute(Path(migration).read_text(encoding="utf-8"))
    return {"status": "migrated", "migrations": [Path(item).name for item in selected]}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Apply Interviewer PostgreSQL schema migrations with a deployment-owner role."
    )
    parser.add_argument(
        "--dsn",
        default=(
            os.getenv("INTERVIEWER_POSTGRES_MIGRATION_DSN", "").strip()
            or os.getenv("INTERVIEWER_POSTGRES_DSN", "").strip()
        ),
        help="Migration-owner DSN; defaults to INTERVIEWER_POSTGRES_MIGRATION_DSN then INTERVIEWER_POSTGRES_DSN.",
    )
    args = parser.parse_args()
    print(json.dumps(apply_migrations(args.dsn), ensure_ascii=False))


if __name__ == "__main__":
    main()
