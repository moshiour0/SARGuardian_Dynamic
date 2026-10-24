#!/usr/bin/env python3
"""Compare main SARGuardian reader outputs from raw and curated HDF5 products.

Uses the exact reader source checkout supplied with --science-root. It is a
pre-deletion gate, not a substitute for a whole-stack regression run.
"""
from __future__ import annotations

import argparse
import importlib
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

IGNORED_PATH_FIELDS = {"file", "filename", "source_file", "input_path", "source_path", "elapsed", "elapsed_seconds"}


def _array_equal(a: np.ndarray, b: np.ndarray) -> bool:
    if a.shape != b.shape or a.dtype != b.dtype:
        return False
    if a.dtype.kind in "fc" and b.dtype.kind in "fc":
        return bool(np.array_equal(a, b, equal_nan=True))
    return bool(np.array_equal(a, b))


def compare_values(left: Any, right: Any, path: str = "$") -> tuple[list[str], int, int]:
    errors: list[str] = []
    arrays = scalars = 0
    if isinstance(left, np.ndarray) or isinstance(right, np.ndarray):
        if not isinstance(left, np.ndarray) or not isinstance(right, np.ndarray):
            return [f"{path}: array-vs-non-array type mismatch"], 0, 0
        arrays += 1
        if not _array_equal(left, right):
            errors.append(f"{path}: array dtype/shape/value mismatch")
        return errors, arrays, scalars
    if isinstance(left, dict) or isinstance(right, dict):
        if not isinstance(left, dict) or not isinstance(right, dict):
            return [f"{path}: dict-vs-non-dict type mismatch"], 0, 0
        lk = {k for k in left if str(k).lower() not in IGNORED_PATH_FIELDS}
        rk = {k for k in right if str(k).lower() not in IGNORED_PATH_FIELDS}
        if lk != rk:
            errors.append(f"{path}: key mismatch left_only={sorted(map(str, lk-rk))[:12]} right_only={sorted(map(str, rk-lk))[:12]}")
        for key in sorted(lk & rk, key=str):
            e, a, sc = compare_values(left[key], right[key], f"{path}.{key}")
            errors.extend(e)
            arrays += a
            scalars += sc
        return errors, arrays, scalars
    if isinstance(left, (list, tuple)) or isinstance(right, (list, tuple)):
        if type(left) is not type(right) or len(left) != len(right):
            return [f"{path}: sequence type/length mismatch"], 0, 0
        for i, (a0, b0) in enumerate(zip(left, right)):
            e, a, sc = compare_values(a0, b0, f"{path}[{i}]")
            errors.extend(e)
            arrays += a
            scalars += sc
        return errors, arrays, scalars
    scalars += 1
    if isinstance(left, (float, np.floating)) and isinstance(right, (float, np.floating)):
        if math.isnan(float(left)) and math.isnan(float(right)):
            return errors, arrays, scalars
        if float(left) != float(right):
            errors.append(f"{path}: scalar numeric mismatch {left!r} != {right!r}")
    elif left != right:
        errors.append(f"{path}: scalar mismatch {left!r} != {right!r}")
    return errors, arrays, scalars


def _count_valid(value: Any, key: str = "") -> int:
    total = 0
    if isinstance(value, dict):
        for k, v in value.items():
            total += _count_valid(v, str(k).lower())
    elif isinstance(value, (list, tuple)):
        for v in value:
            total += _count_valid(v, key)
    elif isinstance(value, np.ndarray) and key in {"valid", "valid_mask"} and value.dtype.kind == "b":
        total += int(value.sum())
    return total


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--product-type", required=True, choices=["GUNW", "GOFF"])
    p.add_argument("--source", required=True, type=Path)
    p.add_argument("--archive", required=True, type=Path)
    p.add_argument("--science-root", required=True, type=Path)
    p.add_argument("--aoi", default="langtang", choices=["source", "langtang", "lhende", "custom"])
    p.add_argument("--science-commit", required=True, help="Exact pinned Git commit of the source reader")
    p.add_argument("--preflight-report", type=Path, help="Verified AOI grid-intersection report from validate_product_aoi.py")
    p.add_argument("--out", required=True, type=Path)
    args = p.parse_args()

    preflight = None
    if args.preflight_report:
        try:
            preflight = json.loads(args.preflight_report.read_text(encoding="utf-8"))
        except Exception:
            print("ERROR=AOI_PREFLIGHT_REPORT_INVALID", file=sys.stderr)
            return 2
        if preflight.get("status") != "PASS" or preflight.get("aoi_name") != args.aoi:
            print("ERROR=AOI_PREFLIGHT_NOT_PASSED_OR_MISMATCHED", file=sys.stderr)
            return 2

    src_dir = args.science_root / "src"
    if not (src_dir / "gunw_reader.py").is_file() or not (src_dir / "goff_reader.py").is_file():
        print("ERROR=SCIENCE_READER_SOURCE_NOT_FOUND", file=sys.stderr)
        return 2
    sys.path.insert(0, str(src_dir.resolve()))
    try:
        gunw_reader = importlib.import_module("gunw_reader")
        gunw_reader.set_aoi(args.aoi)
        if args.product_type == "GUNW":
            reader = gunw_reader.read_gunw
            left = reader(args.source, clip_aoi=True, auto_ref=True)
            right = reader(args.archive, clip_aoi=True, auto_ref=True)
        else:
            goff_reader = importlib.import_module("goff_reader")
            goff_reader.set_aoi(args.aoi)
            reader = goff_reader.read_goff
            # All discovered layers/polarisations are compared, not only layer2.
            left = reader(args.source, layer="best", clip_aoi=True, auto_ref=True, deramp=True)
            right = reader(args.archive, layer="best", clip_aoi=True, auto_ref=True, deramp=True)
    except Exception as exc:
        print(f"ERROR=SCIENCE_READER_EXECUTION_FAILED TYPE={type(exc).__name__}", file=sys.stderr)
        return 2

    errors, arrays_compared, scalars_compared = compare_values(left, right)
    left_valid = _count_valid(left)
    right_valid = _count_valid(right)
    warnings: list[str] = []
    if left_valid != right_valid:
        errors.append(f"raw/archive valid-pixel counts differ: {left_valid} != {right_valid}")
    elif left_valid == 0:
        # A no-valid-pixel observation can still verify archive equivalence if
        # preflight proved the grid intersects the AOI and all scientific output
        # arrays/masks compare identically. It is not a usable deformation result.
        if preflight is None:
            errors.append("zero valid pixels require an AOI grid-intersection preflight report")
        else:
            warnings.append("AOI grid intersects the product, but no science-valid pixels survive the reader quality gates")

    geometry_checks: list[dict[str, Any]] = []
    if args.product_type == "GUNW" and preflight is not None:
        try:
            geometry_merge = importlib.import_module("geometry_merge")
            grids = [g for g in preflight.get("grid_checks", [])
                     if g.get("grid") == "GUNW_geometry_merge_radarGrid"]
            points = grids[0].get("sample_points_wgs84", []) if grids else []
            for point in points:
                lat, lon = float(point["lat"]), float(point["lon"])
                left_geo = geometry_merge.los_from_product(args.source, lat, lon)
                right_geo = geometry_merge.los_from_product(args.archive, lat, lon)
                geo_errors, geo_arrays, geo_scalars = compare_values(left_geo, right_geo, "$.los_from_product")
                errors.extend(geo_errors)
                arrays_compared += geo_arrays
                scalars_compared += geo_scalars
                geometry_checks.append({"lat": lat, "lon": lon, "source_available": left_geo is not None,
                                        "archive_available": right_geo is not None,
                                        "equivalent": not geo_errors and ((left_geo is None) == (right_geo is None))})
        except Exception as exc:
            errors.append(f"geometry dependency comparison failed: {type(exc).__name__}")

    result = {
        "schema_version": "sarguardian-reader-equivalence/1.1",
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "product_type": args.product_type,
        "aoi_name": args.aoi,
        "science_source_commit": args.science_commit,
        "source_filename": args.source.name,
        "archive_filename": args.archive.name,
        "reader_options": {"GUNW": {"clip_aoi": True, "auto_ref": True}, "GOFF": {"layer": "best", "clip_aoi": True, "auto_ref": True, "deramp": True}}[args.product_type],
        "preflight_status": preflight.get("status") if preflight else None,
        "arrays_compared": arrays_compared,
        "scalars_compared": scalars_compared,
        "source_valid_pixel_count": left_valid,
        "archive_valid_pixel_count": right_valid,
        "science_measurement_available": bool(left_valid and right_valid),
        "geometry_dependency_checks": geometry_checks,
        "status": ("PASS_NO_VALID_AOI_PIXELS" if not errors and left_valid == 0 else ("PASS" if not errors else "FAIL")),
        "warnings": warnings,
        "errors": errors[:100],
        "raw_source_deleted": False,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"READER_EQUIVALENCE_STATUS={result['status']}")
    print(f"SCIENCE_MEASUREMENT_AVAILABLE={str(result['science_measurement_available']).lower()}")
    print(f"ARRAYS_COMPARED={arrays_compared}")
    print(f"SCALARS_COMPARED={scalars_compared}")
    print(f"VALID_PIXELS_SOURCE={left_valid}")
    print(f"VALID_PIXELS_ARCHIVE={right_valid}")
    print(f"REPORT_PATH={args.out}")
    if errors:
        print("ERROR=READER_EQUIVALENCE_FAILED", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
