#!/usr/bin/env python3
"""Measure an AOI-clipped raster near a selected WGS84 target.

Outputs are pair-specific measurements in the reader's per-product reference
frame. They are not a common-datum displacement time series or a forecast.
"""
from __future__ import annotations

import argparse
import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from rasterio.warp import transform as transform_coordinates
from rasterio.windows import Window


def finite_values(values: np.ndarray, nodata: float | None = None,
                  mask: np.ndarray | None = None) -> np.ndarray:
    arr = np.asarray(values, dtype="float64")
    if mask is not None:
        if mask.shape != arr.shape:
            raise ValueError("Science and quality bands must have identical windows")
        arr = arr[np.asarray(mask, dtype=bool)]
    arr = arr.ravel()
    arr = arr[np.isfinite(arr)]
    if nodata is not None and math.isfinite(float(nodata)):
        arr = arr[arr != float(nodata)]
    return arr


def measure_target(
    raster_path: Path,
    product_type: str,
    longitude: float,
    latitude: float,
    *,
    radius_px: int = 3,
    minimum_valid_pixels: int = 3,
) -> dict[str, Any]:
    """Return local window statistics without interpreting them as a time series."""
    family = product_type.upper()
    if family not in {"GUNW", "GOFF"}:
        raise ValueError("product_type must be GUNW or GOFF")
    if not (math.isfinite(longitude) and math.isfinite(latitude)
            and -180 <= longitude <= 180 and -90 <= latitude <= 90):
        raise ValueError("Target must be finite WGS84 longitude/latitude")
    if radius_px < 0 or minimum_valid_pixels < 1:
        raise ValueError("radius_px must be non-negative and minimum_valid_pixels must be positive")

    with rasterio.open(raster_path) as ds:
        if ds.crs is None or ds.count < (2 if family == "GUNW" else 3):
            raise ValueError("Raster CRS or expected quality bands are missing")
        xs, ys = transform_coordinates("EPSG:4326", ds.crs, [longitude], [latitude])
        row, col = ds.index(xs[0], ys[0])
        total = (2 * radius_px + 1) ** 2
        if row < 0 or col < 0 or row >= ds.height or col >= ds.width:
            return {
                "schema_version": 1, "status": "TARGET_OUTSIDE_RASTER",
                "product_type": family, "source_raster": raster_path.name,
                "target": {"longitude": longitude, "latitude": latitude, "radius_px": radius_px,
                           "row": row, "column": col, "window_pixels": 0,
                           "requested_window_pixels": total, "valid_pixels": 0, "valid_fraction": 0.0},
                "measurements": {},
                "interpretation": "The target is not covered by this product's exported AOI raster; no value was inferred.",
            }
        row0, col0 = max(0, row - radius_px), max(0, col - radius_px)
        height = min(ds.height, row + radius_px + 1) - row0
        width = min(ds.width, col + radius_px + 1) - col0
        window = Window(col0, row0, width, height)
        window_cells_available = int(width * height)
        first = ds.read(1, window=window).astype("float64")
        if ds.nodata is not None:
            first[first == ds.nodata] = np.nan
        first[~np.isfinite(first)] = np.nan
        valid = np.isfinite(first)
        support = int(valid.sum())
        values = first[valid]
        tags = ds.tags()
        measurements: dict[str, Any] = {}
        if family == "GUNW":
            metric_name = "target_local_los_median_mm"
            metric_description = "GUNW LOS displacement"
            quality_name = "mean_coherence"
            quality_values = finite_values(ds.read(2, window=window), ds.nodata, valid)
            quality = float(np.mean(quality_values)) if quality_values.size >= minimum_valid_pixels else None
            quality_status = "VALID" if quality is not None and 0.0 <= quality <= 1.0 else "INSUFFICIENT_OR_INVALID"
            if quality_status != "VALID":
                quality = None
        else:
            metric_name = "target_local_range_median_mm"
            metric_description = "GOFF slant-range offset"
            second = ds.read(2, window=window).astype("float64")
            if ds.nodata is not None:
                second[second == ds.nodata] = np.nan
            second[~np.isfinite(second)] = np.nan
            second_values = second[valid & np.isfinite(second)]
            metric_azimuth = "target_local_azimuth_median_mm"
            measurements[metric_azimuth] = float(np.median(second_values)) if second_values.size >= minimum_valid_pixels else None
            quality_name = "mean_correlation"
            quality_values = finite_values(ds.read(3, window=window), ds.nodata, valid)
            quality = float(np.mean(quality_values)) if quality_values.size >= minimum_valid_pixels else None
            quality_status = "VALID" if quality is not None and 0.0 <= quality <= 1.0 else "INSUFFICIENT_OR_INVALID"
            if quality_status != "VALID":
                quality = None

        measurements[metric_name] = float(np.median(values)) if support >= minimum_valid_pixels else None
        measurements["p05_mm"] = float(np.percentile(values, 5)) if support >= minimum_valid_pixels else None
        measurements["p95_mm"] = float(np.percentile(values, 95)) if support >= minimum_valid_pixels else None
        measurements["robust_sigma_mm"] = float(1.4826 * np.median(np.abs(values - np.median(values)))) if support >= minimum_valid_pixels else None
        measurements[quality_name] = quality
        measurements["quality_valid_pixels"] = int(quality_values.size)
        measurements["quality_status"] = quality_status
        status = "MEASURED" if support >= minimum_valid_pixels else "INSUFFICIENT_TARGET_SUPPORT"
        ref_date = tags.get("reference") or None
        sec_date = tags.get("secondary") or None
        match = re.search(r"_(\d{8})_(\d{8})(?:_|\.)", raster_path.name)
        if match:
            ref_date = ref_date or datetime.strptime(match.group(1), "%Y%m%d").date().isoformat()
            sec_date = sec_date or datetime.strptime(match.group(2), "%Y%m%d").date().isoformat()
        return {
            "schema_version": 1, "status": status, "product_type": family,
            "source_raster": raster_path.name, "metric_name": metric_name,
            "metric_description": metric_description, "units": "mm",
            "target": {"longitude": longitude, "latitude": latitude, "radius_px": radius_px,
                       "row": row, "column": col, "window_pixels": window_cells_available,
                       "requested_window_pixels": total, "valid_pixels": support,
                       "valid_fraction": support / window_cells_available if window_cells_available else 0.0},
            "temporal_scope": {"reference_date": ref_date, "secondary_date": sec_date},
            "measurements": measurements,
            "reference_policy": tags.get("reference_policy", "per-product reader reference; a shared cross-pair datum is not verified"),
            "interpretation": (
                "Pair-specific local median in the reader's per-product reference frame. "
                "Do not connect this value into a cumulative trend unless all pairs are independently verified against one common datum. "
                "This metric is not vertical or full 3-D movement and is not a forecast."
            ),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raster", required=True, type=Path)
    parser.add_argument("--product-type", required=True, choices=("GUNW", "GOFF"))
    parser.add_argument("--longitude", required=True, type=float)
    parser.add_argument("--latitude", required=True, type=float)
    parser.add_argument("--radius-px", type=int, default=3)
    parser.add_argument("--minimum-valid-pixels", type=int, default=3)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = measure_target(args.raster, args.product_type, args.longitude, args.latitude,
                                radius_px=args.radius_px, minimum_valid_pixels=args.minimum_valid_pixels)
    except (OSError, ValueError, rasterio.errors.RasterioError) as exc:
        print("TARGET_MEASUREMENT_ERROR=" + type(exc).__name__ + ":" + str(exc)[:250])
        return 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    print("TARGET_MEASUREMENT_STATUS=" + str(result.get("status")))
    print("TARGET_MEASUREMENT_OUT=" + str(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
