#!/usr/bin/env python3
"""Write an explicit no-data row when a successful AOI reader emits no statistics.

A no-valid-pixels report is not zero displacement. This helper is only for the
reader's documented all-selected-layers NO VALID PIXELS outcome when the
reader produced no CSV rows. It emits no numeric displacement metric and marks
reader-derived maturity as UNKNOWN because no reader CSV row existed.
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

_HEADER = re.compile(r"^\s*---\s+(.+?)\s+---\s*$")
_NO_PIXELS = re.compile(r"^\s*NO VALID PIXELS\s*$", re.IGNORECASE)
_FIELDS = ("file", "layer", "valid_px", "maturity", "science_status")


def no_valid_pixel_layers(log_text: str, product_type: str) -> list[str]:
    """Return selected layer headings only when each reports no valid pixels."""
    blocks: list[tuple[str, list[str]]] = []
    current_name: str | None = None
    current_lines: list[str] = []

    for line in log_text.splitlines():
        match = _HEADER.match(line)
        if match:
            if current_name is not None:
                blocks.append((current_name, current_lines))
            current_name = match.group(1).strip()
            current_lines = []
        elif current_name is not None:
            current_lines.append(line)

    if current_name is not None:
        blocks.append((current_name, current_lines))

    selected = blocks
    if product_type == "GOFF":
        selected = [(name, body) for name, body in blocks if "layer2" in name.lower()]
    if not selected:
        raise ValueError("NO_SELECTED_READER_LAYER_FOUND")

    bad = [
        name for name, body in selected
        if not any(_NO_PIXELS.match(line.strip()) for line in body)
    ]
    if bad:
        raise ValueError("NO_VALID_PIXEL_MARKER_NOT_CONFIRMED_FOR_ALL_SELECTED_LAYERS")
    return [name for name, _ in selected]


def write_no_valid_pixel_stats(
    reader_log: Path,
    stats_csv: Path,
    raw_product: Path,
    product_type: str,
) -> dict[str, object]:
    """Write one diagnostic-only product row; never invent a measurement."""
    if product_type not in {"GUNW", "GOFF"}:
        raise ValueError("PRODUCT_TYPE_INVALID")
    if stats_csv.exists():
        raise ValueError("REFUSING_TO_REPLACE_EXISTING_READER_STATS")
    if not reader_log.is_file():
        raise ValueError("READER_LOG_MISSING")
    if raw_product.suffix.lower() != ".h5":
        raise ValueError("RAW_PRODUCT_SUFFIX_INVALID")
    if not raw_product.is_file():
        raise ValueError("RAW_PRODUCT_NOT_FOUND")

    text = reader_log.read_text(encoding="utf-8", errors="replace")
    layer_names = no_valid_pixel_layers(text, product_type)
    # Keep the row simple and product-level. It intentionally does not pretend
    # that any one polarization had a measured value or that no motion occurred.
    layer = "layer2 (no valid pixels)" if product_type == "GOFF" else "no valid pixels"
    row = {
        "file": raw_product.name,
        "layer": layer,
        "valid_px": "0",
        "maturity": "UNKNOWN",
        "science_status": "NO_VALID_PIXELS_AFTER_AOI_AND_QUALITY_GATES",
    }
    stats_csv.parent.mkdir(parents=True, exist_ok=True)
    with stats_csv.open("x", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerow(row)

    result = {
        "status": "NO_VALID_PIXELS_AFTER_AOI_AND_QUALITY_GATES",
        "product_type": product_type,
        "granule_name": raw_product.name,
        "selected_reader_layers": layer_names,
        "valid_pixels": 0,
        "measurement_reported": False,
        "reader_maturity_reported": False,
        "stats_csv": str(stats_csv),
    }
    print(
        "NO_VALID_PIXEL_DIAGNOSTIC_ROW=PASS "
        f"PRODUCT_TYPE={product_type} VALID_PIXELS=0 MEASUREMENT_REPORTED=false"
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reader-log", required=True, type=Path)
    parser.add_argument("--stats-csv", required=True, type=Path)
    parser.add_argument("--raw-product", required=True, type=Path)
    parser.add_argument("--product-type", required=True, choices=("GUNW", "GOFF"))
    args = parser.parse_args()
    try:
        write_no_valid_pixel_stats(args.reader_log, args.stats_csv, args.raw_product, args.product_type)
    except (OSError, ValueError) as exc:
        print(f"ERROR=NO_VALID_PIXEL_DIAGNOSTIC_NOT_CREATED reason={str(exc)[:220]}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
