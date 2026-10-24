"""Run the supported per-product SARGuardian displacement screening workflow.

This adapter intentionally stops at AOI-clipped GUNW/GOFF outputs.  It does
not assemble an arbitrary-AOI time series or make a hazard/forecast claim:
the pinned science source does not support that for ``custom`` AOIs.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable
from uuid import UUID

from . import catalog
from .geometry import processing_fingerprint, validate_wkt
from .validation import (ValidationError, create_raster_preview, inventory_hdf5,
                         enforce_retention_aoi_mask, retain_quality_raster, sha256_file as validated_sha256,
                         validate_raster, write_raster_summary)

SCIENCE_REPOSITORY = "moshiour0/SARGuardian"
SCIENCE_REF = "10639a916c9829850775410b3ed7de62e527770d"
PRODUCT_TYPES = frozenset({"GUNW", "GOFF"})
MAX_DATE_WINDOW_DAYS = 90

logger = logging.getLogger("sarguardian.dynamic-displacement")


class AdapterError(RuntimeError):
    """A safe failure suitable for the worker callback."""


@dataclass(frozen=True)
class JobInput:
    job_id: str
    aoi_wkt: str
    start_date: date
    end_date: date
    science_root: Path
    work_dir: Path
    max_download_gib: float
    product_type: str = "BOTH"
    acquisition_window: str = "secondary-acquisition-in-window"
    retention_mode: str = "derived-only"
    processing_profile: str = "validation-pilot"
    stream_upload: bool = False
    report_progress: bool = False


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_stage(work_dir: Path, stage: str, **details: Any) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    event = {"stage": stage, **details}
    with (work_dir / "stage-events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, default=str, sort_keys=True) + "\n")
    logger.info("stage=%s %s", stage, json.dumps(details, default=str, sort_keys=True))


def select_eligible_products(records: Iterable[dict[str, Any]], start_date: date, end_date: date) -> list[dict[str, Any]]:
    """Select NISAR pairs by their secondary-acquisition date.

    This is the documented policy used by the dynamic-AOI reference workflow:
    a pair represents change up to its secondary acquisition, so an in-window
    secondary is eligible even when its reference falls immediately before the
    requested start date.  The returned manifest records that policy so the
    date semantics are never implicit.
    """
    if end_date < start_date:
        raise AdapterError("End date must be on or after start date.")
    selected = [
        record for record in records
        if record.get("productType") in PRODUCT_TYPES
        and isinstance(record.get("reference"), date)
        and isinstance(record.get("secondary"), date)
        and record["reference"] <= record["secondary"]
        and start_date <= record["secondary"] <= end_date
        and isinstance(record.get("url"), str) and record["url"]
    ]
    selected.sort(key=lambda item: (item["secondary"], item["reference"], item["productType"], item["granule"]))
    return selected


def enforce_download_budget(records: Iterable[dict[str, Any]], max_download_gib: float) -> float:
    if max_download_gib <= 0:
        raise AdapterError("Maximum download budget must be positive.")
    sizes_mb: list[float] = []
    for record in records:
        size_mb = record.get("size_mb")
        if not isinstance(size_mb, (int, float)) or size_mb <= 0:
            raise AdapterError("A selected product has no validated download size.")
        sizes_mb.append(float(size_mb))
    total_gib = sum(sizes_mb) / 1024
    if total_gib > max_download_gib:
        raise AdapterError(f"Selected products require {total_gib:.2f} GiB, exceeding the configured {max_download_gib:.2f} GiB worker budget.")
    return total_gib


def resolve_download_sizes(records: Iterable[dict[str, Any]], acquisition: Any) -> list[dict[str, Any]]:
    """Resolve missing catalogue sizes before enforcing the hard download limit.

    Current ASF NISAR catalogue responses may omit ``sizeMB``.  Use the pinned
    source's authenticated ASF session to make a HEAD-only request to the final
    data URL, then require a positive Content-Length.  A missing or invalid size
    remains a safe failure; it can never bypass the configured download budget.
    """
    session: Any | None = None
    resolved: list[dict[str, Any]] = []
    for record in records:
        size_mb = record.get("size_mb")
        if isinstance(size_mb, (int, float)) and size_mb > 0:
            resolved.append(record)
            continue
        if session is None:
            try:
                session = acquisition.build_session()
            except AdapterError:
                raise
            except Exception as exc:
                raise AdapterError("Could not initialize the authenticated ASF session for size validation.") from exc
        response: Any | None = None
        try:
            response = session.head(str(record["url"]), allow_redirects=True, timeout=60)
            response.raise_for_status()
            content_length = response.headers.get("Content-Length")
            byte_count = int(content_length) if isinstance(content_length, str) and content_length.isdigit() else 0
        except Exception as exc:
            raise AdapterError("Could not validate the download size for a selected ASF product.") from exc
        finally:
            if response is not None:
                response.close()
        if byte_count <= 0:
            raise AdapterError("ASF did not provide a valid Content-Length for a selected product.")
        resolved.append(record | {"size_mb": byte_count / (1024 * 1024)})
    return resolved


def _install_earthdata_token_session(acquisition: Any) -> None:
    """Build a cookie-free authenticated session for NISAR Level-2 products.

    ``asf_search.auth_with_creds`` additionally requests an ``asf-urs`` cookie,
    which the upstream client documents as necessary for certain products such
    as SLC bursts, not these NISAR Level-2 products.  Prefer a normal EDL
    username/password exchange to obtain the same bearer token without that
    unrelated cookie step.  An explicit EDL token remains a fallback only when
    credentials are not configured.
    """
    username = os.getenv("EARTHDATA_USERNAME", "").strip()
    password = os.getenv("EARTHDATA_PASSWORD", "")
    token = os.getenv("EARTHDATA_TOKEN", "").strip()
    if not ((username and password) or token):
        return
    try:
        import asf_search as asf
        import requests
        from requests.auth import HTTPBasicAuth
    except ImportError as exc:  # pragma: no cover - workflow installs this dependency
        raise AdapterError("The pinned ASF client is unavailable for Earthdata token authentication.") from exc

    cached_session: Any | None = None

    def bound_network_session(session: Any) -> Any:
        # The upstream downloader omits a timeout on its streaming GET.  A
        # read timeout is an idle-socket limit, so healthy multi-GB transfers
        # continue while a dead connection is bounded.
        source_request = getattr(session, "request", None)
        if callable(source_request):
            def request(method: str, url: str, **kwargs: Any) -> Any:
                kwargs.setdefault("timeout", (10, 120))
                return source_request(method, url, **kwargs)
            session.request = request
        return session

    def build_session() -> Any:
        nonlocal cached_session
        if cached_session is not None:
            return cached_session
        if username and password:
            session = asf.ASFSession()
            response: Any | None = None
            try:
                # asf-search's private token helper does not set a request
                # timeout.  Call the same EDL endpoint explicitly so an
                # unavailable auth service cannot stall every retry for
                # minutes.
                response = session.post(
                    f"https://{session.edl_host}/api/users/find_or_create_token",
                    auth=HTTPBasicAuth(username, password), timeout=(10, 20),
                )
                response.raise_for_status()
                token_payload = response.json()
                access_token = token_payload.get("access_token") if isinstance(token_payload, dict) else None
                if not isinstance(access_token, str) or not access_token:
                    raise AdapterError("Earthdata authentication returned no access token.")
            except requests.Timeout as exc:
                raise AdapterError("Earthdata authentication timed out after 20 seconds.") from exc
            except requests.HTTPError as exc:
                raise AdapterError("Earthdata username/password authentication was rejected.") from exc
            except requests.RequestException as exc:
                raise AdapterError("Earthdata authentication service is unavailable.") from exc
            except ValueError as exc:
                raise AdapterError("Earthdata authentication returned an invalid response.") from exc
            finally:
                if response is not None:
                    response.close()
            session._update_edl_token(access_token)
            cached_session = bound_network_session(session)
            return cached_session
        try:
            cached_session = bound_network_session(asf.ASFSession().auth_with_token(token))
            return cached_session
        except Exception as exc:
            # The upstream client uses this exact safe message for a bad EDL
            # token.  Do not include exception data that could contain a token.
            if "Invalid/Expired token passed" in str(exc):
                raise AdapterError("Earthdata token was rejected or has expired.") from exc
            raise AdapterError("Earthdata token authentication failed; authorize the ASF application and generate a new token.") from exc

    acquisition.build_session = build_session


def _install_asf_response_compatibility(acquisition: Any) -> None:
    """Adapt the pinned source's documented dict-or-list ASF JSON contract.

    The pinned source annotates ``_get`` as returning ``dict | list``, but its
    ``asf_query`` immediately calls ``.get``.  ASF currently returns batches as
    a top-level list of product lists for this query.  Normalizing only that
    response to the source's existing ``{"results": [...]}`` shape preserves
    every catalogue record and lets the original acquisition, filtering, and
    reader code remain authoritative.
    """
    source_get = acquisition._get

    def normalized_rows(rows: list[Any]) -> list[dict[str, Any]]:
        if not all(isinstance(row, dict) for row in rows):
            raise AdapterError("ASF catalogue returned a list containing an invalid product record.")
        normalized: list[dict[str, Any]] = []
        for row in rows:
            normalized_row = dict(row)
            product = row.get("productType") or row.get("processingLevel")
            path = row.get("path")
            if isinstance(product, str):
                normalized_row["productType"] = product.upper()
            if path is None:
                path = row.get("relativeOrbit", row.get("track"))
            if path is not None:
                normalized_row["path"] = path
            normalized.append(normalized_row)
        return normalized

    def compatible_get(*args: Any, **kwargs: Any) -> dict[str, Any]:
        response = source_get(*args, **kwargs)
        if isinstance(response, list):
            if all(isinstance(row, dict) for row in response):
                return {"results": normalized_rows(response)}
            if all(isinstance(batch, list) for batch in response):
                rows = [row for batch in response for row in batch]
                return {"results": normalized_rows(rows)}
            raise AdapterError("ASF catalogue returned a list containing an invalid product record.")
        if not isinstance(response, dict):
            raise AdapterError("ASF catalogue returned an unsupported JSON response type.")
        results = response.get("results")
        if results is not None and (
            not isinstance(results, list) or not all(isinstance(row, dict) for row in results)
        ):
            raise AdapterError("ASF catalogue returned an invalid results payload.")
        return response | ({"results": normalized_rows(results)} if isinstance(results, list) else {})

    acquisition._get = compatible_get


def _load_science_modules(science_root: Path, aoi_wkt: str) -> tuple[Any, Any, Any]:
    source_dir = science_root / "src"
    if not (source_dir / "aoi_wkt.py").is_file():
        raise AdapterError("Pinned science source does not contain the required WKT parser.")
    os.environ["SARGUARDIAN_AOI_WKT"] = aoi_wkt
    sys.path.insert(0, str(source_dir))
    for name in ("aoi_wkt", "gunw_reader", "goff_reader", "nisar_acquisition"):
        sys.modules.pop(name, None)
    aoi_wkt_module = importlib.import_module("aoi_wkt")
    try:
        aoi_wkt_module.parse_polygon_wkt(aoi_wkt)
    except ValueError as exc:
        raise AdapterError(f"Science source rejected AOI WKT: {exc}") from None
    gunw = importlib.import_module("gunw_reader")
    goff = importlib.import_module("goff_reader")
    acquisition = importlib.import_module("nisar_acquisition")
    _install_asf_response_compatibility(acquisition)
    _install_earthdata_token_session(acquisition)
    if "custom" not in gunw.AOIS:
        raise AdapterError("Pinned science source did not install the submitted custom AOI.")
    acquisition.set_aoi("custom")
    gunw.set_aoi("custom")
    return acquisition, gunw, goff


def _raw_path(science_root: Path, record: dict[str, Any]) -> Path:
    filename = Path(str(record["url"]).split("?", 1)[0]).name
    candidate = (science_root / "data" / "nisar_l2" / "_incoming" / filename).resolve()
    incoming = (science_root / "data" / "nisar_l2" / "_incoming").resolve()
    if incoming not in candidate.parents or not candidate.name.startswith("NISAR_"):
        raise AdapterError("Source data path failed the safe cleanup check.")
    return candidate


def _validated_output(path: Path, kind: str, product: str) -> dict[str, Any]:
    return {"path": str(path).replace("\\", "/"), "kind": kind, "product_type": product, "sha256": sha256_file(path), "bytes": path.stat().st_size}


def _artifact(work_dir: Path, path: Path, kind: str, product: str | None = None) -> dict[str, Any]:
    path = Path(path).resolve()
    root = work_dir.resolve()
    if root not in path.parents or not path.is_file():
        raise AdapterError("Generated scientific artifact is outside the isolated job workspace.")
    result = {"path": path.relative_to(root).as_posix(), "kind": kind,
              "sha256": sha256_file(path), "bytes": path.stat().st_size}
    if product:
        result["product_type"] = product
    return result


def _download_with_retry(acquisition: Any, record: dict[str, Any], raw: Path, work_dir: Path) -> None:
    # ``nisar_acquisition.download`` logs these fields before it transfers a
    # byte.  Check its public record contract up front: retrying a malformed
    # record only hides an integration defect and burns runner time.
    required = ("productType", "granule", "url", "direction", "path", "reference", "secondary")
    missing = [key for key in required if record.get(key) in (None, "")]
    if missing:
        raise AdapterError(f"Selected catalog record is missing downloader field(s): {', '.join(missing)}.")
    max_attempts = 2
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            if raw.exists():
                raw.unlink()
            acquisition.download([record["productType"]], [record])
            if raw.is_file() and raw.stat().st_size > 0:
                return
        except AdapterError:
            # Authentication/configuration failures cannot improve by
            # repeating the exact request, so fail immediately.
            raise
        except Exception as exc:
            last_error = exc
        if attempt < max_attempts:
            write_stage(work_dir, "download_retry", granule=record["granule"], attempt=attempt,
                        next_attempt=attempt + 1, error_type=type(last_error).__name__ if last_error else "empty_download")
    if last_error is not None:
        raise AdapterError("Download failed after two bounded attempts.") from last_error
    raise AdapterError("Downloaded file was missing or empty after two bounded attempts.")


def _discovery_progress_summary(discovery: dict[str, Any]) -> dict[str, Any]:
    eligible = discovery.get("eligible_product_counts")
    selected = discovery.get("selected_product_counts")
    if not isinstance(eligible, dict) or not isinstance(selected, dict):
        raise AdapterError("Product discovery did not provide per-family counts.")
    return {
        "schema_version": 2,
        "catalog_granule_counts": {family: int(eligible.get(family, 0)) for family in sorted(PRODUCT_TYPES)},
        "selected_granule_counts": {family: int(selected.get(family, 0)) for family in sorted(PRODUCT_TYPES)},
    }


def _report_discovery_progress(job: JobInput, discovery: dict[str, Any]) -> None:
    if not job.report_progress:
        return
    callback_url = os.getenv("CALLBACK_URL", "").rstrip("/")
    callback_secret = os.getenv("CALLBACK_SECRET", "")
    request_fingerprint = os.getenv("REQUEST_FINGERPRINT", "")
    run_id = os.getenv("GITHUB_RUN_ID", "")
    repository = os.getenv("GITHUB_REPOSITORY", "")
    server_url = os.getenv("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    if not all((callback_url, callback_secret, request_fingerprint, run_id, repository)):
        raise AdapterError("Worker progress callback configuration is incomplete.")
    payload = {
        "state": "running", "stage": "products_discovered", "sequence": 3,
        "github_run_id": run_id, "github_run_url": f"{server_url}/{repository}/actions/runs/{run_id}",
        "job_id": job.job_id, "contract_version": 2, "request_fingerprint": request_fingerprint,
        "result_summary": _discovery_progress_summary(discovery),
    }
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = "sha256=" + hmac.new(callback_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    try:
        import requests
        response = requests.post(
            f"{callback_url}/api/displacement-jobs/{job.job_id}/callback", data=body,
            headers={"Content-Type": "application/json", "X-SARGuardian-Signature": signature},
            timeout=(10, 30),
        )
        try:
            response.raise_for_status()
        finally:
            response.close()
    except requests.RequestException as exc:
        raise AdapterError("Could not report discovered product counts to the job status service.") from exc
    (job.work_dir / "discovery-callback-ack").touch()


def process_record(record: dict[str, Any], science_root: Path, work_dir: Path, gunw: Any, goff: Any, aoi_wkt: str | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    product = str(record["productType"])
    raw = _raw_path(science_root, record)
    granule = Path(str(record["granule"])).name
    if not granule.startswith("NISAR_"):
        raise AdapterError("Catalogue product identifier failed the safe output-path check.")
    output_root = work_dir / "outputs" / Path(granule).stem
    output_root.mkdir(parents=True, exist_ok=True)
    try:
        inventory = inventory_hdf5(raw, record)
        inventory_path = output_root / "hdf5-inventory.json"
        inventory_path.write_text(json.dumps(inventory, indent=2, sort_keys=True), encoding="utf-8")
        if product == "GUNW":
            result = gunw.read_gunw(raw, coherence_threshold=gunw.DEFAULT_COHERENCE, clip_aoi=True, apply_iono=True, auto_ref=True)
            summary: dict[str, Any] = gunw.report(result)
            export = gunw.export_clipped(result, output_root)
            summary["export"] = export
            exports = [output_root / export["export_file"]] if export.get("export_status") == "WRITTEN" else []
        elif product == "GOFF":
            result = goff.read_goff(raw, layer="best", correlation_min=goff.DEFAULT_CORRELATION, snr_min=goff.DEFAULT_SNR, cull_sigma=goff.CULL_SIGMA if hasattr(goff, "CULL_SIGMA") else 4.0, clip_aoi=True, auto_ref=True, deramp=True)
            summary = {"layers": goff.report(result, span_days=(record["secondary"] - record["reference"]).days)}
            export_rows = goff.export_clipped(result, output_root)
            summary["export"] = export_rows
            exports = [output_root / item["export_file"] for item in export_rows if item.get("export_status") == "WRITTEN" and item.get("export_file")]
        else:  # pragma: no cover - selection prevents this path
            raise AdapterError(f"Unsupported product type {product}.")
        quality_exports = retain_quality_raster(result, product, output_root)
        validation_reports = []
        for export_path in [*exports, *quality_exports]:
            retained_mask = enforce_retention_aoi_mask(export_path, aoi_wkt or os.environ.get("SARGUARDIAN_AOI_WKT", ""))
            validation = validate_raster(export_path, record, aoi_wkt or os.environ.get("SARGUARDIAN_AOI_WKT", ""))
            validation["retention_aoi_mask"] = retained_mask
            report_path = export_path.with_suffix(".validation.json")
            report_path.write_text(json.dumps(validation, indent=2, sort_keys=True), encoding="utf-8")
            validation_reports.append(report_path)
        summary_path = output_root / "product-summary.json"
        summary_path.write_text(json.dumps(summary, default=str, indent=2, sort_keys=True), encoding="utf-8")
        outputs: list[dict[str, Any]] = []
        if exports:
            statistics = write_raster_summary(exports, record, output_root)
            outputs.extend(_artifact(work_dir, path, "aoi_clipped_geotiff", product) for path in exports)
            outputs.extend(_artifact(work_dir, path, "quality_auxiliary_geotiff", product) for path in quality_exports)
            outputs.extend(_artifact(work_dir, path, "raster_validation", product) for path in validation_reports)
            outputs.append(_artifact(work_dir, statistics, "raster_statistics_csv", product))
            for export_path in exports:
                outputs.append(_artifact(work_dir, create_raster_preview(export_path, output_root / "previews"), "raster_preview", product))
            outputs += [_artifact(work_dir, summary_path, "processing_summary", product),
                        _artifact(work_dir, inventory_path, "hdf5_inventory", product)]
        raw_info = {"raw_path": str(raw), "sha256": inventory["sha256"], "bytes": inventory["bytes"],
                    "inventory": _artifact(work_dir, inventory_path, "hdf5_inventory", product)["path"]}
        if os.getenv("SARGUARDIAN_RETENTION_MODE", "derived-only") == "full-lossless":
            archive = output_root / "lossless" / raw.name
            archive.parent.mkdir(parents=True, exist_ok=True)
            import shutil
            shutil.copyfile(raw, archive)
            if sha256_file(archive) != inventory["sha256"]:
                raise AdapterError("Lossless raw HDF5 archive failed byte-for-byte verification.")
            archive_receipt = {"status": "PASS", "method": "byte_identical_copy", "source_sha256": inventory["sha256"], "archive_sha256": sha256_file(archive)}
            receipt_path = output_root / "lossless-retention.json"
            receipt_path.write_text(json.dumps(archive_receipt, indent=2, sort_keys=True), encoding="utf-8")
            outputs += [_artifact(work_dir, archive, "full_lossless_hdf5", product), _artifact(work_dir, receipt_path, "full_lossless_receipt", product)]
        return {"granule": granule, "product_type": product, "reference": str(record["reference"]), "secondary": str(record["secondary"]), "maturity": record.get("maturity"), "crid": record.get("crid"), "export_status": "WRITTEN" if outputs else "NO_VALID_SCIENCE_PIXELS", "raw": raw_info}, outputs
    except ValidationError as exc:
        raise AdapterError(str(exc)) from exc


def run_job(job: JobInput) -> dict[str, Any]:
    UUID(job.job_id)
    if job.end_date < job.start_date:
        raise AdapterError("End date must be on or after start date.")
    if (job.end_date - job.start_date).days > MAX_DATE_WINDOW_DAYS:
        raise AdapterError(f"Date range cannot exceed {MAX_DATE_WINDOW_DAYS} days.")
    if not job.science_root.is_dir():
        raise AdapterError("Pinned science source checkout is unavailable.")
    try:
        aoi = validate_wkt(job.aoi_wkt)
    except ValueError as exc:
        raise AdapterError(str(exc)) from exc
    if job.retention_mode not in {"derived-only", "full-lossless"}:
        raise AdapterError("Unsupported HDF5 retention mode.")
    os.environ["SARGUARDIAN_RETENTION_MODE"] = job.retention_mode
    write_stage(job.work_dir, "validating_input", job_id=job.job_id, aoi_sha256=aoi.sha256, aoi_area_km2=aoi.area_km2)
    acquisition, gunw, goff = _load_science_modules(job.science_root, job.aoi_wkt)
    write_stage(job.work_dir, "discovering_products", start_date=str(job.start_date), end_date=str(job.end_date))
    discovery = catalog.discover(aoi.wkt, job.start_date, job.end_date, product_type=job.product_type,
                                 acquisition_window=job.acquisition_window, processing_profile=job.processing_profile)
    _report_discovery_progress(job, discovery)
    selected = discovery["selected"]
    if not selected:
        raise AdapterError("No eligible GUNW or GOFF products have both acquisitions inside the requested date window.")
    write_stage(job.work_dir, "resolving_download_sizes", selected_product_count=len(selected))
    selected = resolve_download_sizes(selected, acquisition)
    download_gib = enforce_download_budget(selected, job.max_download_gib)
    product_results: list[dict[str, Any]] = []
    validated_outputs: list[dict[str, Any]] = []
    for ordinal, record in enumerate(selected, 1):
        write_stage(job.work_dir, "processing_product", ordinal=ordinal, total=len(selected), granule=record["granule"], product_type=record["productType"])
        raw = _raw_path(job.science_root, record)
        _download_with_retry(acquisition, record, raw, job.work_dir)
        result, outputs = process_record(record, job.science_root, job.work_dir, gunw, goff, job.aoi_wkt)
        if job.stream_upload and outputs:
            outputs = _publish_product_checkpoint(job, result, outputs)
            result["checkpoint"] = "REMOTE_VERIFIED_AND_LOCAL_RAW_CLEANED"
            write_stage(job.work_dir, "product_checkpoint_verified", granule=record["granule"], outputs=len(outputs))
        product_results.append(result)
        validated_outputs.extend(outputs)
    if not validated_outputs:
        raise AdapterError("Eligible products were processed but none contained valid AOI science pixels.")
    manifest = {
        "schema_version": 2,
        "job_id": job.job_id,
        "status": "succeeded",
        "analysis_type": "aoi_clipped_per_product_displacement_screening",
        "scientific_limit": "Per-product AOI-clipped GUNW/GOFF outputs; no arbitrary-AOI time-series, velocity trend, or forecast is claimed.",
        "science_source_repository": SCIENCE_REPOSITORY,
        "science_source_ref": SCIENCE_REF,
        "aoi_wkt_sha256": aoi.sha256,
        "start_date": job.start_date.isoformat(), "end_date": job.end_date.isoformat(),
        "date_policy": job.acquisition_window,
        "product_type": job.product_type, "retention_mode": job.retention_mode, "processing_profile": job.processing_profile,
        "request_fingerprint": processing_fingerprint(aoi_wkt=job.aoi_wkt, start_date=str(job.start_date), end_date=str(job.end_date), product_type=job.product_type, acquisition_window=job.acquisition_window, retention_mode=job.retention_mode, processing_profile=job.processing_profile),
        "selected_product_count": len(selected), "estimated_download_gib": round(download_gib, 3),
        "products": product_results, "validated_outputs": validated_outputs, "discovery": discovery,
        "diagnostics": {
            "candidate_screening": {"status": "SKIPPED_UNSUPPORTED_CUSTOM_AOI", "reason": "The pinned candidate checker contains named-AOI assumptions and is not validated for arbitrary user polygons."},
            "time_series_inversion": {"status": "SKIPPED", "reason": "Per-product AOI exports do not establish a common datum, compatible geometry or connected temporal network for a custom-AOI inversion."},
            "per_pixel_stack": {"status": "INSUFFICIENT_DATA", "reason": "Validation-pilot/per-product processing retains no validated common-lattice multi-date stack. A future stage must prove exact grid, measurement, maturity and reference compatibility before stacking."},
        },
        "warnings": ["NISAR BETA and PROVISIONAL products are not interchangeable without a scientifically validated common processing treatment.", "Structural validation and remote storage verification do not independently validate geophysical accuracy."],
        "cleanup": {"status": "PENDING_REMOTE_STORAGE_VERIFICATION", "raw_files": [item["raw"] for item in product_results]},
    }
    (job.work_dir / "result-manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str), encoding="utf-8")
    write_stage(job.work_dir, "awaiting_verified_storage", validated_output_count=len(validated_outputs))
    return manifest


def cleanup_after_verified_storage(work_dir: Path, science_root: Path) -> dict[str, Any]:
    """Delete only raw files proven uploaded/validated in the current manifest.

    This is intentionally a separate explicit gate.  A process crash, Drive
    failure, failed output check, or missing receipt preserves raw input for the
    runner lifetime and leaves a machine-readable pending cleanup record.
    """
    work_dir, science_root = work_dir.resolve(), science_root.resolve()
    manifest_path = work_dir / "result-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdapterError("Cannot clean raw files without the result manifest.") from exc
    storage = manifest.get("storage_verification")
    if not isinstance(storage, dict) or storage.get("status") != "PASS":
        raise AdapterError("Raw cleanup is blocked until remote result storage is verified.")
    outputs = manifest.get("validated_outputs")
    if not isinstance(outputs, list) or not outputs or any(not isinstance(row, dict) or not row.get("drive_file_id") or not row.get("remote_verification", {}).get("status") == "PASS" for row in outputs):
        raise AdapterError("Raw cleanup is blocked until every validated output has a remote verification receipt.")
    raw_files = manifest.get("cleanup", {}).get("raw_files")
    if not isinstance(raw_files, list):
        raise AdapterError("Raw cleanup manifest is invalid.")
    incoming = (science_root / "data" / "nisar_l2" / "_incoming").resolve()
    deleted = []
    for row in raw_files:
        if not isinstance(row, dict):
            raise AdapterError("Raw cleanup manifest contains an invalid entry.")
        raw_path = Path(str(row.get("raw_path", ""))).resolve()
        if incoming not in raw_path.parents or raw_path.suffix != ".h5" or not raw_path.name.startswith("NISAR_"):
            raise AdapterError("Raw cleanup path failed the workspace safety check.")
        if raw_path.exists():
            if sha256_file(raw_path) != row.get("sha256") or raw_path.stat().st_size != row.get("bytes"):
                raise AdapterError("Raw cleanup is blocked because a source HDF5 changed after validation.")
            raw_path.unlink()
            deleted.append({"name": raw_path.name, "sha256": row["sha256"], "bytes": row["bytes"]})
    cleanup = manifest["cleanup"]
    cleanup.update({"status": "PASS", "deleted": deleted, "deleted_count": len(deleted),
                    "reason": "All validated outputs had verified private remote-storage receipts."})
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    write_stage(work_dir, "raw_cleanup_verified", deleted_count=len(deleted))
    return manifest


def _publish_product_checkpoint(job: JobInput, product: dict[str, Any], outputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Retain one product remotely before the next raw HDF5 download begins."""
    from .upload_to_drive import upload_validated_results
    checkpoint = {
        "schema_version": 2, "status": "succeeded", "job_id": job.job_id,
        "validated_outputs": outputs,
        "cleanup": {"status": "PENDING_REMOTE_STORAGE_VERIFICATION", "raw_files": [product["raw"]]},
    }
    manifest_path = job.work_dir / "result-manifest.json"
    manifest_path.write_text(json.dumps(checkpoint, indent=2, sort_keys=True), encoding="utf-8")
    upload_validated_results(job.work_dir)
    cleanup_after_verified_storage(job.work_dir, job.science_root)
    upload_validated_results(job.work_dir)
    persisted = json.loads(manifest_path.read_text(encoding="utf-8"))
    verified = persisted.get("validated_outputs")
    if not isinstance(verified, list) or any(not isinstance(item, dict) or not item.get("drive_file_id") for item in verified):
        raise AdapterError("Product checkpoint lacks verified private storage receipts.")
    return verified


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--aoi-wkt", required=True)
    parser.add_argument("--start-date", required=True, type=_parse_date)
    parser.add_argument("--end-date", required=True, type=_parse_date)
    parser.add_argument("--science-root", required=True, type=Path)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--max-download-gib", type=float, default=20.0)
    parser.add_argument("--product-type", choices=("BOTH", "GUNW", "GOFF"), default="BOTH")
    parser.add_argument("--acquisition-window", choices=("secondary-acquisition-in-window", "both-acquisitions-in-window"), default="secondary-acquisition-in-window")
    parser.add_argument("--retention-mode", choices=("derived-only", "full-lossless"), default="derived-only")
    parser.add_argument("--processing-profile", choices=("validation-pilot", "full-aoi"), default="validation-pilot")
    parser.add_argument("--cleanup-after-verified-storage", action="store_true")
    parser.add_argument("--stream-upload", action="store_true")
    parser.add_argument("--report-progress", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.cleanup_after_verified_storage:
            cleanup_after_verified_storage(args.work_dir, args.science_root)
        else:
            run_job(JobInput(args.job_id, args.aoi_wkt, args.start_date, args.end_date, args.science_root.resolve(), args.work_dir.resolve(), args.max_download_gib, args.product_type, args.acquisition_window, args.retention_mode, args.processing_profile, args.stream_upload, args.report_progress))
    except (AdapterError, ValueError) as exc:
        logger.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
