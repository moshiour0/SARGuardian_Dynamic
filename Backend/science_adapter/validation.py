"""Evidence checks around the pinned SARGuardian readers.

These checks verify identity and retained numerical/geospatial contracts. They
do not independently establish the accuracy of a satellite measurement.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any


class ValidationError(ValueError):
    """A scientific artifact cannot safely pass its retention gate."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_name(record: dict) -> str:
    name = str(record.get("granule") or record.get("source") or "")
    if not name or "/" in name or "\\" in name or not name.startswith("NISAR_L2_"):
        raise ValidationError("Invalid exact NISAR granule identity.")
    return name if name.endswith(".h5") else name + ".h5"


def _day(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y%m%d")
    text = str(value)
    try:
        if re.fullmatch(r"\d{8}", text):
            return datetime.strptime(text, "%Y%m%d").strftime("%Y%m%d")
        return date.fromisoformat(text[:10]).strftime("%Y%m%d")
    except ValueError as exc:
        raise ValidationError("Missing or invalid acquisition date.") from exc


def _family(record: dict) -> str:
    family = str(record.get("productType") or record.get("product_type") or "")
    if family not in {"GUNW", "GOFF"} or f"_{family}_" not in source_name(record):
        raise ValidationError("Product family and exact granule identity disagree.")
    return family


def _scalar(value: Any, h5: Any = None) -> Any:
    """Lossless JSON representation of ordinary HDF5 metadata and references."""
    import numpy as np
    import h5py

    if isinstance(value, h5py.Reference):
        if not value:
            return {"hdf5_reference": None}
        return {"hdf5_reference": h5[value].name} if h5 is not None else {"hdf5_reference": "present"}
    if isinstance(value, np.ndarray):
        return _scalar(value.tolist(), h5)
    if isinstance(value, np.generic):
        return _scalar(value.item(), h5)
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return {"encoding": "hex", "value": value.hex()}
    if isinstance(value, (tuple, list)):
        return [_scalar(item, h5) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return {"nonfinite": str(value)}
    if isinstance(value, complex):
        return {"real": _scalar(value.real), "imaginary": _scalar(value.imag)}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def inventory_hdf5(raw: Path, record: dict) -> dict:
    """Inventory native HDF5 objects and cross-check available identification.

    No measurement array is cropped or converted. The whole-file hash binds
    this metadata inventory to the original downloaded byte stream.
    """
    import h5py

    raw = Path(raw)
    expected = source_name(record)
    family = _family(record)
    if raw.name != expected:
        raise ValidationError("Downloaded filename does not match the selected exact granule.")
    stamps = re.findall(r"_(\d{8})T\d{6}", expected)
    if len(stamps) != 4 or stamps[0] != _day(record["reference"]) or stamps[2] != _day(record["secondary"]):
        raise ValidationError("Granule acquisition dates do not match the selected catalogue record.")
    if stamps[2] <= stamps[0]:
        raise ValidationError("Secondary acquisition must follow the reference acquisition.")
    datasets: list[dict] = []
    groups: list[dict] = []
    links: list[dict] = []
    evidence: list[dict] = []
    with h5py.File(raw, "r") as h5:
        if f"/science/LSAR/{family}" not in h5:
            raise ValidationError("HDF5 science group disagrees with the selected product family.")
        visited: set[int] = set()

        def visit(group: Any) -> None:
            address = h5py.h5o.get_info(group.id).addr
            if address in visited:
                return
            visited.add(address)
            groups.append({"path": group.name, "attributes": {key: _scalar(value, h5) for key, value in group.attrs.items()}})
            for name in sorted(group.keys()):
                link = group.get(name, getlink=True)
                path = group.name.rstrip("/") + "/" + name
                if isinstance(link, h5py.ExternalLink):
                    raise ValidationError("External HDF5 links cannot be retained as a standalone source.")
                if isinstance(link, h5py.SoftLink):
                    if group.get(name) is None:
                        raise ValidationError("Unresolved HDF5 link in downloaded product.")
                    links.append({"path": path, "target": link.path, "type": "soft"})
                    continue
                item = group[name]
                if isinstance(item, h5py.Group):
                    visit(item)
                    continue
                if item.is_virtual or item.external:
                    raise ValidationError("Virtual/external HDF5 datasets require additional unretained files.")
                entry = {
                    "path": path, "shape": list(item.shape), "dtype": str(item.dtype),
                    "logical_bytes": int(item.size * item.dtype.itemsize),
                    "stored_bytes": int(item.id.get_storage_size()),
                    "chunks": list(item.chunks) if item.chunks else None,
                    "compression": item.compression, "compression_options": _scalar(item.compression_opts),
                    "shuffle": bool(item.shuffle), "fletcher32": bool(item.fletcher32),
                    "scaleoffset": item.scaleoffset, "fillvalue": _scalar(item.fillvalue, h5),
                    "attributes": {key: _scalar(value, h5) for key, value in item.attrs.items()},
                }
                if item.size <= 16 and "/identification/" in path:
                    value = _scalar(item[()], h5)
                    entry["value"] = value
                    while isinstance(value, list) and len(value) == 1:
                        value = value[0]
                    if name in {"granuleId", "granuleName", "productName"}:
                        actual = str(value)
                        actual = actual if actual.endswith(".h5") else actual + ".h5"
                        if actual != expected:
                            raise ValidationError("HDF5 identification contains a different exact granule.")
                        evidence.append({"path": path, "check": "exact_granule", "value": actual})
                    elif name == "productType":
                        if str(value) != family:
                            raise ValidationError("HDF5 identification product type disagrees with catalogue.")
                        evidence.append({"path": path, "check": "product_family", "value": str(value)})
                    elif name in {"referenceZeroDopplerStartTime", "secondaryZeroDopplerStartTime"}:
                        key = "reference" if name.startswith("reference") else "secondary"
                        if _day(value) != _day(record[key]):
                            raise ValidationError("HDF5 identification acquisition date disagrees with catalogue.")
                        evidence.append({"path": path, "check": key, "value": str(value)})
                datasets.append(entry)

        visit(h5)
    required = "unwrappedPhase" if family == "GUNW" else "slantRangeOffset"
    if not any(row["path"].endswith("/" + required) and len(row["shape"]) == 2 for row in datasets):
        raise ValidationError("Required native scientific measurement dataset is absent.")
    return {
        "schema_version": "sarguardian-hdf5-inventory/1", "status": "PASS",
        "source": expected, "product_type": family, "bytes": raw.stat().st_size,
        "sha256": sha256_file(raw), "reference": _day(record["reference"]),
        "secondary": _day(record["secondary"]), "dataset_count": len(datasets),
        "datasets": datasets, "groups": groups, "links": links,
        "identity_evidence": evidence,
        "identity_limit": None if any(item["check"] == "exact_granule" for item in evidence)
        else "Exact identity uses the authenticated catalogue URL and filename; the HDF5 has no exact-granule identifier.",
    }


def _ring(wkt: str) -> list[tuple[float, float]]:
    # API validation remains authoritative; keep this worker-side mask parser
    # independent of application authentication/database imports.
    match = re.fullmatch(r"\s*(?:SRID=4326;\s*)?POLYGON\s*\(\s*\(([^()]*)\)\s*\)\s*", wkt, re.I)
    if not match:
        raise ValidationError("Expected a single-ring EPSG:4326 AOI polygon.")
    try:
        ring = [tuple(float(number) for number in pair.split()) for pair in match.group(1).split(",")]
    except ValueError as exc:
        raise ValidationError("Invalid AOI coordinate.") from exc
    if len(ring) < 4 or ring[0] != ring[-1] or any(len(point) != 2 for point in ring):
        raise ValidationError("AOI must contain a closed longitude/latitude polygon.")
    if any(not math.isfinite(lon) or not math.isfinite(lat) or abs(lon) > 180 or abs(lat) > 90 for lon, lat in ring):
        raise ValidationError("AOI coordinates are outside EPSG:4326 bounds.")
    return ring


def _inside_polygon(lon: Any, lat: Any, ring: list[tuple[float, float]]) -> Any:
    import numpy as np

    inside = np.zeros(np.shape(lon), dtype=bool)
    boundary = np.zeros_like(inside)
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        cross = (lon - x1) * (y2 - y1) - (lat - y1) * (x2 - x1)
        length = math.hypot(x2 - x1, y2 - y1)
        boundary |= (np.abs(cross) <= 1e-9 * max(length, 1e-12)) & (lon >= min(x1, x2) - 1e-9) & (lon <= max(x1, x2) + 1e-9) & (lat >= min(y1, y2) - 1e-9) & (lat <= max(y1, y2) + 1e-9)
        if y1 != y2:
            inside ^= ((y1 > lat) != (y2 > lat)) & (lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1)
    return inside | boundary


def enforce_retention_aoi_mask(path: Path, aoi_wkt: str) -> dict:
    """Remove retained values outside the submitted polygon before validation.

    The pinned exporters produce a compact AOI *window*.  The window can
    include edge cells whose centres fall outside an irregular polygon.  Those
    cells must be represented as NaN in an artifact retained for a custom AOI;
    this routine only removes values and records the change in GeoTIFF tags.
    """
    import numpy as np
    import rasterio
    from pyproj import Transformer

    ring = _ring(aoi_wkt)
    path = Path(path)
    removed = 0
    valid_count = 0
    with rasterio.open(path, "r+") as src:
        if src.crs is None:
            raise ValidationError("Raster CRS is missing before custom-AOI retention masking.")
        affine = src.transform
        transformer = Transformer.from_crs(src.crs, "EPSG:4326", always_xy=True)
        for _, window in src.block_windows(1):
            data = src.read(window=window)
            rr, cc = np.indices((int(window.height), int(window.width)))
            xs = affine.c + (cc + window.col_off + .5) * affine.a
            ys = affine.f + (rr + window.row_off + .5) * affine.e
            lon, lat = transformer.transform(xs, ys)
            outside = ~_inside_polygon(lon, lat, ring)
            removed += int((np.isfinite(data) & outside).sum())
            if outside.any():
                data[:, outside] = np.nan
                src.write(data, window=window)
            valid_count += int(np.isfinite(data[0]).sum())
        src.update_tags(
            export_status="WRITTEN" if valid_count else "NO_VALID_PIXELS",
            valid_pixel_count=str(valid_count),
            retention_aoi_mask_enforced="true",
            retention_aoi_mask_removed_values=str(removed),
        )
    return {"status": "PASS", "removed_finite_values": removed, "valid_pixels": valid_count}


def validate_raster(path: Path, record: dict, aoi_wkt: str) -> dict:
    """Check retained values, source identity, CRS and actual AOI membership."""
    import numpy as np
    import rasterio
    from pyproj import Transformer

    family = _family(record)
    expected = source_name(record)
    ring = _ring(aoi_wkt)
    with rasterio.open(path) as src:
        tags = src.tags()
        if tags.get("source") != expected:
            raise ValidationError("Raster source identity does not match its exact granule.")
        if tags.get("aoi_mask_applied", "").lower() != "true" or tags.get("aoi_name") != "custom":
            raise ValidationError("Raster lacks explicit custom-AOI masking evidence.")
        if tags.get("reference") != _day(record["reference"]) or tags.get("secondary") != _day(record["secondary"]):
            raise ValidationError("Raster acquisition dates disagree with selected product.")
        if src.crs is None or src.width < 1 or src.height < 1:
            raise ValidationError("Raster CRS or dimensions are missing.")
        affine = src.transform
        if not all(math.isfinite(value) for value in affine) or affine.a <= 0 or affine.e >= 0 or affine.b != 0 or affine.d != 0:
            raise ValidationError("Raster must have a finite north-up geotransform.")
        if src.nodata is None or not math.isnan(src.nodata) or any(dtype not in {"float32", "float64"} for dtype in src.dtypes):
            raise ValidationError("Retained scientific rasters require floating values with NaN nodata.")
        quality = tags.get("artifact_role") == "quality_auxiliary"
        expected_bands = ("LOS displacement (mm)", "coherence") if family == "GUNW" else ("slant-range offset (mm)", "along-track offset (mm)", "correlation")
        if quality:
            if family != "GOFF" or not src.count or any(name not in {"signal-to-noise ratio", "slant-range offset variance (mm^2)"} for name in src.descriptions):
                raise ValidationError("Unexpected retained quality band contract.")
        elif tuple(src.descriptions) != expected_bands:
            raise ValidationError("Raster scientific band structure or units are incorrect.")
        transformer = Transformer.from_crs(src.crs, "EPSG:4326", always_xy=True)
        valid_count = 0
        aoi_grid_count = 0
        for _, window in src.block_windows(1):
            data = src.read(window=window)
            if np.isinf(data).any():
                raise ValidationError("Raster contains infinite scientific values.")
            rr, cc = np.indices((int(window.height), int(window.width)))
            xs = affine.c + (cc + window.col_off + .5) * affine.a
            ys = affine.f + (rr + window.row_off + .5) * affine.e
            lon, lat = transformer.transform(xs, ys)
            inside = _inside_polygon(lon, lat, ring)
            aoi_grid_count += int(inside.sum())
            finite = np.isfinite(data)
            if (finite & ~inside).any():
                raise ValidationError("Raster contains finite pixels outside the submitted AOI.")
            valid = finite[0]
            valid_count += int(valid.sum())
            if quality:
                if (data[finite] < 0).any():
                    raise ValidationError("Quality values/variances cannot be negative.")
            else:
                if not all(np.array_equal(valid, finite[index]) for index in range(1, src.count)):
                    raise ValidationError("Displacement and quality bands have different validity masks.")
                coherence = data[-1][valid]
                if ((coherence < 0) | (coherence > 1)).any():
                    raise ValidationError("Coherence/correlation must be in [0, 1].")
        if not aoi_grid_count:
            raise ValidationError("Raster has no grid cells intersecting the requested AOI.")
        expected_status = "WRITTEN" if valid_count else "NO_VALID_PIXELS"
        if tags.get("export_status") != expected_status or tags.get("valid_pixel_count") != str(valid_count):
            raise ValidationError("Raster validity counts/status disagree with actual band values.")
        if tags.get("grid_policy") not in {"common_aoi_lattice", "per_product_aoi_window_not_common_lattice"}:
            raise ValidationError("Raster grid provenance is missing.")
        if family == "GUNW":
            try:
                wavelength = float(tags["wavelength_m"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValidationError("GUNW wavelength metadata is missing.") from exc
            if not math.isfinite(wavelength) or wavelength <= 0:
                raise ValidationError("GUNW wavelength must be finite and positive.")
        return {
            "schema_version": "sarguardian-raster-validation/1", "status": "PASS",
            "science_status": "PASS" if valid_count else "NOT_MEASURABLE",
            "source": expected, "file": Path(path).name, "product_type": family,
            "valid_pixels": valid_count, "aoi_grid_pixels": aoi_grid_count,
            "aoi_coverage_percent": 100 * valid_count / aoi_grid_count,
            "shape": [src.height, src.width], "crs": src.crs.to_string(),
            "transform": list(affine)[:6], "bands": list(src.descriptions),
            "nodata": "NaN", "tags": tags, "sha256": sha256_file(path),
            "bytes": Path(path).stat().st_size,
            "limitation": "Structural and retained-value validation; not independent geophysical validation.",
        }


def write_raster_summary(exports: list[Path], record: dict, output_dir: Path) -> Path:
    """Write and reread numeric CSV summaries of actual retained pixels."""
    import numpy as np
    import rasterio

    fields = ["source", "product_type", "reference", "secondary", "raster", "band", "quantity", "valid_pixels", "minimum", "p05", "median", "p95", "maximum", "mean", "std", "status"]
    rows = []
    for path in sorted(map(Path, exports)):
        with rasterio.open(path) as src:
            if src.tags().get("source") != source_name(record):
                raise ValidationError("CSV source raster identity mismatch.")
            for band in range(1, src.count + 1):
                array = src.read(band, masked=True).filled(np.nan)
                if np.isinf(array).any():
                    raise ValidationError("Cannot summarize infinite raster values.")
                values = array[np.isfinite(array)].astype(np.float64)
                row = {"source": source_name(record), "product_type": _family(record), "reference": _day(record["reference"]), "secondary": _day(record["secondary"]), "raster": path.name, "band": band, "quantity": src.descriptions[band - 1], "valid_pixels": int(values.size), "status": "PASS" if values.size else "NOT_MEASURABLE"}
                if values.size:
                    row.update(dict(zip(["minimum", "p05", "median", "p95", "maximum", "mean", "std"], [float(np.min(values)), *map(float, np.percentile(values, [5, 50, 95])), float(np.max(values)), float(np.mean(values)), float(np.std(values))])))
                rows.append(row)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "raster-statistics.csv"
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != fields:
            raise ValidationError("Retained summary CSV schema mismatch.")
        retained = list(reader)
    if len(retained) != len(rows):
        raise ValidationError("Retained summary CSV row count mismatch.")
    for expected, actual in zip(rows, retained):
        for key, value in expected.items():
            if isinstance(value, (float, int)):
                if not math.isfinite(float(actual[key])) or float(actual[key]) != float(value):
                    raise ValidationError("Retained summary CSV numeric value mismatch.")
            elif str(value) != actual[key]:
                raise ValidationError("Retained summary CSV identity mismatch.")
    return path


def retain_quality_raster(result: dict, family: str, output_dir: Path) -> list[Path]:
    """Preserve GOFF SNR/range variance on each existing export's exact grid.

    The upstream reader does not return along-track variance; its absence must
    not be interpreted as zero uncertainty. GUNW fit uncertainty is downstream.
    """
    import numpy as np
    import rasterio

    if family != "GOFF":
        return []
    paths = []
    for target in sorted(Path(output_dir).glob("GOFF_*.tif")):
        if target.name.endswith("_quality.tif"):
            continue
        with rasterio.open(target) as src:
            layer = result["layers"].get(src.tags().get("layer"))
            if layer is None:
                raise ValidationError("Quality retention cannot identify the exported GOFF layer.")
            xs, ys = np.asarray(layer["xs"]), np.asarray(layer["ys"])
            if len(xs) < 2 or len(ys) < 2:
                raise ValidationError("Quality source grid is degenerate.")
            out_x = src.transform.c + (np.arange(src.width) + .5) * src.transform.a
            out_y = src.transform.f + (np.arange(src.height) + .5) * src.transform.e
            cols = np.rint((out_x - xs[0]) / (xs[1] - xs[0])).astype(int)
            rows = np.rint((out_y - ys[0]) / (ys[1] - ys[0])).astype(int)
            if not np.allclose(xs[0] + cols * (xs[1] - xs[0]), out_x, rtol=0, atol=abs(src.transform.a) * 1e-6) or not np.allclose(ys[0] + rows * (ys[1] - ys[0]), out_y, rtol=0, atol=abs(src.transform.e) * 1e-6):
                raise ValidationError("Quality raster would require unapproved resampling.")
            in_rows, in_cols = (rows >= 0) & (rows < len(ys)), (cols >= 0) & (cols < len(xs))
            bands = []
            primary_valid = np.isfinite(src.read(1))
            for key, factor, description in [("snr", 1.0, "signal-to-noise ratio"), ("range_var", 1e6, "slant-range offset variance (mm^2)")]:
                if layer.get(key) is None:
                    continue
                values = np.asarray(layer[key])
                if values.shape != np.asarray(layer["valid"]).shape:
                    raise ValidationError("GOFF quality array shape disagrees with reader mask.")
                array = np.full((src.height, src.width), np.nan, dtype="float32")
                array[np.ix_(in_rows, in_cols)] = values[np.ix_(rows[in_rows], cols[in_cols])] * factor
                array[~primary_valid] = np.nan
                if np.isinf(array).any() or (array[np.isfinite(array)] < 0).any():
                    raise ValidationError("Invalid GOFF uncertainty/SNR values in retained pixels.")
                bands.append((array, description))
            if not bands:
                continue
            profile = src.profile.copy()
            profile.update(count=len(bands), dtype="float32", nodata=np.nan)
            tags = src.tags() | {"artifact_role": "quality_auxiliary", "variance_availability": "range variance only; along-track variance is not exposed by pinned reader"}
            count = int(np.isfinite(bands[0][0]).sum())
            tags.update(valid_pixel_count=str(count), export_status="WRITTEN" if count else "NO_VALID_PIXELS")
        quality_path = target.with_name(target.stem + "_quality.tif")
        with rasterio.open(quality_path, "w", **profile) as dst:
            for index, (array, description) in enumerate(bands, 1):
                dst.write(array, index)
                dst.set_band_description(index, description)
            dst.update_tags(**tags)
        paths.append(quality_path)
    return paths


def create_raster_preview(path: Path, output_dir: Path) -> Path:
    """Render a labelled image from measured retained displacement values."""
    import numpy as np
    import rasterio
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import pyplot as plt
    from rasterio.enums import Resampling

    with rasterio.open(path) as src:
        scale = min(1.0, 900 / max(src.width, src.height))
        values = src.read(1, out_shape=(max(1, round(src.height * scale)), max(1, round(src.width * scale))), masked=True, resampling=Resampling.nearest)
        figure, axes = plt.subplots(figsize=(8, 6), layout="constrained")
        finite = values.compressed()
        finite = finite[np.isfinite(finite)]
        if len(finite):
            limit = max(float(np.percentile(np.abs(finite), 98)), 1e-6)
            picture = axes.imshow(values, cmap="RdBu_r", vmin=-limit, vmax=limit, extent=[src.bounds.left, src.bounds.right, src.bounds.bottom, src.bounds.top])
            figure.colorbar(picture, ax=axes, label=src.descriptions[0] or "displacement (mm)")
        else:
            axes.text(.5, .5, "No measurable AOI pixels", transform=axes.transAxes, ha="center")
        axes.set_title("Research displacement screening\n" + str(src.tags().get("reference", "")) + " → " + str(src.tags().get("secondary", "")))
        axes.set_xlabel(str(src.crs) + " x")
        axes.set_ylabel(str(src.crs) + " y")
        figure.text(.5, .005, "Research output — not a verified hazard warning", ha="center", fontsize=8)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        target = output_dir / (Path(path).stem + "_preview.png")
        figure.savefig(target, dpi=120)
        plt.close(figure)
    return target
