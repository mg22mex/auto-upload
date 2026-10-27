#!/usr/bin/env python3
"""Populate ``data/graphql/*.json`` from versioned ``*.example.json`` templates.

``data/**`` is gitignored (persistent fb-worker volume). Captured GraphQL
templates are versioned under ``src/facebook/graphql/examples/``. This script
copies them into the runtime directory so create/delete/photo/selling work
after checkout + ``ln -s ~/auto-upload-data/data data``.

Default: only fill *missing* runtime files (never clobber a fresher capture).
``--force`` overwrites all. ``create_vehicle.json`` is always ensured present
(copy from example if missing, even without ``--force``).

Examples:
  python scripts/sync_graphql_templates.py
  python scripts/sync_graphql_templates.py --force
  python scripts/sync_graphql_templates.py --root /path/to/checkout
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.facebook.graphql.templates import default_graphql_dir

# Basename (without .example.json) → required at runtime.
_TEMPLATES = (
    "create_vehicle",
    "delete_listing",
    "photo_upload",
    "selling_active",
)


def examples_dir(root: Path) -> Path:
    return root / "src" / "facebook" / "graphql" / "examples"


def sync_templates(
    *,
    root: Path,
    force: bool = False,
    dry_run: bool = False,
) -> int:
    """Copy examples → data/graphql. Returns number of files written."""
    src_dir = examples_dir(root)
    dst_dir = default_graphql_dir(root)
    if not src_dir.is_dir():
        raise SystemExit(f"Examples dir missing: {src_dir}")

    if not dry_run:
        dst_dir.mkdir(parents=True, exist_ok=True)

    written = 0
    for name in _TEMPLATES:
        example = src_dir / f"{name}.example.json"
        target = dst_dir / f"{name}.json"
        if not example.is_file():
            print(f"  SKIP missing example: {example}", flush=True)
            continue

        # Fill missing; never clobber unless --force.
        if target.is_file() and not force:
            print(f"  keep existing {target.relative_to(root)}", flush=True)
            continue

        # Validate JSON before copy.
        with example.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict) or not str(payload.get("doc_id") or "").strip():
            print(f"  WARN skip invalid example (no doc_id): {example}", flush=True)
            continue

        action = "overwrite" if target.is_file() else "create"
        print(
            f"  {action} {target.relative_to(root)} ← {example.relative_to(root)}",
            flush=True,
        )
        if not dry_run:
            shutil.copy2(example, target)
        written += 1

    # Hard guarantee: create_vehicle.json must exist after sync.
    create_dst = dst_dir / "create_vehicle.json"
    create_src = src_dir / "create_vehicle.example.json"
    if not create_dst.is_file():
        if not create_src.is_file():
            raise SystemExit(
                f"FATAL: create_vehicle missing and no example at {create_src}"
            )
        print(
            f"  ENSURE create {create_dst.relative_to(root)} "
            f"← {create_src.relative_to(root)}",
            flush=True,
        )
        if not dry_run:
            shutil.copy2(create_src, create_dst)
        written += 1

    print(
        f"GraphQL templates sync done: wrote={written} dir={dst_dir}",
        flush=True,
    )
    return written


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync versioned GraphQL examples into data/graphql/"
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="Repo root (default: checkout containing this script)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing data/graphql/*.json from examples",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log actions only",
    )
    args = parser.parse_args()
    root = args.root.resolve()
    print(f"=== Sync GraphQL templates (root={root}) ===", flush=True)
    sync_templates(root=root, force=bool(args.force), dry_run=bool(args.dry_run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
