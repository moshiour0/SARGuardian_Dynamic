#!/usr/bin/env python3
"""Lossless, dependency-driven NISAR L2 HDF5 subset archive builder.

This tool does NOT delete raw sources and does NOT upload to Google Drive.
It creates a structure-preserving HDF5 subset plus a manifest. A caller must
verify the generated archive, upload it to durable storage, verify the remote
object size/checksum, and only then remove the temporary raw source.

Preservation policy v1:
  * All source/group/dataset attributes are recorded in the sidecar inventory.
  * All group attributes are copied to the same path in the curated HDF5.
  * All scalar and 1-D datasets are copied (metadata, coordinate axes, IDs, etc.).
  * GOFF: every dataset under every pixelOffsets grid/layer/polarisation is copied.
  * GUNW: every dataset under every unwrappedInterferogram and pixelOffsets group is copied.
  * GUNW/GOFF: every dataset under metadata/radarGrid is copied.
  * Conservative compact default: preserve every source dataset except each
    multidimensional GUNW complex wrappedInterferogram raster, and derive a
    same-grid wrappedPhase array in radians with chunked numpy.angle. The
    original complex raster remains available with --include-wrapped.
  * Companion datasets (coherence/offsets/coordinates) stay; all original
    exclusions are recorded with path/shape/dtype/attributes in the manifest.
  * Actual HDF5 dimension-scale flags/names and links are reconstructed and
    verified, including valid empty scale names found in real NISAR products.

Numeric and fixed-string values are copied without scale/offset conversion;
compression is lossless gzip. The source HDF5 hierarchy and dataset paths are
kept so the existing SARGuardian readers can be pointed at the curated file.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import h5py
import numpy as np

SCHEMA_VERSION = "sarguardian-science-archive/1.2"
CHUNK_TARGET_BYTES = 16 * 1024 * 1024


def sha256_file(path: Path, block_size: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            block = f.read(block_size)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _is_reference_dtype(dtype: np.dtype) -> bool:
    """Return true for direct or nested/compound HDF5 object-reference dtypes."""
    try:
        if h5py.check_dtype(ref=dtype) is not None:
            return True
        if dtype.fields:
            return any(_is_reference_dtype(field_info[0]) for field_info in dtype.fields.values())
        if dtype.subdtype:
            return _is_reference_dtype(dtype.subdtype[0])
    except Exception:
        return False
    return False


def _json_value(value: Any) -> Any:
    """Convert HDF5 attr values to stable, JSON-serialisable representations."""
    if isinstance(value, h5py.RegionReference):
        return {"__hdf5_region_reference__": True}
    if isinstance(value, h5py.Reference):
        return {"__hdf5_object_reference__": True}
    if isinstance(value, np.ndarray):
        if _is_reference_dtype(value.dtype):
            return {"__hdf5_reference_array__": True, "shape": list(value.shape), "dtype": str(value.dtype)}
        if value.dtype.kind in ("S", "U", "O"):
            return [_json_value(v) for v in value.tolist()]
        if value.dtype.fields:
            rows = value.tolist()
            names = list(value.dtype.names or ())
            def structured_row(row):
                return {name: _json_value(row[i]) for i, name in enumerate(names)}
            return {"__structured_array__": [structured_row(row) for row in rows], "dtype": str(value.dtype), "shape": list(value.shape)}
        return {"__ndarray__": value.tolist(), "dtype": str(value.dtype), "shape": list(value.shape)}
    if isinstance(value, np.generic):
        return _json_value(value.item())
    if isinstance(value, bytes):
        try:
            return {"__bytes_utf8__": value.decode("utf-8")}
        except UnicodeDecodeError:
            return {"__bytes_hex__": value.hex()}
    if isinstance(value, str) or value is None or isinstance(value, (bool, int, float)):
        if isinstance(value, float) and not np.isfinite(value):
            return {"__float__": repr(value)}
        return value
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    return {"__repr__": repr(value), "__type__": type(value).__name__}


def attrs_json(obj: Any) -> dict[str, Any]:
    return {str(k): _json_value(obj.attrs[k]) for k in sorted(obj.attrs.keys())}


def _canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def attrs_digest(obj: Any) -> str:
    return hashlib.sha256(_canonical_json(attrs_json(obj))).hexdigest()


def _logical_chunk_bytes(block: Any, dtype: np.dtype) -> bytes:
    arr = np.asarray(block)
    if _is_reference_dtype(dtype) or arr.dtype.kind == "O":
        # Variable-length string datasets are objects in NumPy; pointer bytes
        # are unstable, so hash their decoded logical values instead.
        return _canonical_json(_json_value(arr))
    return np.ascontiguousarray(arr).tobytes(order="C")


def _slices_for_dataset(ds: h5py.Dataset, target_bytes: int = CHUNK_TARGET_BYTES) -> Iterable[tuple]:
    """Use deterministic row-major slabs independent of HDF5 chunk layout.

    Digesting via Dataset.iter_chunks() would make the byte stream depend on
    chunk shape, so a losslessly re-chunked archive could get a different hash
    for identical logical values. These slabs always span every trailing axis.
    """
    if ds.shape == ():
        yield ()
        return
    dtype_size = max(int(ds.dtype.itemsize), 1)
    trailing = int(np.prod(ds.shape[1:], dtype=np.int64)) if len(ds.shape) > 1 else 1
    rows = max(1, min(ds.shape[0], target_bytes // max(dtype_size * trailing, 1)))
    for start in range(0, ds.shape[0], rows):
        yield (slice(start, min(start + rows, ds.shape[0])),) + tuple(slice(None) for _ in ds.shape[1:])


def dataset_digest(ds: h5py.Dataset) -> str:
    h = hashlib.sha256()
    for slc in _slices_for_dataset(ds):
        h.update(_logical_chunk_bytes(ds[slc], ds.dtype))
    return h.hexdigest()


def _value_equal(a: Any, b: Any) -> bool:
    aa, bb = np.asarray(a), np.asarray(b)
    if aa.shape != bb.shape:
        return False
    if _is_reference_dtype(aa.dtype) or aa.dtype.kind == "O" or _is_reference_dtype(bb.dtype) or bb.dtype.kind == "O":
        return _canonical_json(_json_value(aa)) == _canonical_json(_json_value(bb))
    if aa.dtype.kind in "fc" or bb.dtype.kind in "fc":
        return bool(np.array_equal(aa, bb, equal_nan=True))
    return bool(np.array_equal(aa, bb))


def _dataset_record(path: str, ds: h5py.Dataset) -> dict[str, Any]:
    try:
        storage_bytes = int(ds.id.get_storage_size())
    except Exception:
        storage_bytes = None
    try:
        logical_nbytes = int(ds.size * ds.dtype.itemsize)
    except Exception:
        logical_nbytes = None
    return {
        "path": "/" + path.lstrip("/"),
        "shape": list(ds.shape),
        "ndim": int(ds.ndim),
        "dtype": str(ds.dtype),
        "logical_nbytes_estimate": logical_nbytes,
        "source_storage_nbytes": storage_bytes,
        "dataset_creation_properties": {
            "maxshape": list(ds.maxshape) if ds.maxshape is not None else None,
            "chunks": list(ds.chunks) if ds.chunks is not None else None,
            "compression": ds.compression,
            "compression_opts": _json_value(ds.compression_opts),
            "shuffle": bool(ds.shuffle),
            "fletcher32": bool(ds.fletcher32),
            "scaleoffset": ds.scaleoffset,
            "fillvalue": _json_value(ds.fillvalue),
            "is_virtual": bool(ds.is_virtual),
            "external_storage": _json_value(ds.external),
        },
        "attrs": attrs_json(ds),
        "attrs_sha256": attrs_digest(ds),
    }


def discover_noncanonical_links(src: h5py.File) -> list[dict[str, str]]:
    """Find links/aliases that a path-preserving copy would otherwise omit.

    Hard-linked objects with more than one public path, soft links, and external
    links are deliberately fail-closed for the first archive version. NISAR
    products normally use ordinary hierarchy paths; if an exception appears,
    inventory it and require an explicit implementation rather than silently
    changing source semantics.
    """
    found: list[dict[str, str]] = []
    seen_addresses: dict[int, str] = {}

    def obj_addr(obj: Any) -> int | None:
        try:
            return int(h5py.h5o.get_info(obj.id).addr)
        except Exception:
            return None

    def walk_group(group: h5py.Group, prefix: str) -> None:
        for leaf in group.keys():
            full = f"{prefix.rstrip('/')}/{leaf}" if prefix != "/" else f"/{leaf}"
            link = group.get(leaf, getlink=True)
            if isinstance(link, h5py.SoftLink):
                found.append({"path": full, "type": "soft_link", "target": str(link.path)})
                continue
            if isinstance(link, h5py.ExternalLink):
                found.append({"path": full, "type": "external_link", "target_file": str(link.filename), "target_path": str(link.path)})
                continue
            try:
                obj = group[leaf]
            except Exception as exc:
                found.append({"path": full, "type": "unreadable_link", "target": type(exc).__name__})
                continue
            address = obj_addr(obj)
            if address is not None and address in seen_addresses:
                found.append({"path": full, "type": "hard_link_alias", "target_path": seen_addresses[address]})
                continue
            if address is not None:
                seen_addresses[address] = full
            if isinstance(obj, h5py.Group):
                walk_group(obj, full)

    root_address = obj_addr(src)
    if root_address is not None:
        seen_addresses[root_address] = "/"
    walk_group(src, "/")
    return found


def discover_inventory(src: h5py.File) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    datasets: list[dict[str, Any]] = []
    groups: list[str] = ["/"]
    def visit(name: str, obj: Any) -> None:
        if isinstance(obj, h5py.Group):
            groups.append("/" + name.strip("/"))
        elif isinstance(obj, h5py.Dataset):
            datasets.append(_dataset_record(name, obj))
    src.visititems(visit)
    groups = sorted(set(groups))
    root_meta = {"attrs": attrs_json(src), "attrs_sha256": attrs_digest(src)}
    return sorted(datasets, key=lambda x: x["path"]), groups, root_meta


def infer_product_type(src: h5py.File, filename: str) -> str:
    low_name = filename.upper()
    paths: list[str] = []
    src.visititems(lambda name, obj: paths.append(name.lower()) if isinstance(obj, h5py.Dataset) else None)
    has_goff = any("pixeloffsets" in p and "slantrangeoffset" in p for p in paths) or "_GOFF_" in low_name
    has_gunw = any("unwrappedphase" in p for p in paths) or "_GUNW_" in low_name
    if has_goff and not has_gunw:
        return "GOFF"
    if has_gunw and not has_goff:
        return "GUNW"
    if has_goff and has_gunw:
        raise ValueError("Product contains both GOFF and GUNW signatures; set --product-type explicitly and inspect schema")
    raise ValueError("Could not infer GUNW/GOFF from HDF5 dataset paths or filename")


def _has_path_segment(path: str, segment: str) -> bool:
    return segment.lower() in [p.lower() for p in path.strip("/").split("/")]


def selection_reason(
    path: str, ndim: int, family: str, include_wrapped: bool = False,
    retention_profile: str = "engine-ready",
) -> str | None:
    """Return an explicit dataset-retention reason for a profile.

    engine-ready preserves arrays used by the pinned current engine:
      * all scalar/1-D metadata and coordinate axes;
      * all unwrapped-interferogram datasets used by GUNW readers/reference;
      * all GUNW/GOFF pixelOffsets grids (including future quality checks);
      * the LOS geometry arrays directly opened by geometry_merge.los_from_product.

    It excludes high-posting GUNW wrapped-grid rasters and other multidimensional
    fields not consumed by the pinned processing paths. The full source inventory
    and source HDF5 SHA-256 remain in the manifest. Use full-lossless when every
    original dataset must be retained for unrestricted future reprocessing.
    """
    if retention_profile not in {"engine-ready", "full-lossless"}:
        raise ValueError("retention_profile must be engine-ready or full-lossless")

    normalized = "/" + path.strip("/").lower()
    if retention_profile == "full-lossless":
        return "full_lossless_source_dataset"

    # Wrapped opt-in means the complete high-posting wrapped grid family, not
    # only the complex array: coherence, masks, coordinate axes and projection
    # are needed together for meaningful future wrapped-domain analyses.
    if family == "GUNW" and "/wrappedinterferogram/" in normalized and include_wrapped:
        return "wrapped_grid_retained_by_explicit_include_wrapped"

    # Scalars and vectors across the hierarchy carry schema, dates, axes, CRS,
    # frequency, calibration and provenance needed by current or future readers.
    if ndim <= 1:
        return "all_scalar_and_1d_metadata_axes"

    if family == "GUNW":
        if "/unwrappedinterferogram/" in normalized:
            return "GUNW_reader_phase_quality_and_grid_arrays"
        if "/pixeloffsets/" in normalized:
            return "GUNW_pixelOffsets_crosscheck_and_future_quality_arrays"
        if "/metadata/radargrid/" in normalized:
            leaf = normalized.rsplit("/", 1)[-1]
            if leaf in {"losunitvectorx", "losunitvectory", "incidenceangle"}:
                return "geometry_merge_direct_LOS_geometry_dependency"
            return None
        return None

    if family == "GOFF":
        if "/pixeloffsets/" in normalized:
            return "GOFF_reader_all_layer_polarization_offsets_quality_and_coordinates"
        return None

    return None

def exclusion_reason(
    path: str, family: str, retention_profile: str, include_wrapped: bool,
) -> str:
    """Explain a profile exclusion in terms of the audited science dependency."""
    normalized = "/" + path.strip("/").lower()
    if retention_profile == "full-lossless":
        return "Unexpected exclusion in full-lossless profile; archive must fail closed."
    if family == "GUNW" and "/wrappedinterferogram/" in normalized:
        if not include_wrapped:
            return (
                "Excluded from engine-ready: the pinned current src/ code does not "
                "read this multidimensional high-posting wrappedInterferogram-grid "
                "array. The full original inventory and attrs are retained in sidecars; "
                "use --include-wrapped or --retention-profile full-lossless when future "
                "science needs this array."
            )
    if family == "GUNW" and "/metadata/radargrid/" in normalized:
        leaf = normalized.rsplit("/", 1)[-1]
        if leaf not in {"losunitvectorx", "losunitvectory", "incidenceangle"}:
            return (
                "Excluded from engine-ready: pinned geometry_merge.los_from_product "
                "reads LOS unit vector X/Y and incidenceAngle plus coordinates and "
                "projection; this other multidimensional radarGrid field is not read "
                "by the pinned consumer. Full source schema/attrs remain in sidecars."
            )
    if family == "GUNW" and "/unwrappedinterferogram/" in normalized:
        return "Unexpected exclusion inside a required GUNW unwrappedInterferogram grid; fail closed."
    if "/pixeloffsets/" in normalized:
        return "Unexpected exclusion inside a retained pixelOffsets grid; fail closed."
    return (
        "Excluded from engine-ready because source audit found no reader or downstream "
        "processing path in the pinned src/ engine that consumes this multidimensional "
        "array. Its path, shape, dtype, size and attrs are retained in the source "
        "inventory; use full-lossless for unrestricted future reprocessing."
    )


def _is_dimension_scale(obj: Any) -> bool:
    """Return whether a dataset is a real HDF5 dimension scale."""
    if not isinstance(obj, h5py.Dataset):
        return False
    try:
        return bool(h5py.h5ds.is_scale(obj.id))
    except Exception:
        return False


def _dimension_scale_name(ds: h5py.Dataset) -> str:
    """Read the stored scale label, including valid empty labels.

    Some valid NISAR coordinate scales have NAME=b''.  H5DSget_scale_name()
    raises an HDF5 RuntimeError for those scales in h5py, so read the actual
    attribute instead of relying on that API.
    """
    if "NAME" not in ds.attrs:
        raise ValueError(f"Dimension scale {ds.name} has no NAME attribute")
    value = ds.attrs["NAME"]
    if isinstance(value, (bytes, np.bytes_)):
        return bytes(value).decode("utf-8", errors="surrogateescape")
    return str(value)


def _ordinary_attrs_json(obj: Any) -> dict[str, Any]:
    """Attributes whose values are portable across separate HDF5 files.

    DIMENSION_LIST and REFERENCE_LIST contain file-local HDF5 object references.
    Their actual references must be rebuilt, and are checked separately as
    dimension-scale relationships, never copied or compared byte-for-byte.
    """
    return {
        key: value for key, value in attrs_json(obj).items()
        if key not in {"DIMENSION_LIST", "REFERENCE_LIST"}
    }


def _dimension_links(obj: h5py.Dataset) -> list[dict[str, Any]]:
    """Describe axis labels and attached scales by stable dataset path."""
    result: list[dict[str, Any]] = []
    for axis in range(obj.ndim):
        scales = sorted(scale.name for _name, scale in obj.dims[axis].items())
        result.append({"axis": axis, "label": str(obj.dims[axis].label or ""), "scales": scales})
    return result


def _copy_attr_set(src_obj: Any, dst_obj: Any, path: str, skipped: list[dict[str, str]]) -> None:
    # HDF5 dimension scales contain two kinds of non-portable bookkeeping:
    # DIMENSION_LIST/REFERENCE_LIST file-local references, and CLASS/NAME,
    # which must be regenerated with make_scale() to create a true scale.
    internal_ref_attrs = {"DIMENSION_LIST", "REFERENCE_LIST"}
    scale_attrs = {"CLASS", "NAME"} if _is_dimension_scale(src_obj) else set()
    for key in src_obj.attrs.keys():
        if key in internal_ref_attrs:
            skipped.append({"object_path": path or "/", "attribute": str(key), "reason": "HDF5 internal object references are rebuilt by HDF5 and cannot be copied cross-file safely"})
            continue
        if key in scale_attrs:
            skipped.append({"object_path": path or "/", "attribute": str(key), "reason": "HDF5 dimension-scale marker is recreated with make_scale() to preserve scale semantics"})
            continue
        val = src_obj.attrs[key]
        if isinstance(val, h5py.Reference) or isinstance(val, h5py.RegionReference) or _is_reference_dtype(np.asarray(val).dtype):
            raise ValueError(
                f"Non-standard HDF5 reference attribute {path or '/'}@{key}; "
                "the archive tool can rebuild only HDF5 dimension-scale bookkeeping "
                "(DIMENSION_LIST/REFERENCE_LIST). Refusing to silently drop metadata."
            )
        try:
            dst_obj.attrs.create(key, val, dtype=src_obj.attrs.get_id(key).dtype)
        except Exception as exc:
            try:
                dst_obj.attrs[key] = val
            except Exception as inner_exc:
                raise ValueError(
                    f"Could not preserve attribute {path or '/'}@{key}; "
                    "refusing an incomplete archive"
                ) from inner_exc


def _iter_paths(src: h5py.File) -> tuple[list[str], list[str]]:
    ds_paths: list[str] = []
    group_paths: list[str] = ["/"]
    def visit(name: str, obj: Any) -> None:
        full = "/" + name.strip("/")
        if isinstance(obj, h5py.Group):
            group_paths.append(full)
        elif isinstance(obj, h5py.Dataset):
            ds_paths.append(full)
    src.visititems(visit)
    return sorted(set(ds_paths)), sorted(set(group_paths))


def _choose_chunk_shape(shape: tuple[int, ...], dtype: np.dtype) -> tuple[int, ...]:
    if not shape:
        return ()
    item = max(int(dtype.itemsize), 1)
    chunks = list(shape)
    while int(np.prod(chunks, dtype=np.int64)) * item > CHUNK_TARGET_BYTES:
        idx = max(range(len(chunks)), key=lambda i: chunks[i])
        if chunks[idx] <= 1:
            break
        chunks[idx] = max(1, (chunks[idx] + 1) // 2)
    return tuple(chunks)


def _copy_dataset_losslessly(src_ds: h5py.Dataset, dst_group: h5py.Group, leaf: str,
                             skipped_attrs: list[dict[str, str]], manifest_dataset: dict[str, Any]) -> h5py.Dataset:
    """Copy a complete HDF5 dataset with HDF5's native object-copy operation.

    H5Ocopy preserves values, dtype, shape/maxshape, fill value, chunk layout,
    compression filters and stored chunks without routing every pixel through
    Python and recompressing the whole array. Ordinary attributes are copied
    explicitly afterward because HDF5 dimension-scale references are file-local
    and must be rebuilt against destination objects.
    """
    if leaf in dst_group:
        raise ValueError(f"Destination dataset already exists: {dst_group.name}/{leaf}")
    dst_group.file.copy(src_ds, dst_group, name=leaf, without_attrs=True)
    dst_ds = dst_group[leaf]
    _copy_attr_set(src_ds, dst_ds, manifest_dataset["path"], skipped_attrs)
    return dst_ds


def build_archive(source: Path, archive: Path, manifest_path: Path,
                  product_type: str = "auto", include_wrapped: bool = False,
                  retention_profile: str = "engine-ready",
                  derive_wrapped_phase: bool = False) -> dict[str, Any]:
    source = source.resolve()
    archive = archive.resolve()
    manifest_path = manifest_path.resolve()
    if source == archive or source == manifest_path:
        raise ValueError("source, archive, and manifest must be different files")
    archive.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    if archive.exists():
        raise FileExistsError(f"Archive output already exists; refusing to overwrite: {archive}")
    if manifest_path.exists():
        raise FileExistsError(f"Manifest output already exists; refusing to overwrite: {manifest_path}")

    with h5py.File(source, "r") as src:
        family = infer_product_type(src, source.name) if product_type == "auto" else product_type.upper()
        if family not in {"GUNW", "GOFF"}:
            raise ValueError("product_type must be auto, GUNW or GOFF")
        if retention_profile not in {"engine-ready", "full-lossless"}:
            raise ValueError("retention_profile must be engine-ready or full-lossless")
        noncanonical_links = discover_noncanonical_links(src)
        if noncanonical_links:
            preview = "; ".join(f"{x['path']} ({x['type']})" for x in noncanonical_links[:10])
            raise ValueError("Non-canonical HDF5 links/aliases require explicit preservation logic; refusing archive: " + preview)
        if int(src.userblock_size or 0) > 0:
            raise ValueError(
                f"Source HDF5 has a {src.userblock_size}-byte user block outside the dataset hierarchy; "
                "archive tool does not yet preserve user blocks, refusing silent loss."
            )
        records, group_paths, root_meta = discover_inventory(src)
        # Virtual datasets and external raw storage depend on data outside this
        # HDF5. Until those dependencies can be bundled and verified explicitly,
        # fail rather than create a deceptively self-contained archive.
        special_storage = []
        def _check_storage(name: str, obj: Any) -> None:
            if isinstance(obj, h5py.Dataset):
                if bool(obj.is_virtual):
                    special_storage.append(f"/{name}:virtual_dataset")
                if obj.external:
                    special_storage.append(f"/{name}:external_raw_storage")
        src.visititems(_check_storage)
        if special_storage:
            raise ValueError("External/virtual dataset storage needs explicit retention support; refusing incomplete archive: " + ", ".join(special_storage[:20]))
        group_attribute_records = [
            {"path": gp, "attrs": attrs_json(src[gp]), "attrs_sha256": attrs_digest(src[gp])}
            for gp in group_paths if gp != "/"
        ]
        source_datasets = {"/" + k.strip("/"): src[k] for k in src.keys()}  # only used to assert openability
        del source_datasets
        chosen: dict[str, str] = {}
        excluded: list[dict[str, Any]] = []
        for rec in records:
            reason = selection_reason(rec["path"], rec["ndim"], family, include_wrapped, retention_profile)
            if reason:
                chosen[rec["path"]] = reason
            else:
                reason_text = exclusion_reason(rec["path"], family, retention_profile, include_wrapped)
                if retention_profile == "full-lossless":
                    raise RuntimeError(f"full-lossless profile unexpectedly excluded {rec['path']}")
                if reason_text.startswith("Unexpected exclusion"):
                    raise RuntimeError(reason_text + ": " + rec["path"])
                excluded.append({**rec, "retention_status": "excluded_profile_dependency", "reason": reason_text})

        # Never silently copy an HDF5 reference dataset. Its values refer to
        # object addresses in the source file; a byte/value hash of the raw
        # references does not prove that the targets still resolve in the new
        # file. Current SARGuardian readers do not require such datasets, so
        # stop and require an explicit mapping if one appears in retained data.
        reference_paths = [p for p in chosen if _is_reference_dtype(src[p].dtype)]
        if reference_paths:
            raise ValueError(
                "Retained dataset(s) contain HDF5 object/region references; refusing "
                "to build an archive until their targets are explicitly remapped: "
                + ", ".join(reference_paths[:20])
            )

        if family == "GUNW" and not any("unwrappedphase" in p.lower() for p in chosen):
            raise ValueError("No unwrappedPhase dataset selected; refusing to create incomplete GUNW archive")
        if family == "GOFF" and not any("slantrangeoffset" in p.lower() for p in chosen):
            raise ValueError("No slantRangeOffset dataset selected; refusing to create incomplete GOFF archive")

        skipped_attrs: list[dict[str, str]] = []
        selected_records: list[dict[str, Any]] = []
        # Build a temporary file beside the target, then atomically rename it.
        with tempfile.NamedTemporaryFile(prefix=archive.name + ".", suffix=".tmp", dir=archive.parent, delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            with h5py.File(tmp_path, "w") as dst:
                _copy_attr_set(src, dst, "/", skipped_attrs)
                # Preserve the complete group skeleton and all group attributes,
                # even where a large raster is intentionally excluded.
                for gp in group_paths:
                    if gp == "/":
                        continue
                    obj = src[gp]
                    new_group = dst.require_group(gp)
                    _copy_attr_set(obj, new_group, gp, skipped_attrs)
                # Copy only selected datasets while preserving their exact HDF5 paths.
                for rec in records:
                    p = rec["path"]
                    if p not in chosen:
                        continue
                    src_ds = src[p]
                    parent = str(Path(p).parent).replace("\\", "/")
                    if parent == ".":
                        parent = "/"
                    dst_group = dst.require_group(parent)
                    dst_ds = _copy_dataset_losslessly(src_ds, dst_group, Path(p).name, skipped_attrs, rec)
                    # Hash the exact logical source values through a streaming read.
                    src_hash = dataset_digest(src_ds)
                    selected_records.append({**rec, "retention_status": "retained", "reason": chosen[p],
                                             "logical_data_sha256": src_hash,
                                             "archive_dtype": str(dst_ds.dtype),
                                             "archive_shape": list(dst_ds.shape)})
                # Recreate actual dimension scales before attaching them. Copying
                # CLASS/NAME as ordinary attributes alone is not sufficient for all
                # valid source files, especially empty-name scales used in NISAR
                # wrappedInterferogram grids.
                for rec in selected_records:
                    p = rec["path"]
                    src_ds, dst_ds = src[p], dst[p]
                    if _is_dimension_scale(src_ds):
                        try:
                            dst_ds.make_scale(_dimension_scale_name(src_ds))
                        except Exception as exc:
                            raise ValueError(
                                f"Could not recreate HDF5 dimension scale {p}; "
                                "refusing an archive with altered coordinate semantics"
                            ) from exc

                # Derive wrapped phase from each excluded complex GUNW raster in
                # bounded-memory row-major slabs. The compact archive keeps the
                # phase (radians) and coherence/quality arrays; --include-wrapped
                # still controls retention of the exact original complex samples.
                derived_records = []
                # Derive wrappedPhase in both compact and full-retention modes.
                # The full-retention option keeps original complex samples too.
                for rec in (records if derive_wrapped_phase else []):
                    p = rec["path"]
                    if not (
                        family == "GUNW"
                        and rec["ndim"] >= 2
                        and rec["dtype"].startswith("complex")
                        and Path(p).name == "wrappedInterferogram"
                    ):
                        continue
                    src_ds = src[p]
                    out_path = str(Path(p).with_name("wrappedPhase")).replace("\\", "/")
                    if out_path in dst:
                        raise ValueError(
                            f"Cannot derive wrapped phase because output path already exists: {out_path}"
                        )
                    if src_ds.dtype.itemsize == 8:
                        phase_dtype = np.dtype("float32")
                    elif src_ds.dtype.itemsize == 16:
                        phase_dtype = np.dtype("float64")
                    else:
                        raise ValueError(
                            f"Unsupported complex dtype {src_ds.dtype} for wrapped-phase derivation at {p}"
                        )
                    kwargs: dict[str, Any] = {
                        "maxshape": src_ds.maxshape,
                        "fillvalue": phase_dtype.type(np.nan),
                    }
                    if src_ds.shape != () and src_ds.size > 0:
                        kwargs["chunks"] = _choose_chunk_shape(src_ds.shape, phase_dtype)
                        kwargs["compression"] = "gzip"
                        kwargs["compression_opts"] = 4
                        kwargs["shuffle"] = True
                    parent = str(Path(out_path).parent).replace("\\", "/")
                    phase_ds = dst.require_group(parent).create_dataset(
                        Path(out_path).name, shape=src_ds.shape, dtype=phase_dtype, **kwargs
                    )
                    source_hash = hashlib.sha256()
                    for slc in _slices_for_dataset(src_ds):
                        values = src_ds[slc]
                        source_hash.update(_logical_chunk_bytes(values, src_ds.dtype))
                        phase_ds[slc] = np.angle(values).astype(phase_dtype, copy=False)
                    # Preserve useful coordinate/grid metadata, but explicitly
                    # transform fill value and units for a real-valued phase layer.
                    for key in src_ds.attrs.keys():
                        if key in {"DIMENSION_LIST", "REFERENCE_LIST", "_FillValue", "units", "description"}:
                            continue
                        val = src_ds.attrs[key]
                        if isinstance(val, h5py.Reference) or isinstance(val, h5py.RegionReference) or _is_reference_dtype(np.asarray(val).dtype):
                            raise ValueError(
                                f"Cannot derive wrapped phase while source has reference attribute {p}@{key}"
                            )
                        try:
                            phase_ds.attrs.create(key, val, dtype=src_ds.attrs.get_id(key).dtype)
                        except Exception:
                            phase_ds.attrs[key] = val
                    phase_ds.attrs["_FillValue"] = phase_dtype.type(np.nan)
                    phase_ds.attrs["units"] = "radians"
                    phase_ds.attrs["description"] = (
                        f"Wrapped phase angle in radians derived from {p} using numpy.angle; "
                        "complex amplitude is not represented by this derived dataset."
                    )
                    phase_ds.attrs["long_name"] = "wrapped interferometric phase"
                    phase_ds.attrs["phase_derivation"] = "numpy.angle(complex samples), in radians"
                    derived_records.append({
                        "path": out_path,
                        "source_path": p,
                        "transform": "numpy.angle(complex samples) -> radians; no spatial unwrapping performed",
                        "shape": list(phase_ds.shape),
                        "dtype": str(phase_ds.dtype),
                        "units": "radians",
                        "source_logical_data_sha256": source_hash.hexdigest(),
                        "logical_data_sha256": dataset_digest(phase_ds),
                        "ordinary_attrs": _ordinary_attrs_json(phase_ds),
                        "ordinary_attrs_sha256": hashlib.sha256(
                            _canonical_json(_ordinary_attrs_json(phase_ds))
                        ).hexdigest(),
                    })

                # Rebuild original dimension labels and links for all retained
                # arrays. Links to omitted complex source rasters are not copied.
                for rec in selected_records:
                    p = rec["path"]
                    src_ds, dst_ds = src[p], dst[p]
                    for axis in range(src_ds.ndim):
                        try:
                            if src_ds.dims[axis].label:
                                dst_ds.dims[axis].label = src_ds.dims[axis].label
                            for _scale_name, src_scale in src_ds.dims[axis].items():
                                if src_scale.name in dst:
                                    dst_ds.dims[axis].attach_scale(dst[src_scale.name])
                        except Exception as exc:
                            raise ValueError(
                                f"Could not rebuild HDF5 dimension-scale attachment for {p} axis {axis}; "
                                "refusing an archive with altered coordinate semantics"
                            ) from exc
                # The derived wrappedPhase array takes the same source coordinate
                # scales as the complex wrappedInterferogram it represents.
                for item in derived_records:
                    source_phase = src[item["source_path"]]
                    output_phase = dst[item["path"]]
                    for axis in range(source_phase.ndim):
                        try:
                            if source_phase.dims[axis].label:
                                output_phase.dims[axis].label = source_phase.dims[axis].label
                            for _name, source_scale in source_phase.dims[axis].items():
                                if source_scale.name in dst:
                                    output_phase.dims[axis].attach_scale(dst[source_scale.name])
                        except Exception as exc:
                            raise ValueError(
                                f"Could not attach source coordinate scales to derived dataset {item['path']} "
                                f"axis {axis}"
                            ) from exc
                    item["ordinary_attrs"] = _ordinary_attrs_json(output_phase)
                    item["ordinary_attrs_sha256"] = hashlib.sha256(
                        _canonical_json(item["ordinary_attrs"])
                    ).hexdigest()
                    item["dimension_links"] = _dimension_links(output_phase)
                dst.flush()
            # Ensure output closed and flush complete before verification.
            # Compare every retained dataset's logical data and ordinary attrs.
            verification_errors: list[str] = []
            with h5py.File(source, "r") as src_verify, h5py.File(tmp_path, "r") as archive_verify:
                src_root_attrs = _ordinary_attrs_json(src_verify)
                arc_root_attrs = _ordinary_attrs_json(archive_verify)
                if _canonical_json(src_root_attrs) != _canonical_json(arc_root_attrs):
                    verification_errors.append("root attributes differ")
                for item in selected_records:
                    p = item["path"]
                    if p not in archive_verify:
                        verification_errors.append(f"missing retained dataset {p}")
                        continue
                    source_ds, archive_ds = src_verify[p], archive_verify[p]
                    if source_ds.shape != archive_ds.shape or source_ds.dtype != archive_ds.dtype:
                        verification_errors.append(f"shape/dtype mismatch: {p}")
                        continue
                    if source_ds.maxshape != archive_ds.maxshape:
                        verification_errors.append(f"maxshape mismatch: {p}")
                    if not _value_equal(np.asarray(source_ds.fillvalue), np.asarray(archive_ds.fillvalue)):
                        verification_errors.append(f"fillvalue mismatch: {p}")
                    if source_ds.chunks != archive_ds.chunks:
                        verification_errors.append(f"chunk layout mismatch: {p}")
                    if source_ds.compression != archive_ds.compression:
                        verification_errors.append(f"compression filter mismatch: {p}")
                    if source_ds.compression_opts != archive_ds.compression_opts:
                        verification_errors.append(f"compression options mismatch: {p}")
                    if source_ds.shuffle != archive_ds.shuffle or source_ds.fletcher32 != archive_ds.fletcher32:
                        verification_errors.append(f"dataset filter flags mismatch: {p}")
                    if source_ds.scaleoffset != archive_ds.scaleoffset:
                        verification_errors.append(f"scale-offset filter mismatch: {p}")
                    # SHA of source values was recorded while extracting; now
                    # compare to the archived dataset in bounded memory.
                    if dataset_digest(archive_ds) != item["logical_data_sha256"]:
                        verification_errors.append(f"logical dataset checksum mismatch: {p}")
                    if _canonical_json(_ordinary_attrs_json(source_ds)) != _canonical_json(_ordinary_attrs_json(archive_ds)):
                        verification_errors.append(f"dataset attrs mismatch: {p}")
                    if _is_dimension_scale(source_ds) != _is_dimension_scale(archive_ds):
                        verification_errors.append(f"dimension-scale status mismatch: {p}")
                    if _is_dimension_scale(source_ds) and _dimension_scale_name(source_ds) != _dimension_scale_name(archive_ds):
                        verification_errors.append(f"dimension-scale name mismatch: {p}")
                    if _dimension_links(source_ds) != _dimension_links(archive_ds):
                        verification_errors.append(f"dimension-scale links mismatch: {p}")
                for gp in group_paths:
                    if gp == "/":
                        continue
                    if gp not in archive_verify:
                        verification_errors.append(f"missing group {gp}")
                        continue
                    source_attrs = _ordinary_attrs_json(src_verify[gp])
                    archive_attrs = _ordinary_attrs_json(archive_verify[gp])
                    if _canonical_json(source_attrs) != _canonical_json(archive_attrs):
                        verification_errors.append(f"group attrs mismatch: {gp}")
                for item in derived_records:
                    p = item["path"]
                    source_path = item["source_path"]
                    if p not in archive_verify or source_path not in src_verify:
                        verification_errors.append(f"derived wrapped-phase dataset missing: {p} (source {source_path})")
                        continue
                    phase_ds = archive_verify[p]
                    complex_ds = src_verify[source_path]
                    if list(phase_ds.shape) != item["shape"] or str(phase_ds.dtype) != item["dtype"]:
                        verification_errors.append(f"derived wrapped-phase shape/dtype mismatch: {p}")
                        continue
                    if dataset_digest(phase_ds) != item["logical_data_sha256"]:
                        verification_errors.append(f"derived wrapped-phase checksum mismatch: {p}")
                    source_hash = hashlib.sha256()
                    for slc in _slices_for_dataset(complex_ds):
                        complex_values = complex_ds[slc]
                        source_hash.update(_logical_chunk_bytes(complex_values, complex_ds.dtype))
                        expected_phase = np.angle(complex_values).astype(phase_ds.dtype, copy=False)
                        if not _value_equal(phase_ds[slc], expected_phase):
                            verification_errors.append(f"derived wrapped-phase values mismatch: {p}")
                            break
                    if source_hash.hexdigest() != item["source_logical_data_sha256"]:
                        verification_errors.append(f"derived wrapped-phase source checksum mismatch: {source_path}")
                    if _canonical_json(_ordinary_attrs_json(phase_ds)) != _canonical_json(item["ordinary_attrs"]):
                        verification_errors.append(f"derived wrapped-phase metadata mismatch: {p}")
                    if _dimension_links(complex_ds) != _dimension_links(phase_ds):
                        verification_errors.append(f"derived wrapped-phase coordinates mismatch: {p}")
            if verification_errors:
                raise RuntimeError("Archive verification failed: " + "; ".join(verification_errors[:20]))
            os.replace(tmp_path, archive)
        except Exception:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
            # Incomplete/failed archive must not be mistaken for a valid product.
            archive.unlink(missing_ok=True)
            raise

    source_stat = source.stat()
    archive_stat = archive.stat()
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "policy": {
            "name": f"sarguardian_{retention_profile}_v4",
            "product_type": family,
            "retention_profile": retention_profile,
            "include_wrapped_interferogram": bool(family == "GUNW" and (retention_profile == "full-lossless" or include_wrapped)),
            "derive_wrapped_phase": derive_wrapped_phase,
            "deletion_performed": False,
            "deletion_policy": "Never delete here. Only the runner/orchestrator may delete the temporary raw source after remote durable-storage upload and remote size+checksum verification.",
            "preservation_contract": [
                "engine-ready retains the pinned GUNW/GOFF reader inputs and geometry_merge LOS dependencies, full native grids, every scalar/1-D metadata dataset, exact ordinary attributes, and all group paths.",
                "full-lossless retains every source dataset. It is the profile for unrestricted future reprocessing; engine-ready exclusions are deliberate and itemized in the manifest, not presented as a complete original product.",
                "All GUNW pixelOffsets grids are kept for future same-granule GOFF cross-checks. All GOFF pixelOffsets layers/polarizations, including quality and variance arrays, are kept in full.",
                "HDF5 DIMENSION_LIST/REFERENCE_LIST bookkeeping is rebuilt as true destination dimension-scale links; scale labels, links, values, dtypes, shape, maxshape, fillvalue, chunking and filters are verified.",
                "Native HDF5 object copy preserves stored dataset data/chunks and source creation filters; no numeric scaling, quantisation, interpolation, AOI crop, or lossy conversion is applied to retained source arrays.",
                "Optional --derive-wrapped-phase computes a same-grid phase-only derivative in radians. This does not preserve complex amplitude and does not spatially unwrap the phase. Original wrapped-grid datasets require --include-wrapped or --retention-profile full-lossless.",
                "HDF5 source user blocks, virtual datasets, external raw-storage datasets, noncanonical links/aliases and unsupported reference attributes/datasets fail closed.",
            ],
            "known_exclusions": [
                "engine-ready excludes multidimensional GUNW wrappedInterferogram-grid arrays because no pinned main-engine path reads them. Their full hierarchy/schema/attributes remain documented in source-inventory.json and the retention manifest.",
                "engine-ready excludes multidimensional metadata/radarGrid fields other than losUnitVectorX, losUnitVectorY and incidenceAngle; geometry_merge.los_from_product reads those three plus coordinates/projection.",
                "engine-ready excludes other multidimensional auxiliary fields outside unwrappedInterferogram/pixelOffsets and the documented LOS geometry dependencies. Use full-lossless to retain every such field.",
                "Set --include-wrapped to retain the full GUNW wrappedInterferogram group, including complex samples, coherence/mask arrays and coordinate vectors. Use --derive-wrapped-phase only if the phase-only output is specifically desired.",
            ],
        },
        "source": {"filename": source.name, "bytes": int(source_stat.st_size), "sha256": sha256_file(source), "hdf5_root_attrs": root_meta["attrs"], "hdf5_root_attrs_sha256": root_meta["attrs_sha256"]},
        "source_group_attributes": group_attribute_records,
        "noncanonical_links": noncanonical_links,
        "archiver": {"name": Path(__file__).name, "schema_version": SCHEMA_VERSION, "script_sha256": sha256_file(Path(__file__).resolve())},
        "archive": {"filename": archive.name, "bytes": int(archive_stat.st_size), "sha256": sha256_file(archive), "copy_method": "HDF5 native object copy (H5Ocopy), preserving source chunks and compression filters"},
        "statistics": {
            "source_dataset_count": len(records),
            "retained_dataset_count": len(selected_records),
            "excluded_large_dataset_count": len(excluded),
            "source_bytes": int(source_stat.st_size),
            "archive_bytes": int(archive_stat.st_size),
            "ratio_archive_to_source": round(archive_stat.st_size / max(source_stat.st_size, 1), 6),
            "retained_logical_bytes_estimate": sum((x.get("logical_nbytes_estimate") or 0) for x in selected_records),
            "excluded_logical_bytes_estimate": sum((x.get("logical_nbytes_estimate") or 0) for x in excluded),
            "derived_dataset_count": len(derived_records),
            "derived_logical_bytes_estimate": sum(int(np.prod(x.get("shape", []), dtype=np.int64)) * np.dtype(x["dtype"]).itemsize for x in derived_records),
        },
        "verification": {"local_archive_verification": "PASS", "retained_data_logical_checksums": "PASS", "derived_phase_verification": "PASS", "dimension_scale_verification": "PASS", "raw_source_deleted": False},
        "retained_datasets": selected_records,
        "excluded_datasets": excluded,
        "derived_datasets": derived_records,
        "skipped_hdf5_reference_attributes": skipped_attrs,
    }
    # Keep manifest useful but bounded. All dataset records include attr inventory.
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return manifest


def verify_archive(source: Path, archive: Path, manifest_path: Path) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    if sha256_file(source) != manifest["source"]["sha256"]:
        errors.append("source file SHA-256 mismatch against manifest")
    if sha256_file(archive) != manifest["archive"]["sha256"]:
        errors.append("archive file SHA-256 mismatch against manifest")
    with h5py.File(source, "r") as src, h5py.File(archive, "r") as arc:
        for item in manifest["retained_datasets"]:
            path = item["path"]
            if path not in src or path not in arc:
                errors.append(f"retained dataset missing in source/archive: {path}")
                continue
            if dataset_digest(src[path]) != item["logical_data_sha256"]:
                errors.append(f"source logical dataset checksum changed: {path}")
            if dataset_digest(arc[path]) != item["logical_data_sha256"]:
                errors.append(f"archive logical dataset checksum mismatch: {path}")
            if src[path].shape != arc[path].shape or src[path].dtype != arc[path].dtype:
                errors.append(f"source/archive shape or dtype changed: {path}")
            if _canonical_json(_ordinary_attrs_json(src[path])) != _canonical_json(_ordinary_attrs_json(arc[path])):
                errors.append(f"source/archive attrs mismatch: {path}")
            if _is_dimension_scale(src[path]) != _is_dimension_scale(arc[path]):
                errors.append(f"source/archive dimension-scale status mismatch: {path}")
            if _is_dimension_scale(src[path]) and _dimension_scale_name(src[path]) != _dimension_scale_name(arc[path]):
                errors.append(f"source/archive dimension-scale name mismatch: {path}")
            if _dimension_links(src[path]) != _dimension_links(arc[path]):
                errors.append(f"source/archive dimension-scale links mismatch: {path}")
        for item in manifest.get("derived_datasets", []):
            path = item["path"]
            source_path = item["source_path"]
            if path not in arc or source_path not in src:
                errors.append(f"derived dataset/source missing: {path} / {source_path}")
                continue
            phase_ds, complex_ds = arc[path], src[source_path]
            if list(phase_ds.shape) != item["shape"] or str(phase_ds.dtype) != item["dtype"]:
                errors.append(f"derived dataset shape/dtype mismatch: {path}")
                continue
            if dataset_digest(phase_ds) != item["logical_data_sha256"]:
                errors.append(f"derived dataset checksum mismatch: {path}")
            source_hash = hashlib.sha256()
            values_mismatch = False
            for slc in _slices_for_dataset(complex_ds):
                complex_values = complex_ds[slc]
                source_hash.update(_logical_chunk_bytes(complex_values, complex_ds.dtype))
                expected_phase = np.angle(complex_values).astype(phase_ds.dtype, copy=False)
                if not _value_equal(phase_ds[slc], expected_phase):
                    values_mismatch = True
                    break
            if values_mismatch:
                errors.append(f"derived dataset values mismatch: {path}")
            if source_hash.hexdigest() != item["source_logical_data_sha256"]:
                errors.append(f"derived source checksum mismatch: {source_path}")
            if _canonical_json(_ordinary_attrs_json(phase_ds)) != _canonical_json(item["ordinary_attrs"]):
                errors.append(f"derived dataset attributes mismatch: {path}")
            if _dimension_links(complex_ds) != _dimension_links(phase_ds):
                errors.append(f"derived dataset dimension-scale mismatch: {path}")
        for item in manifest.get("excluded_datasets", []):
            path = item["path"]
            if path not in src:
                errors.append(f"excluded source dataset is unexpectedly missing: {path}")
            if path in arc:
                errors.append(f"excluded dataset unexpectedly copied: {path}")
    if errors:
        raise RuntimeError("verification failed:\n- " + "\n- ".join(errors[:100]))
    return {"status": "PASS", "retained_dataset_count": len(manifest["retained_datasets"]), "excluded_dataset_count": len(manifest["excluded_datasets"]), "source_sha256": manifest["source"]["sha256"], "archive_sha256": manifest["archive"]["sha256"]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    inv = sub.add_parser("inventory", help="inventory every HDF5 dataset/attribute; no archive created")
    inv.add_argument("--input", required=True, type=Path)
    inv.add_argument("--out", required=True, type=Path)
    inv.add_argument("--product-type", choices=["auto", "GUNW", "GOFF"], default="auto")
    build = sub.add_parser("archive", help="build curated lossless HDF5 subset + manifest; never deletes source")
    build.add_argument("--input", required=True, type=Path)
    build.add_argument("--out", required=True, type=Path)
    build.add_argument("--manifest", required=True, type=Path)
    build.add_argument("--product-type", choices=["auto", "GUNW", "GOFF"], default="auto")
    build.add_argument("--retention-profile", choices=["engine-ready", "full-lossless"], default="engine-ready", help="engine-ready keeps current engine dependencies; full-lossless retains every source dataset")
    build.add_argument("--include-wrapped", action="store_true", help="engine-ready only: preserve the entire high-posting GUNW wrappedInterferogram grid family")
    build.add_argument("--derive-wrapped-phase", action="store_true", help="derive and verify a phase-only wrappedPhase raster in radians; not spatial unwrapping")
    verify = sub.add_parser("verify", help="verify local source/archive against manifest; no delete")
    verify.add_argument("--input", required=True, type=Path)
    verify.add_argument("--archive", required=True, type=Path)
    verify.add_argument("--manifest", required=True, type=Path)
    args = ap.parse_args()

    if args.command == "inventory":
        with h5py.File(args.input, "r") as f:
            family = infer_product_type(f, args.input.name) if args.product_type == "auto" else args.product_type
            datasets, groups, root = discover_inventory(f)
            group_attributes = [{"path": gp, "attrs": attrs_json(f[gp]), "attrs_sha256": attrs_digest(f[gp])} for gp in groups if gp != "/"]
            noncanonical_links = discover_noncanonical_links(f)
        obj = {"schema_version": SCHEMA_VERSION, "created_utc": datetime.now(timezone.utc).isoformat(), "product_type": family, "source": {"filename": args.input.name, "bytes": args.input.stat().st_size, "sha256": sha256_file(args.input)}, "root_attrs": root, "groups": groups, "group_attributes": group_attributes, "noncanonical_links": noncanonical_links, "datasets": datasets}
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"INVENTORY_WRITTEN={args.out} DATASETS={len(datasets)} GROUPS={len(groups)}")
        return 0
    if args.command == "archive":
        manifest = build_archive(args.input, args.out, args.manifest, args.product_type, args.include_wrapped,
                                 args.retention_profile, args.derive_wrapped_phase)
        print("ARCHIVE_VERIFICATION=PASS")
        print(f"PRODUCT_TYPE={manifest['policy']['product_type']}")
        print(f"SOURCE_BYTES={manifest['source']['bytes']}")
        print(f"ARCHIVE_BYTES={manifest['archive']['bytes']}")
        print(f"RETAINED_DATASETS={manifest['statistics']['retained_dataset_count']}")
        print(f"EXCLUDED_LARGE_DATASETS={manifest['statistics']['excluded_large_dataset_count']}")
        print(f"DERIVED_PHASE_DATASETS={manifest['statistics']['derived_dataset_count']}")
        print(f"RETENTION_PROFILE={manifest['policy']['retention_profile']}")
        print(f"SOURCE_SHA256={manifest['source']['sha256']}")
        print(f"ARCHIVE_SHA256={manifest['archive']['sha256']}")
        print("RAW_SOURCE_DELETED=no")
        return 0
    result = verify_archive(args.input, args.archive, args.manifest)
    print(json.dumps(result, indent=2))
    return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2)
