#!/usr/bin/env python3
"""Prepare shared Google Drive ancestors before isolated AOI workers run in parallel.

The product workers still download/process/verify/clean one raw HDF5 each on
separate ephemeral runners. This preflight creates only shared ancestors (job,
Products, product family, maturity); each product worker owns its unique
granule/Runs/version folders. No product data are downloaded or uploaded here.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

# This script's directory is sys.path[0], so import the uploader as a sibling.
from drive_resumable_upload import Drive, DriveError, drive_folder_names, required_env


def prepare_drive_folders(
    manifest: dict[str, Any],
    drive: Any,
    parent_id: str,
    job_id: str | None,
    version_tag: str,
) -> int:
    """Create shared path ancestors serially; return the selected product count."""
    if not isinstance(manifest, dict):
        raise ValueError("AOI_MANIFEST_INVALID")
    products = manifest.get("products")
    if not isinstance(products, list) or not products:
        raise ValueError("AOI_MANIFEST_HAS_NO_SELECTED_PRODUCTS")
    if not parent_id or not version_tag:
        raise ValueError("DRIVE_PARENT_AND_VERSION_TAG_REQUIRED")

    # Validate every requested path and reject duplicate products before making
    # remote mutations. This avoids half-preparing a manifest known to be bad.
    prepared: list[tuple[str, list[str]]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in products:
        if not isinstance(row, dict):
            raise ValueError("AOI_MANIFEST_PRODUCT_INVALID")
        product_type = str(row.get("product_type") or "").strip().upper()
        maturity = str(row.get("maturity") or "").strip().upper()
        granule_name = str(row.get("granule_name") or "").strip()
        product_id = str(row.get("id") or granule_name).strip()
        identity = (product_type, maturity, granule_name)
        if not product_id or not granule_name or identity in seen:
            raise ValueError("AOI_MANIFEST_DUPLICATE_OR_INCOMPLETE_PRODUCT")
        names = drive_folder_names(
            job_id=job_id,
            product_type=product_type,
            maturity=maturity,
            granule_name=granule_name,
            version_tag=version_tag,
        )
        if (
            not isinstance(names, list)
            or len(names) < 4
            or any(not isinstance(n, str) or not n for n in names)
            or names[-2:] != ["Runs", version_tag]
        ):
            raise ValueError("DRIVE_FOLDER_PATH_INVALID")
        # Granule name, Runs, and run-version are unique to one product worker.
        # Only their ancestors can be shared and raced by concurrent workers.
        shared_names = names[:-3]
        if not shared_names:
            raise ValueError("DRIVE_SHARED_FOLDER_PREFIX_MISSING")
        seen.add(identity)
        prepared.append((product_id, shared_names))

    # Cache each created edge in the folder tree. Shared ancestors are created
    # exactly once here, serially, even when many products use the same family.
    folder_ids: dict[tuple[str, str], str] = {}
    for product_id, names in prepared:
        parent = parent_id
        for name in names:
            key = (parent, name)
            folder_id = folder_ids.get(key)
            if folder_id is None:
                folder_id = drive.ensure_folder(parent, name)
                if not isinstance(folder_id, str) or not folder_id:
                    raise DriveError("DRIVE_FOLDER_PREPARATION_RETURNED_INVALID_ID")
                folder_ids[key] = folder_id
            parent = folder_id
        print(f"DRIVE_PRODUCT_FOLDER_PREPARED id={product_id}")
    print(f"DRIVE_PRODUCTS_WITH_SHARED_ANCESTORS_PREPARED={len(prepared)}")
    print(f"DRIVE_UNIQUE_SHARED_FOLDER_EDGES={len(folder_ids)}")
    return len(prepared)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args()
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        parent_id = required_env("GOOGLE_DRIVE_PARENT_FOLDER_ID")
        job_id = os.environ.get("DRIVE_JOB_ID", "").strip() or None
        version_tag = required_env("DRIVE_VERSION_TAG")
        drive = Drive()
        prepare_drive_folders(manifest, drive, parent_id, job_id, version_tag)
    except (OSError, json.JSONDecodeError, ValueError, DriveError) as exc:
        print(f"ERROR=DRIVE_PRODUCT_FOLDER_PREFLIGHT_FAILED reason={str(exc)[:240]}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
