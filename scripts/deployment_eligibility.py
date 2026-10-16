#!/usr/bin/env python3
"""Classify migration changes conservatively for the release contract."""

from __future__ import annotations

import argparse
import sys


def classify_migration_changes(paths: list[str], *, base_known: bool) -> str:
    if not base_known:
        return "unknown"
    for path in paths:
        normalized = path.strip().lower()
        parts = normalized.split("/")
        if normalized in {"app/migrations.py", "app/safe_migrations.py"}:
            return "review-required"
        if "migrations" in parts and normalized.endswith(".py"):
            return "review-required"
        if normalized.startswith("app/") and "migration" in parts[-1] and normalized.endswith(".py"):
            return "review-required"
    return "no-migration-files-changed"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-known", action="store_true")
    args = parser.parse_args()
    paths = sys.stdin.read().splitlines()
    print(f"migration_gate={classify_migration_changes(paths, base_known=args.base_known)}")
    print("deployment_eligible=false")


if __name__ == "__main__":
    main()
