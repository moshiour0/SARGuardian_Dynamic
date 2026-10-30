from __future__ import annotations

import math
import logging
import hashlib
import re
from io import BytesIO
from pathlib import PurePosixPath
from zipfile import BadZipFile, ZipFile
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.requests import Request
from fastapi.responses import JSONResponse, Response

from .asf_catalog import ASFAdapterError, ASFAdapterNotConfigured, normalize_product_rows, search_asf_catalog
from .auth import get_current_user
from .config import SettingsError, get_settings, validate_production_settings
from .jobs import CreateJobRequest, JobResponse, JobState
from .known_place import (
    KnownPlaceAnalysisRequest,
    KnownPlaceAnalysisSubmission,
    KnownPlaceWorkflowError,
    dispatch_known_place_analysis,
    get_known_place_analysis_status,
)
from .displacements import (
    ACCOUNT_PROCESSING_OPTIONS,
    PROCESSING_OPTION_DEFAULTS,
    CreateDisplacementJobRequest,
    DisplacementJobResponse,
    GitHubCancellationError,
    GitHubDispatchError,
    ResubmitDisplacementJobRequest,
    WorkerCallback,
    callback_signature_is_valid,
    cancel_workflow_run,
    dispatch_workflow,
    find_dispatched_workflow_run,
    is_dispatch_timed_out,
    request_fingerprint as displacement_request_fingerprint,
    workflow_run_status,
)
from .models import CatalogResponse, CatalogSearchRequest
from .persistence import (
    PersistenceConfigurationError,
    PersistenceConflictError,
    PersistenceError,
    get_job_store,
)
from science_adapter.upload_to_drive import DriveUploadError, download_private_result

logger = logging.getLogger(__name__)

EARTH_RADIUS_METERS = 6_371_000
MAX_AOI_AREA_KM2 = 1_000
MAX_DATE_WINDOW_DAYS = 90
MAX_REQUEST_BODY_BYTES = 1_000_000
ELIGIBILITY_UNDETERMINED_REASON = "Catalogue metadata alone does not establish scientific processing eligibility; the science workflow must inspect the selected products and AOI separately."


def _cors_origins() -> list[str]:
    settings = get_settings()
    configured = settings.cors_origins
    if settings.app_env == "production" and not configured:
        raise RuntimeError("CORS_ORIGINS must be explicitly configured in production.")
    if not configured.strip():
        configured = "http://localhost:5173,http://127.0.0.1:5173"
    return [origin.strip() for origin in configured.split(",") if origin.strip()]


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        validate_production_settings()
    except SettingsError as exc:
        raise RuntimeError(str(exc)) from None
    yield


app = FastAPI(title="SARGuardian Catalogue API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-SARGuardian-Google-Maps-Key"],
)


@app.exception_handler(Exception)
async def handle_unexpected_error(_request: Request, _exc: Exception) -> JSONResponse:
    logger.error("Unhandled API error", exc_info=True)
    return JSONResponse(status_code=500, content={"detail": "Internal server error."})


@app.middleware("http")
async def enforce_request_size(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_REQUEST_BODY_BYTES:
                return JSONResponse(status_code=413, content={"detail": "Request body is too large."})
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "Invalid Content-Length header."})
    return await call_next(request)


def _validate_coordinates(coordinates: list[list[float]]) -> list[tuple[float, float]]:
    if len(coordinates) < 3:
        raise HTTPException(status_code=422, detail="AOI must contain at least 3 coordinates.")

    normalized: list[tuple[float, float]] = []
    for coordinate in coordinates:
        if len(coordinate) != 2:
            raise HTTPException(status_code=422, detail="Each AOI coordinate must contain longitude and latitude.")
        longitude, latitude = coordinate
        if not math.isfinite(longitude) or not math.isfinite(latitude):
            raise HTTPException(status_code=422, detail="AOI coordinates must be finite numbers.")
        if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
            raise HTTPException(status_code=422, detail="AOI coordinates must use WGS84 longitude/latitude ranges.")
        normalized.append((longitude, latitude))

    if normalized[-1] != normalized[0]:
        raise HTTPException(status_code=422, detail="AOI polygon must be closed by repeating its first coordinate.")
    normalized.pop()
    if len(normalized) < 3:
        raise HTTPException(status_code=422, detail="AOI must contain at least 3 distinct polygon vertices.")
    return normalized


def _area_km2(coordinates: list[tuple[float, float]]) -> float:
    ring = [*coordinates, coordinates[0]]
    total = 0.0
    for (longitude_a, latitude_a), (longitude_b, latitude_b) in zip(ring, ring[1:]):
        delta_longitude = math.radians(longitude_b - longitude_a)
        if delta_longitude > math.pi:
            delta_longitude -= 2 * math.pi
        if delta_longitude < -math.pi:
            delta_longitude += 2 * math.pi
        total += delta_longitude * (
            math.sin(math.radians(latitude_a)) + math.sin(math.radians(latitude_b))
        )
    return abs(total) * EARTH_RADIUS_METERS**2 / 2 / 1_000_000


def _validate_request(request: CatalogSearchRequest) -> tuple[float, list[tuple[float, float]]]:
    coordinates = _validate_coordinates(request.coordinates)
    area = _area_km2(coordinates)
    if not math.isfinite(area) or area <= 0:
        raise HTTPException(status_code=422, detail="AOI must have positive area.")
    if area > MAX_AOI_AREA_KM2:
        raise HTTPException(status_code=422, detail=f"AOI exceeds the {MAX_AOI_AREA_KM2} km2 maximum.")

    if request.end_date < request.start_date:
        raise HTTPException(status_code=422, detail="end_date must be on or after start_date.")
    if (request.end_date - request.start_date).days > MAX_DATE_WINDOW_DAYS:
        raise HTTPException(status_code=422, detail=f"Date range cannot exceed {MAX_DATE_WINDOW_DAYS} days.")
    if request.end_date > date.today():
        raise HTTPException(status_code=422, detail="end_date cannot be in the future.")
    return area, coordinates


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "sarguardian-catalogue-api"}


@app.post("/api/known-place-analysis", response_model=KnownPlaceAnalysisSubmission)
def submit_known_place_analysis(
    request: KnownPlaceAnalysisRequest,
    _user: dict = Depends(get_current_user),
) -> KnownPlaceAnalysisSubmission:
    """Dispatch the isolated GOFF Know Place engine, independent of Find Displacements."""
    try:
        return dispatch_known_place_analysis(get_settings(), request)
    except KnownPlaceWorkflowError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


@app.get("/api/known-place-analysis/requests/{request_id}", response_model=KnownPlaceAnalysisSubmission)
def read_known_place_analysis_status(
    request_id: str,
    _user: dict = Depends(get_current_user),
) -> KnownPlaceAnalysisSubmission:
    """Read the requested GitHub run without exposing the server-side Actions token."""
    try:
        return get_known_place_analysis_status(get_settings(), request_id)
    except KnownPlaceWorkflowError as exc:
        code = 422 if "Invalid Known Place request ID" in str(exc) else 503
        raise HTTPException(status_code=code, detail=str(exc)) from None


@app.post("/api/catalog/search", response_model=CatalogResponse)
def search_catalog(request: CatalogSearchRequest, _user: dict = Depends(get_current_user)) -> CatalogResponse:
    area, coordinates = _validate_request(request)
    checked_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    query = {
        "platform": "NISAR",
        "product_type": request.product_type,
        "start_date": request.start_date,
        "end_date": request.end_date,
        "aoi_area_km2": area,
    }
    requested_product_types = "GUNW,GOFF" if request.product_type == "BOTH" else request.product_type
    try:
        result = search_asf_catalog(coordinates, request.start_date, request.end_date, requested_product_types)
    except ASFAdapterNotConfigured:
        return CatalogResponse(
            status="not_configured",
            message="Live ASF catalogue search is disabled by server configuration. No products were returned.",
            checked_at=checked_at,
            query=query,
            processing_eligibility={"status": "undetermined", "reason": ELIGIBILITY_UNDETERMINED_REASON},
            next_step="Set ASF_CATALOG_ENABLED=true to enable the read-only ASF metadata adapter.",
            warnings=["Metadata preflight only: no satellite files were downloaded and no scientific movement analysis was performed."],
        )
    except ASFAdapterError as exc:
        return JSONResponse(status_code=502, content={"status": "upstream_error", "message": str(exc)})

    products, unknown_size, metadata_warnings = normalize_product_rows(result.rows, requested_product_types)
    total_gib = sum(product["size_mb"] or 0 for product in products) / 1024
    warnings = [*metadata_warnings]
    if result.truncated:
        warnings.append("ASF returned the configured result cap; narrow the AOI or date range for a complete search.")
    if unknown_size:
        warnings.append(f"Size metadata is unavailable for {unknown_size} product(s); the volume estimate is incomplete.")
    if not products:
        status = "no_data"
        message = "ASF catalogue search completed with no eligible products matching this AOI and date range."
        next_step = "Try another date range, AOI, or supported product family."
    else:
        status = "ready"
        message = "ASF catalogue metadata search completed. No satellite files were downloaded."
        next_step = "Review the returned metadata before any separate scientific processing workflow."
    response = CatalogResponse(
        status=status,
        message=message,
        checked_at=checked_at,
        query=query,
        processing_eligibility={"status": "undetermined", "reason": ELIGIBILITY_UNDETERMINED_REASON},
        product_count=len(products),
        truncated=result.truncated,
        products_with_unknown_size=unknown_size,
        estimated_full_granule_volume_gib=total_gib,
        estimated_full_granule_volume_is_complete=unknown_size == 0,
        products=products,
        next_step=next_step,
        warnings=[*warnings, "Metadata preflight only: no satellite files were downloaded and no scientific movement analysis was performed."],
    )
    if status != "ready" or request.product_type == "BOTH":
        return response

    try:
        store = get_job_store()
        response.preflight_id = store.save_preflight(
            str(_user["uid"]),
            request.model_dump(mode="json"),
            response.model_dump(mode="json"),
        )
    except PersistenceConfigurationError as exc:
        return JSONResponse(status_code=503, content={"status": "persistence_not_configured", "message": str(exc)})
    except PersistenceError:
        return JSONResponse(status_code=503, content={"status": "persistence_error", "message": "The preflight could not be persisted for job confirmation."})
    return response


def _required_owner_uid(user: dict) -> str:
    owner_uid = user.get("uid")
    if not isinstance(owner_uid, str) or not owner_uid:
        raise HTTPException(status_code=401, detail="Authenticated Firebase user identity is unavailable.")
    return owner_uid


def _require_store():
    try:
        return get_job_store()
    except PersistenceConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


def _job_response(record: dict) -> JobResponse:
    return JobResponse.model_validate(record)


def _displacement_response(record: dict) -> DisplacementJobResponse:
    return DisplacementJobResponse.model_validate(record)


@app.post("/api/jobs", response_model=JobResponse)
def create_job(request: CreateJobRequest, user: dict = Depends(get_current_user)) -> JobResponse:
    owner_uid = _required_owner_uid(user)
    store = _require_store()
    try:
        preflight = store.get_preflight(owner_uid, request.preflight_id)
    except PersistenceError:
        raise HTTPException(status_code=503, detail="Could not read the preflight persistence service.") from None
    if not preflight:
        raise HTTPException(status_code=404, detail="Preflight manifest was not found for this user.")
    try:
        expires_at = datetime.fromisoformat(str(preflight["expires_at"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=409, detail="Preflight manifest is invalid; run the catalogue search again.") from None
    if expires_at <= datetime.now(UTC):
        raise HTTPException(status_code=409, detail="Preflight manifest has expired; run the catalogue search again.")
    manifest = preflight.get("manifest_snapshot")
    request_snapshot = preflight.get("request_snapshot")
    if not isinstance(manifest, dict) or manifest.get("status") != "ready" or not isinstance(request_snapshot, dict):
        raise HTTPException(status_code=409, detail="Only a current successful preflight can be confirmed as a job.")

    try:
        existing = store.get_job_by_idempotency(owner_uid, request.idempotency_key)
    except PersistenceError:
        raise HTTPException(status_code=503, detail="Could not read the job persistence service.") from None
    if existing:
        return _job_response(existing)
    values = {
        "id": str(uuid4()),
        "owner_uid": owner_uid,
        "preflight_id": str(request.preflight_id),
        "idempotency_key": request.idempotency_key,
        "product_family": request_snapshot.get("product_type"),
        "state": JobState.QUEUED.value,
        "request_snapshot": request_snapshot,
        "manifest_snapshot": manifest,
    }
    try:
        return _job_response(store.create_job(values))
    except PersistenceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


@app.get("/api/jobs", response_model=list[JobResponse])
def list_jobs(user: dict = Depends(get_current_user)) -> list[JobResponse]:
    owner_uid = _required_owner_uid(user)
    try:
        records = _require_store().list_jobs(owner_uid)
    except PersistenceError:
        raise HTTPException(status_code=503, detail="Could not read the job persistence service.") from None
    return [_job_response(record) for record in records]


@app.get("/api/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: UUID, user: dict = Depends(get_current_user)) -> JobResponse:
    owner_uid = _required_owner_uid(user)
    try:
        record = _require_store().get_job(owner_uid, job_id)
    except PersistenceError:
        raise HTTPException(status_code=503, detail="Could not read the job persistence service.") from None
    if not record:
        raise HTTPException(status_code=404, detail="Processing job was not found for this user.")
    return _job_response(record)


def _validate_displacement_request(request: CreateDisplacementJobRequest) -> None:
    if request.end_date < request.start_date:
        raise HTTPException(status_code=422, detail="end_date must be on or after start_date.")
    if (request.end_date - request.start_date).days > MAX_DATE_WINDOW_DAYS:
        raise HTTPException(status_code=422, detail=f"Date range cannot exceed {MAX_DATE_WINDOW_DAYS} days.")
    if request.end_date > date.today():
        raise HTTPException(status_code=422, detail="end_date cannot be in the future.")
    if any(getattr(request, name) != value for name, value in ACCOUNT_PROCESSING_OPTIONS.items()):
        raise HTTPException(status_code=422, detail="Account displacement processing uses the fixed production processing contract.")


def _create_and_dispatch_displacement_job(owner_uid: str, request: CreateDisplacementJobRequest, store) -> DisplacementJobResponse:
    try:
        existing = store.get_displacement_job_by_idempotency(owner_uid, request.idempotency_key)
        active = store.find_active_displacement_job(owner_uid, request.aoi_wkt, request.start_date.isoformat(), request.end_date.isoformat())
        active_jobs = store.list_active_displacement_jobs(owner_uid)
    except PersistenceError:
        raise HTTPException(status_code=503, detail="Could not check displacement job persistence.") from None
    if existing:
        if displacement_request_fingerprint(existing) != displacement_request_fingerprint(request.model_dump()):
            raise HTTPException(status_code=409, detail="That idempotency key is already bound to a different displacement request.")
        return _displacement_response(existing)
    if active:
        # A durable active request already exists for this account and exact
        # immutable payload. Never use a page refresh or second browser tab to
        # dispatch a second science workflow.
        if displacement_request_fingerprint(active) == displacement_request_fingerprint(request.model_dump()):
            return _displacement_response(active)
        raise HTTPException(status_code=409, detail="A different processing configuration is already active for this AOI and date window.")
    if active_jobs:
        raise HTTPException(status_code=429, detail="This account already has an active scientific processing job. Wait for it to finish or cancel it before submitting another.")
    values = {
        "owner_uid": owner_uid,
        "idempotency_key": request.idempotency_key,
        "state": JobState.QUEUED.value,
        "aoi_wkt": request.aoi_wkt,
        "start_date": request.start_date.isoformat(),
        "end_date": request.end_date.isoformat(),
        **{name: getattr(request, name) for name in PROCESSING_OPTION_DEFAULTS},
        "place_name": request.place_name,
        "place_latitude": request.place_latitude,
        "place_longitude": request.place_longitude,
        "target_latitude": request.target_latitude,
        "target_longitude": request.target_longitude,
        "request_fingerprint": displacement_request_fingerprint(request.model_dump()),
        "worker_contract_version": 2,
        "stage": "queued",
    }
    try:
        record = store.create_displacement_job(values)
    except PersistenceError:
        try:
            active = store.find_active_displacement_job(owner_uid, request.aoi_wkt, request.start_date.isoformat(), request.end_date.isoformat())
        except PersistenceError:
            active = None
        if active:
            return _displacement_response(active)
        raise HTTPException(status_code=503, detail="Could not persist the displacement job.") from None
    job_id = UUID(str(record["id"]))
    try:
        dispatch_workflow(get_settings(), job_id, request)
        record = store.update_displacement_job(job_id, {"state": JobState.DISPATCHING.value, "stage": "dispatching"})
    except GitHubDispatchError as exc:
        logger.warning("Displacement dispatch failed for job %s: %s", job_id, exc)
        try:
            record = store.update_displacement_job(job_id, {
                "state": JobState.FAILED.value,
                "stage": "dispatch_failed",
                "error_code": "GITHUB_DISPATCH_FAILED",
                "error_message": str(exc),
            })
        except PersistenceError:
            raise HTTPException(status_code=503, detail="Displacement dispatch failed and its status could not be persisted.") from None
    except PersistenceError:
        raise HTTPException(status_code=503, detail="GitHub Actions accepted the job but its dispatch status could not be persisted.") from None
    return _displacement_response(record)


@app.post("/api/displacement-jobs", response_model=DisplacementJobResponse)
def create_displacement_job(request: CreateDisplacementJobRequest, user: dict = Depends(get_current_user)) -> DisplacementJobResponse:
    _validate_displacement_request(request)
    return _create_and_dispatch_displacement_job(_required_owner_uid(user), request, _require_store())


@app.get("/api/displacement-jobs", response_model=list[DisplacementJobResponse])
def list_displacement_jobs(user: dict = Depends(get_current_user)) -> list[DisplacementJobResponse]:
    try:
        records = _require_store().list_displacement_jobs(_required_owner_uid(user))
    except PersistenceError:
        raise HTTPException(status_code=503, detail="Could not list displacement jobs.") from None
    return [_displacement_response(record) for record in records]


def _cancel_active_displacement_job(owner_uid: str, job_id: UUID, store) -> dict:
    """Cancel a GitHub run and persist cancellation before any replacement exists."""
    try:
        current = store.get_displacement_job(owner_uid, job_id)
    except PersistenceError:
        raise HTTPException(status_code=503, detail="Could not read the current displacement job.") from None
    if not current:
        raise HTTPException(status_code=404, detail="Displacement job was not found for this user.")
    if current.get("state") not in {JobState.DISPATCHING.value, JobState.RUNNING.value}:
        raise HTTPException(status_code=409, detail="Only a currently dispatched or running job can be replaced.")

    settings = get_settings()
    run_id = current.get("github_run_id")
    if not run_id:
        discovered = find_dispatched_workflow_run(settings, job_id)
        if not discovered:
            raise HTTPException(status_code=409, detail="The current workflow has not been allocated by GitHub yet. Wait a moment and try refresh again; no replacement was created.")
        run_id, run_url, run_status, _conclusion = discovered
        if run_status == "completed":
            raise HTTPException(status_code=409, detail="The current workflow already completed. Refresh its status before starting a new request.")
        try:
            current = store.update_displacement_job(job_id, {"github_run_id": run_id, "github_run_url": run_url})
        except PersistenceError:
            raise HTTPException(status_code=503, detail="Could not record the current GitHub Actions run.") from None

    try:
        cancel_workflow_run(settings, str(run_id))
    except GitHubCancellationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    try:
        return store.update_displacement_job(job_id, {
            "state": JobState.CANCELLED.value,
            "stage": "cancelled_by_user",
            "error_code": "CANCELLED_BY_USER",
            "error_message": "Cancelled by the account owner.",
        })
    except PersistenceError:
        raise HTTPException(status_code=503, detail="GitHub Actions was cancelled but the account job state could not be updated.") from None


@app.post("/api/displacement-jobs/{job_id}/cancel", response_model=DisplacementJobResponse)
def cancel_displacement_job(job_id: UUID, user: dict = Depends(get_current_user)) -> DisplacementJobResponse:
    """Explicit account-owner cancellation; it never creates a new workflow."""
    owner_uid = _required_owner_uid(user)
    return _displacement_response(_cancel_active_displacement_job(owner_uid, job_id, _require_store()))


@app.post("/api/displacement-jobs/{job_id}/resubmit", response_model=DisplacementJobResponse)
def resubmit_displacement_job(
    job_id: UUID, request: ResubmitDisplacementJobRequest, user: dict = Depends(get_current_user),
) -> DisplacementJobResponse:
    """Retry a failed job or replace an active workflow for its stored AOI/date window."""
    owner_uid = _required_owner_uid(user)
    store = _require_store()
    try:
        current = store.get_displacement_job(owner_uid, job_id)
    except PersistenceError:
        raise HTTPException(status_code=503, detail="Could not read the current displacement job.") from None
    if not current:
        raise HTTPException(status_code=404, detail="Displacement job was not found for this user.")

    if current.get("state") == JobState.FAILED.value:
        # A failed job has no active worker to cancel. Its stored WKT and dates
        # remain the sole source for a retry; current account processing options
        # are fixed server-side.
        source = current
    elif current.get("state") in {JobState.DISPATCHING.value, JobState.RUNNING.value}:
        source = _cancel_active_displacement_job(owner_uid, job_id, store)
    else:
        raise HTTPException(status_code=409, detail="Only a failed, dispatched, or running job can be retried or refreshed.")

    replacement = CreateDisplacementJobRequest(
        aoi_wkt=str(source["aoi_wkt"]), start_date=source["start_date"], end_date=source["end_date"],
        idempotency_key=request.idempotency_key,
        place_name=source.get("place_name"), place_latitude=source.get("place_latitude"),
        place_longitude=source.get("place_longitude"), target_latitude=source.get("target_latitude"),
        target_longitude=source.get("target_longitude"),
        **ACCOUNT_PROCESSING_OPTIONS,
    )
    return _create_and_dispatch_displacement_job(owner_uid, replacement, store)


@app.get("/api/displacement-jobs/{job_id}", response_model=DisplacementJobResponse)
def get_displacement_job(job_id: UUID, user: dict = Depends(get_current_user)) -> DisplacementJobResponse:
    store = _require_store()
    try:
        record = store.get_displacement_job(_required_owner_uid(user), job_id)
    except PersistenceError:
        raise HTTPException(status_code=503, detail="Could not read the displacement job.") from None
    if not record:
        raise HTTPException(status_code=404, detail="Displacement job was not found for this user.")
    settings = get_settings()
    if settings.displacement_actions_enabled and record.get("state") == JobState.DISPATCHING.value:
        discovered_run = find_dispatched_workflow_run(settings, job_id)
        if discovered_run:
            run_id, run_url, status, conclusion = discovered_run
            updates = {"github_run_id": run_id, "github_run_url": run_url}
            should_persist = (
                run_id != record.get("github_run_id") or run_url != record.get("github_run_url")
            )
            if status == "completed":
                code = "WORKER_CALLBACK_MISSING" if conclusion == "success" else "GITHUB_ACTIONS_WORKER_FAILED"
                message = "GitHub Actions completed without a verified success callback." if conclusion == "success" else "GitHub Actions reported a terminal worker failure before its startup callback."
                updates |= {"state": JobState.FAILED.value, "stage": "terminal_validation_failed", "error_code": code, "error_message": message}
                should_persist = True
            if should_persist:
                try:
                    record = store.update_displacement_job(job_id, updates)
                except PersistenceError:
                    raise HTTPException(status_code=503, detail="Could not persist the discovered workflow state.") from None
    if is_dispatch_timed_out(record, settings.displacement_dispatch_timeout_seconds):
        try:
            record = store.update_displacement_job(job_id, {"state": JobState.FAILED.value, "stage": "start_timeout", "error_code": "WORKFLOW_START_TIMEOUT", "error_message": "The workflow did not report a start before the configured timeout."})
        except PersistenceError:
            raise HTTPException(status_code=503, detail="Could not persist the workflow timeout.") from None
    elif settings.displacement_actions_enabled and record.get("github_run_id") and record.get("state") == JobState.RUNNING.value:
        status = workflow_run_status(settings, str(record["github_run_id"]))
        if status and status[0] == "completed":
            code = "WORKER_CALLBACK_MISSING" if status[1] == "success" else "GITHUB_ACTIONS_WORKER_FAILED"
            message = "GitHub Actions completed without a verified success callback." if status[1] == "success" else "GitHub Actions reported a terminal worker failure."
            try:
                record = store.update_displacement_job(job_id, {"state": JobState.FAILED.value, "stage": "terminal_validation_failed", "error_code": code, "error_message": message})
            except PersistenceError:
                raise HTTPException(status_code=503, detail="Could not persist the terminal worker state.") from None
    return _displacement_response(record)


@app.post("/api/displacement-jobs/{job_id}/callback")
async def update_displacement_job_from_worker(job_id: UUID, request: Request) -> dict[str, str]:
    body = await request.body()
    settings = get_settings()
    if not callback_signature_is_valid(settings.displacement_callback_secret, body, request.headers.get("X-SARGuardian-Signature")):
        raise HTTPException(status_code=401, detail="Invalid worker callback signature.")
    try:
        payload = WorkerCallback.model_validate_json(body)
        store = _require_store()
        record = store.get_displacement_job_unscoped(job_id)
    except PersistenceError:
        raise HTTPException(status_code=503, detail="Could not read worker job state.") from None
    if not record:
        raise HTTPException(status_code=404, detail="Displacement job was not found.")
    if record.get("worker_contract_version") == 2:
        if payload.contract_version != 2 or payload.job_id != job_id:
            raise HTTPException(status_code=409, detail="Worker callback is not bound to this version-2 job.")
        expected_fingerprint = record.get("request_fingerprint") or displacement_request_fingerprint(record)
        if payload.request_fingerprint != expected_fingerprint:
            raise HTTPException(status_code=409, detail="Worker callback request fingerprint does not match this job.")
    current = JobState(record["state"])
    target = JobState(payload.state)
    stored_sequence = int(record.get("callback_sequence") or 0)
    stored_run_id = record.get("github_run_id")
    if payload.sequence < stored_sequence:
        raise HTTPException(status_code=409, detail="Stale worker callback sequence.")
    if payload.sequence == stored_sequence:
        if current == target and stored_run_id == payload.github_run_id and record.get("stage") == payload.stage:
            return {"status": "accepted", "job_id": str(job_id), "job_status": current.value}
        raise HTTPException(status_code=409, detail="Duplicate worker callback does not match the recorded event.")
    if payload.sequence != stored_sequence + 1:
        raise HTTPException(status_code=409, detail="Worker callback sequence is out of order.")
    if stored_run_id and stored_run_id != payload.github_run_id:
        raise HTTPException(status_code=409, detail="Worker callback run does not match the recorded workflow run.")
    if target == JobState.SUCCEEDED and current != JobState.RUNNING:
        raise HTTPException(status_code=409, detail="A result cannot be accepted before the worker reports running.")
    if target == JobState.SUCCEEDED and record.get("worker_contract_version") == 2:
        summary = payload.result_summary
        if (not isinstance(summary, dict) or summary.get("schema_version") != 2
                or not isinstance(summary.get("validation"), dict)
                or summary["validation"].get("status") != "PASS"
                or not isinstance(summary.get("storage_verification"), dict)
                or summary["storage_verification"].get("status") != "PASS"
                or not _valid_private_artifacts(summary.get("artifacts"))):
            raise HTTPException(status_code=409, detail="Version-2 success requires a validated scientific result contract.")
    if target == JobState.RUNNING and payload.result_summary is not None:
        summary = payload.result_summary
        counts = summary.get("catalog_granule_counts") if isinstance(summary, dict) else None
        selected_counts = summary.get("selected_granule_counts") if isinstance(summary, dict) else None
        valid_counts = lambda value: isinstance(value, dict) and set(value) == {"GUNW", "GOFF"} and all(
            isinstance(value[family], int) and not isinstance(value[family], bool) and 0 <= value[family] <= 10_000
            for family in ("GUNW", "GOFF")
        )
        if summary.get("schema_version") != 2 or not valid_counts(counts) or not valid_counts(selected_counts):
            raise HTTPException(status_code=409, detail="Worker progress summary contains invalid product counts.")
    allowed = {
        (JobState.DISPATCHING, JobState.RUNNING), (JobState.DISPATCHING, JobState.FAILED),
        (JobState.RUNNING, JobState.RUNNING), (JobState.RUNNING, JobState.SUCCEEDED), (JobState.RUNNING, JobState.FAILED),
    }
    if (current, target) not in allowed:
        raise HTTPException(status_code=409, detail="Worker callback requested an invalid state transition.")
    try:
        retained_summary = (payload.result_summary if target in {JobState.RUNNING, JobState.SUCCEEDED}
                            and payload.result_summary is not None else record.get("result_summary"))
        updated = store.update_displacement_job(job_id, {
            "state": target.value, "github_run_id": payload.github_run_id,
            "github_run_url": payload.github_run_url, "error_code": payload.error_code if target == JobState.FAILED else None,
            "error_message": payload.error_message if target == JobState.FAILED else None, "callback_sequence": payload.sequence,
            "stage": payload.stage, "result_summary": retained_summary,
            "artifact_url": payload.artifact_url if target == JobState.SUCCEEDED else None,
        }, expected_state=current.value, expected_sequence=stored_sequence)
    except PersistenceConflictError:
        raise HTTPException(status_code=409, detail="Job state changed while this callback was being accepted.") from None
    except PersistenceError:
        raise HTTPException(status_code=503, detail="Could not persist worker job state.") from None
    return {"status": "accepted", "job_id": str(job_id), "job_status": updated["state"]}


def _valid_private_artifacts(value: object) -> bool:
    if not isinstance(value, list) or not value:
        return False
    import re
    for artifact in value:
        if not isinstance(artifact, dict):
            return False
        path, digest, size, drive_id = (artifact.get("path"), artifact.get("sha256"),
                                        artifact.get("bytes"), artifact.get("drive_file_id"))
        if (not isinstance(path, str) or path.startswith("/") or ".." in path.split("/")
                or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
                or not isinstance(size, int) or size <= 0
                or not isinstance(drive_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", drive_id)):
            return False
    return True


MAX_DASHBOARD_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_DASHBOARD_HTML_BYTES = 2 * 1024 * 1024
MAX_DASHBOARD_DATA_BYTES = 40 * 1024 * 1024
DASHBOARD_VIEWER_PATH = "web-dashboard/index.html"
DASHBOARD_DATA_PATH = "web-dashboard/dashboard-data.js"

# Presentation is added at read-time so older, checksum-verified result bundles
# can receive responsive layout and truthful measurement-state messaging without
# repeating expensive NISAR processing or rewriting retained scientific assets.
DASHBOARD_PRESENTATION_CSS = r"""
:root{color-scheme:dark;--bg:#06111b;--panel:#0c1c29;--panel-raised:#102838;--edge:#274455;--ink:#edf7ff;--muted:#a9bdcb;--aqua:#70e6dc;--amber:#ffcf78;--danger:#ff8a80}
html{scroll-behavior:smooth;background:var(--bg)}
body{margin:0;background:radial-gradient(1100px 520px at 18% -8%,#163e4d 0%,transparent 65%),linear-gradient(155deg,#06111b,#081825 60%,#07121d);color:var(--ink);font:15px/1.58 Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;letter-spacing:.005em}
main{width:min(100%,1760px);max-width:1760px;margin:0 auto;padding:clamp(14px,2.3vw,36px)}
header{align-items:center;gap:20px;padding:10px 0 24px;border-color:rgba(134,190,207,.19)}
header>div:first-child{min-width:0;max-width:78ch}
h1{font-size:clamp(28px,3.3vw,46px);line-height:1.08;letter-spacing:-.045em;font-weight:800;margin:9px 0 10px}
h2{font-size:clamp(19px,2vw,25px);line-height:1.2;letter-spacing:-.025em}
p{max-width:82ch}
.kicker{font-size:10px;line-height:1.4;letter-spacing:.17em}
.muted{color:var(--muted);line-height:1.65}
button,select,input{font:inherit}
button,select{border:1px solid #35596b;border-radius:10px;background:linear-gradient(180deg,#153446,#102838);padding:9px 12px;min-height:40px}
button{transition:transform .16s ease,border-color .16s ease,background .16s ease}
button:hover{border-color:#70e6dc;background:#174052}
button:active{transform:translateY(1px)}
button:focus-visible,select:focus-visible,input:focus-visible{outline:2px solid var(--aqua);outline-offset:3px}
header .controls{display:flex;align-items:center;justify-content:flex-end;gap:10px;flex-wrap:wrap}
header .controls button{font-weight:700}
header .controls a,header .controls span{font-size:12px}
.warn{margin:20px 0;padding:14px 16px;border:1px solid rgba(255,207,120,.5);border-left:4px solid var(--amber);background:linear-gradient(100deg,rgba(105,72,26,.35),rgba(105,72,26,.12));border-radius:12px;color:#ffebc5;line-height:1.65}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,158px),1fr));gap:12px;margin:18px 0}
.card,.panel{min-width:0;border:1px solid rgba(116,175,196,.2);border-radius:18px;background:linear-gradient(145deg,rgba(17,42,57,.96),rgba(9,24,36,.96));padding:clamp(14px,1.35vw,22px);box-shadow:0 16px 40px rgba(0,0,0,.14);backdrop-filter:blur(10px)}
.card{position:relative;overflow:hidden}
.card:before{content:"";position:absolute;inset:0 0 auto;height:2px;background:linear-gradient(90deg,rgba(112,230,220,.7),transparent 80%)}
.label{color:#a9bfcd;font-size:11px;font-weight:750;line-height:1.35;letter-spacing:.09em;text-transform:uppercase}
.big{font-size:clamp(22px,2.2vw,32px);line-height:1.12;font-weight:800;letter-spacing:-.04em;margin-top:9px;overflow-wrap:anywhere}
.layout{display:grid;grid-template-columns:minmax(0,1.65fr) minmax(320px,.85fr);align-items:start;gap:18px}
.layout>.panel{min-width:0}
.maphead{align-items:flex-start;flex-wrap:wrap;gap:14px;margin-bottom:14px}
.maphead>div:first-child{min-width:0}
.maphead strong{font-size:clamp(14px,1.3vw,18px);letter-spacing:-.015em}
.controls{display:flex;align-items:center;gap:9px;flex-wrap:wrap;min-width:0}
.controls label{display:inline-flex;align-items:center;gap:7px;flex-wrap:wrap;color:var(--muted);font-size:12px}
.controls select{max-width:100%;min-width:0}
.mapbox{position:relative;isolation:isolate;overflow:hidden;width:100%;height:clamp(380px,58svh,690px);min-height:340px;border:1px solid rgba(112,230,220,.2);border-radius:15px;background:radial-gradient(ellipse at 50% 42%,#0d2a39 0%,#071722 68%,#06111a 100%)}
#map,.compare-mapbox>div{width:100%;height:100%;min-height:0;background:transparent}
.maplibregl-canvas,.maplibregl-canvas-container{outline:none}
.maplibregl-ctrl-group{border:1px solid rgba(150,205,217,.25)!important;border-radius:10px!important;overflow:hidden;background:rgba(7,25,36,.92)!important}
.maplibregl-ctrl-group button{background-color:transparent!important}
.maplibregl-ctrl-attrib{background:rgba(4,15,24,.82)!important;color:#d6e8ef!important}
.maplibregl-ctrl-attrib a{color:#8eece4!important}
.map-basemap-status{position:absolute;z-index:5;left:12px;right:12px;bottom:12px;margin:auto;max-width:620px;padding:12px 14px;border:1px solid rgba(255,207,120,.5);border-radius:12px;background:rgba(10,28,39,.96);color:#ffe9bc;box-shadow:0 8px 30px rgba(0,0,0,.25);font-size:13px;line-height:1.5}
.timeline{display:grid;grid-template-columns:auto minmax(0,1fr) auto;align-items:center;gap:12px;margin-top:14px}
.timeline .controls{display:flex;gap:7px}
input[type=range]{width:100%;min-width:0;accent-color:var(--aqua)}
.timeline small{color:var(--muted);font-size:12px;white-space:nowrap}
.metriclist{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px;margin:14px 0 18px}
.metricitem{min-width:0;padding:12px;border:1px solid rgba(116,175,196,.18);border-radius:12px;background:rgba(4,17,28,.68)}
.metricitem .label{font-size:10px;letter-spacing:.07em}
.metricitem strong{display:block;margin-top:5px;font-size:clamp(13px,1.05vw,16px);line-height:1.4;font-weight:750;overflow-wrap:anywhere}
.metricitem small{display:block;margin-top:6px;color:var(--muted);font-size:12px;line-height:1.55;overflow-wrap:anywhere}
.chart{display:block;width:100%;height:auto;min-height:170px;margin:10px 0 14px;overflow:visible}
#supportChart{min-height:145px}
details{margin-top:14px;border:1px solid rgba(116,175,196,.19);border-radius:14px;background:rgba(4,17,28,.44);padding:14px}
details>summary{cursor:pointer;font-weight:750;line-height:1.5}
details>summary::marker{color:var(--aqua)}
details[open]>summary{margin-bottom:12px;color:var(--aqua)}
.compare-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.compare-mapbox{height:300px;min-height:260px;overflow:hidden;border:1px solid var(--edge);border-radius:12px;background:#071722}
.compare-map-title{font-size:14px}
.compare-controls{display:flex;gap:10px;flex-wrap:wrap}
.compare-controls label{display:grid;gap:6px;min-width:min(100%,170px);flex:1}
.scenario-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}
.scenario-grid label{display:grid;align-content:start;gap:7px;min-width:0;color:var(--muted);font-size:12px}
.scenario-grid input{width:100%;min-width:0}
.legend{margin:12px 0;color:var(--muted);font-size:12px;line-height:1.6;overflow-wrap:anywhere}
.legendbar{height:10px;margin-top:9px;border-radius:99px;border:1px solid rgba(255,255,255,.15)}
.foot{margin:26px 0 0;padding:18px 0 8px;border-top:1px solid rgba(116,175,196,.18);color:#91aab9;font-size:12px;line-height:1.65}
.run-readiness{margin:0 0 18px;padding:clamp(15px,1.7vw,23px);border:1px solid rgba(112,230,220,.2);border-radius:18px;background:linear-gradient(115deg,rgba(13,42,56,.95),rgba(8,24,36,.96));box-shadow:0 14px 34px rgba(0,0,0,.12)}
.readiness-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:13px}
.readiness-head h2{margin:6px 0 0}
.readiness-intro{margin:8px 0 0;max-width:86ch;color:var(--muted);font-size:13px;line-height:1.6}
.readiness-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}
.readiness-card{min-width:0;padding:14px;border:1px solid rgba(116,175,196,.19);border-radius:13px;background:rgba(3,16,26,.56)}
.readiness-card .readiness-value{display:block;margin:8px 0 4px;color:var(--ink);font-size:clamp(18px,1.8vw,25px);font-weight:800;line-height:1.2;letter-spacing:-.025em;overflow-wrap:anywhere}
.readiness-card p{margin:0;color:var(--muted);font-size:12px;line-height:1.6}
.readiness-badge{display:inline-flex;align-items:center;gap:6px;padding:4px 8px;border-radius:99px;background:rgba(255,207,120,.12);color:#ffdfa0;font-size:10px;font-weight:800;letter-spacing:.08em;text-transform:uppercase}
.readiness-badge.good{background:rgba(112,230,220,.12);color:#8af2e4}
@media(max-width:1050px){main{padding:18px}.layout{grid-template-columns:minmax(0,1fr)}.layout>aside{grid-row:auto}.mapbox{height:clamp(340px,55svh,580px)}.readiness-grid{grid-template-columns:repeat(3,minmax(0,1fr))}.compare-mapbox{height:280px}}
@media(max-width:680px){main{padding:12px 10px 24px}header{align-items:flex-start;gap:13px;padding-bottom:18px}header>div:first-child{width:100%;max-width:none}header .controls{width:100%;justify-content:flex-start;gap:8px}h1{font-size:clamp(26px,7vw,34px)}.cards{grid-template-columns:repeat(2,minmax(0,1fr));gap:9px;margin:14px 0}.card,.panel{padding:13px;border-radius:14px}.label{font-size:10px}.big{font-size:clamp(20px,6vw,27px);margin-top:7px}.layout{grid-template-columns:minmax(0,1fr);gap:12px}.maphead{display:grid;grid-template-columns:minmax(0,1fr);gap:10px}.maphead .controls{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:9px}.maphead .controls label{display:grid;align-content:start;gap:5px;min-width:0}.maphead .controls label:first-child{grid-column:1/-1}.maphead .controls select{width:100%;max-width:none}.mapbox{height:54svh;min-height:320px;max-height:480px}.timeline{grid-template-columns:minmax(0,1fr);gap:9px}.timeline small{white-space:normal}.metriclist{grid-template-columns:repeat(2,minmax(0,1fr));gap:7px}.metricitem{padding:10px}.metricitem strong{font-size:13px}.chart{min-height:155px}.compare-grid,.scenario-grid{grid-template-columns:minmax(0,1fr)}.compare-mapbox{height:260px;min-height:240px}.compare-controls{display:grid;grid-template-columns:minmax(0,1fr)}.compare-controls label{min-width:0}.readiness-grid{grid-template-columns:minmax(0,1fr)}.readiness-card{padding:12px}.run-readiness{border-radius:14px;padding:13px}.warn{padding:12px;font-size:13px}.foot{font-size:11px}}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}*,*::before,*::after{animation-duration:.01ms!important;transition-duration:.01ms!important}}
"""
DASHBOARD_READINESS_MARKUP = """
<section class="run-readiness" id="sgRunReadiness" aria-label="Measurement readiness" aria-live="polite">
  <div class="readiness-head">
    <div><div class="kicker">RUN INTERPRETATION</div><h2>Measurement readiness</h2>
      <p class="readiness-intro">A clearer distinction between what this run measured, what remains unavailable, and what must not be inferred from missing values.</p>
    </div>
  </div>
  <div class="readiness-grid" id="sgRunReadinessGrid"></div>
</section>
"""
DASHBOARD_READINESS_SCRIPT = r"""<script id="sg-dashboard-readiness">
(function(){
  function mountReadiness(){
    var manifest=window.SARGUARDIAN_DASHBOARD;
    var root=document.getElementById("sgRunReadinessGrid");
    if(!manifest||!root)return;
    var obs=Array.isArray(manifest.observations)?manifest.observations:[];
    var measurements=obs.filter(function(p){var m=p&&p.metrics||{};return m.target_measurement_status==="MEASURED"&&(m.target_local_los_median_mm!=null||m.target_local_range_median_mm!=null||m.target_local_azimuth_median_mm!=null);}).length;
    var floors=obs.filter(function(p){var m=p&&p.metrics||{};return m.target_local_floor_status==="MEASURED"&&m.target_local_floor_mm_day!=null;}).length;
    var context=manifest.site_context||{},place=context.place||{},target=context.measurement_target||{};
    var placeName=typeof place.name==="string"&&place.name.trim()?place.name.trim():"No named place selected";
    var targetStatus=String(target.status||"NOT_REPORTED");
    var targetDescription=placeName==="No named place selected"?
      "No display name is attached to this run. A product CRID is provenance metadata, not evidence that target-local motion was measured.":
      "Named place context is attached. Check each selected pair before interpreting target-specific values.";
    if(targetStatus==="IN_AOI_TARGET_CAPTURED")targetDescription+=" A target coordinate is recorded inside the AOI.";
    else {
      targetDescription+=" No valid in-AOI measurement target was recorded for this run. To report target-local displacement and local detection floors, perform a target-aware measurement pass with both target_latitude and target_longitude set to an explicit WGS84 point inside this polygon. Do not substitute the AOI center or product CRID for a target coordinate.";
      if(targetStatus!=="NOT_REPORTED"&&targetStatus!=="")targetDescription+=" Target state: "+targetStatus.replace(/_/g," ").toLowerCase()+".";
    }
    var rows=[
      {label:"Target-local displacement",value:measurements+" / "+obs.length+" pairs measured",note:measurements===0?"Not measured is not the same as zero motion. No target-local LOS/range/along-track displacement is reported for these pairs.":"Only pairs marked MEASURED are counted; do not fill missing pairs with zero.",good:measurements>0},
      {label:"Local detection floor",value:floors+" / "+obs.length+" pairs measured",note:floors===0?"No measured target-local detection floor is available. A missing floor is not a 0 mm/day threshold.":"Noise/detectability bounds are not observed displacement or velocity.",good:floors>0},
      {label:"Place / target context",value:placeName,note:targetDescription,good:placeName!=="No named place selected"||targetStatus==="IN_AOI_TARGET_CAPTURED"}
    ];
    root.replaceChildren();
    rows.forEach(function(row){
      var card=document.createElement("div");card.className="readiness-card";
      var label=document.createElement("div");label.className="label";label.textContent=row.label;
      var badge=document.createElement("span");badge.className="readiness-badge"+(row.good?" good":"");badge.textContent=row.good?"REPORTED":"NOT REPORTED";
      var value=document.createElement("strong");value.className="readiness-value";value.textContent=row.value;
      var note=document.createElement("p");note.textContent=row.note;
      card.append(label,badge,value,note);root.append(card);
    });
  }
  if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",mountReadiness,{once:true});else mountReadiness();
})();
</script>
"""

def _apply_dashboard_presentation(html: str) -> str:
    """Enhance an already verified dashboard document without changing its data."""
    if "</head>" not in html or "</body>" not in html:
        raise ValueError("Interactive dashboard HTML is missing its document boundaries.")
    if 'name="referrer"' not in html:
        html = html.replace("</head>", '<meta name="referrer" content="strict-origin-when-cross-origin"></head>', 1)

    if "/* SARGuardian responsive science dashboard */" not in html:
        if "</style>" in html:
            html = html.replace("</style>", "/* SARGuardian responsive science dashboard */\n" + DASHBOARD_PRESENTATION_CSS + "\n</style>", 1)
        else:
            html = html.replace("</head>", "<style>/* SARGuardian responsive science dashboard */\n" + DASHBOARD_PRESENTATION_CSS + "\n</style></head>", 1)

    cards_anchor = '<div class="cards" id="summaryCards"></div>'
    if cards_anchor in html and 'id="sgRunReadiness"' not in html:
        html = html.replace(cards_anchor, cards_anchor + DASHBOARD_READINESS_MARKUP, 1)

    map_anchor = '<div id="map"></div>'
    status_markup = '<div id="mapBasemapStatus" class="map-basemap-status hidden" role="status">Basemap tiles are blocked or unavailable. The georeferenced AOI and scientific raster overlays remain available on the map canvas.</div>'
    if map_anchor in html and 'id="mapBasemapStatus"' not in html:
        html = html.replace(map_anchor, map_anchor + status_markup, 1)

    primary_hook = "M.addControl(new maplibregl.NavigationControl(),'top-right');M.on('load',function(){"
    primary_replacement = (
        "M.addControl(new maplibregl.NavigationControl(),'top-right');"
        "let osmAccessErrors=0;M.on('error',function(event){"
        "if(event&&event.sourceId==='osm'&&event.error&&[401,403,429].includes(event.error.status)){"
        "osmAccessErrors++;if(osmAccessErrors>=2&&M.getLayer('osm')){M.removeLayer('osm');"
        "if(M.getSource('osm'))M.removeSource('osm');"
        "const status=document.getElementById('mapBasemapStatus');if(status)status.classList.remove('hidden');}}});"
        "M.on('load',function(){"
    )
    if primary_hook in html and "let osmAccessErrors=0" not in html:
        html = html.replace(primary_hook, primary_replacement, 1)

    compare_hook = "map.addControl(new maplibregl.NavigationControl(),'top-right');\n map.on('load',function(){"
    compare_replacement = (
        "map.addControl(new maplibregl.NavigationControl(),'top-right');"
        "let compareOsmAccessErrors=0;map.on('error',function(event){"
        "if(event&&event.sourceId==='osm'&&event.error&&[401,403,429].includes(event.error.status)){"
        "compareOsmAccessErrors++;if(compareOsmAccessErrors>=2&&map.getLayer('osm')){map.removeLayer('osm');"
        "if(map.getSource('osm'))map.removeSource('osm');}}});"
        "\n map.on('load',function(){"
    )
    if compare_hook in html and "let compareOsmAccessErrors=0" not in html:
        html = html.replace(compare_hook, compare_replacement, 1)

    if 'id="sg-dashboard-readiness"' not in html:
        html = html.replace("</body>", DASHBOARD_READINESS_SCRIPT + "</body>", 1)
    return html


DASHBOARD_GOOGLE_MAPS_ADAPTER_SCRIPT = r"""<script>
window.SARGUARDIAN_GOOGLE_MAPS_ADAPTER = new Promise(function(resolve) {
  var complete = false;
  function unavailable(message) {
    if (complete) return;
    complete = true;
    window.SARGUARDIAN_GOOGLE_MAPS_ERROR = message;
    resolve({ Map: null, NavigationControl: function() {} });
  }
  var callbackName = "__sarguardianGoogleMapsLoaded";
  window.gm_authFailure = function() {
    window.SARGUARDIAN_GOOGLE_MAPS_ERROR = "Google Maps rejected the browser API key. Check Maps JavaScript API enablement and HTTP-referrer restrictions.";
  };
  window.addEventListener("securitypolicyviolation", function(event) {
    var blocked = String(event.blockedURI || "");
    if (/googleapis\.com|gstatic\.com|google\.com/i.test(blocked)
        && !window.SARGUARDIAN_GOOGLE_MAPS_ERROR) {
      window.SARGUARDIAN_GOOGLE_MAPS_ERROR =
        "Google Maps was blocked by Content Security Policy (" + event.violatedDirective + ").";
    }
  });
  var script = document.createElement("script");
  script.src = "https://maps.googleapis.com/maps/api/js?key=__GOOGLE_MAPS_API_KEY__&v=weekly&loading=async&callback=" + callbackName;
  script.async = true;
  script.defer = true;
  script.referrerPolicy = "origin";
  // With loading=async, API readiness is signalled by the Google callback,
  // not by the element's ordinary load event.
  window[callbackName] = function() {
    if (window.SARGUARDIAN_GOOGLE_MAPS_ERROR) {
      unavailable(window.SARGUARDIAN_GOOGLE_MAPS_ERROR);
      return;
    }
    if (!window.google || !google.maps || typeof google.maps.Map !== "function") {
      unavailable("Google Maps callback ran but the Maps JavaScript API did not initialize.");
      return;
    }
    if (complete) return;
    complete = true;

    function featuresFor(data) {
      if (!data) return [];
      if (data.type === "FeatureCollection") {
        return Array.isArray(data.features)
          ? data.features.filter(function(feature) { return feature && feature.geometry; })
          : [];
      }
      if (data.type === "Feature") return data.geometry ? [data] : [];
      return [{ type: "Feature", properties: {}, geometry: data }];
    }
    function geometryTypeFor(geometry) {
      if (!geometry) return "";
      return geometry.type === "MultiPolygon" ? "Polygon" : geometry.type;
    }
    function polygonPathGroups(geometry) {
      if (!geometry) return [];
      var polygons = geometry.type === "Polygon"
        ? [geometry.coordinates || []]
        : geometry.type === "MultiPolygon" ? (geometry.coordinates || []) : [];
      return polygons.map(function(rings) {
        return rings.map(function(ring) {
          return ring.map(function(coord) {
            return { lat: Number(coord[1]), lng: Number(coord[0]) };
          }).filter(function(point) {
            return Number.isFinite(point.lat) && Number.isFinite(point.lng);
          });
        }).filter(function(path) { return path.length >= 3; });
      }).filter(function(paths) { return paths.length > 0; });
    }
    function pointFor(geometry) {
      if (!geometry || geometry.type !== "Point" || !Array.isArray(geometry.coordinates)) return null;
      return { lat: Number(geometry.coordinates[1]), lng: Number(geometry.coordinates[0]) };
    }
    function valueFor(expression, feature) {
      if (!Array.isArray(expression)) return expression;
      var operator = expression[0];
      if (operator === "get") return (feature.properties || {})[expression[1]];
      if (operator === "geometry-type") return geometryTypeFor(feature.geometry);
      if (operator === "==") return valueFor(expression[1], feature) === valueFor(expression[2], feature);
      if (operator === "!=") return valueFor(expression[1], feature) !== valueFor(expression[2], feature);
      if (operator === "!") return !valueFor(expression[1], feature);
      if (operator === "all") return expression.slice(1).every(function(item) { return Boolean(valueFor(item, feature)); });
      if (operator === "any") return expression.slice(1).some(function(item) { return Boolean(valueFor(item, feature)); });
      if (operator === "case") {
        for (var index = 1; index < expression.length - 1; index += 2) {
          if (Boolean(valueFor(expression[index], feature))) return valueFor(expression[index + 1], feature);
        }
        return valueFor(expression[expression.length - 1], feature);
      }
      return undefined;
    }
    function matchesFilter(filter, feature) {
      if (!Array.isArray(filter) || !filter.length) return true;
      if (filter[0] === "==") return valueFor(filter[1], feature) === valueFor(filter[2], feature);
      if (filter[0] === "!=") return valueFor(filter[1], feature) !== valueFor(filter[2], feature);
      if (filter[0] === "all") return filter.slice(1).every(function(item) { return matchesFilter(item, feature); });
      if (filter[0] === "any") return filter.slice(1).some(function(item) { return matchesFilter(item, feature); });
      if (filter[0] === "!") return !matchesFilter(filter[1], feature);
      return false;
    }
    function paintValue(value, feature, fallback) {
      var resolved = valueFor(value, feature);
      return resolved === undefined || resolved === null ? fallback : resolved;
    }
    function boundsForCorners(corners) {
      if (!Array.isArray(corners) || corners.length < 4) return null;
      var lngs = corners.map(function(p) { return Number(p[0]); });
      var lats = corners.map(function(p) { return Number(p[1]); });
      if (lngs.some(function(v) { return !Number.isFinite(v); }) || lats.some(function(v) { return !Number.isFinite(v); })) return null;
      return { west: Math.min.apply(null, lngs), east: Math.max.apply(null, lngs), south: Math.min.apply(null, lats), north: Math.max.apply(null, lats) };
    }
    class GoogleMapCompatibilityAdapter {
      constructor(options) {
        this.container = typeof options.container === "string" ? document.getElementById(options.container) : options.container;
        if (!this.container) throw new Error("Google Maps container was not found.");
        this.sources = new globalThis.Map();
        this.layers = new globalThis.Map();
        this.handlers = new globalThis.Map();
        this.layerHandlers = new globalThis.Map();
        this.isLoaded = false;
        var center = Array.isArray(options.center) ? { lat: Number(options.center[1]), lng: Number(options.center[0]) } : (options.center || { lat: 0, lng: 0 });
        this.map = new google.maps.Map(this.container, {
          center: center,
          zoom: Number(options.zoom) || 2,
          mapTypeId: google.maps.MapTypeId.TERRAIN,
          mapTypeControl: true,
          mapTypeControlOptions: { style: google.maps.MapTypeControlStyle.DROPDOWN_MENU },
          fullscreenControl: false,
          streetViewControl: false,
          zoomControl: true,
          clickableIcons: false,
          backgroundColor: "#071722",
          controlSize: 28,
          styles: [
            { elementType: "labels.text.fill", stylers: [{ color: "#d9e7ed" }] },
            { elementType: "labels.text.stroke", stylers: [{ color: "#132c3a" }, { weight: 3 }] },
            { featureType: "water", elementType: "geometry", stylers: [{ color: "#102d42" }] }
          ]
        });
        var self = this;
        google.maps.event.addListenerOnce(this.map, "idle", function() {
          self.isLoaded = true;
          self.emit("load");
        });
        google.maps.event.addListener(this.map, "idle", function() { self.emit("move"); });
      }
      emit(name, payload) {
        (this.handlers.get(name) || []).slice().forEach(function(callback) {
          try { callback(payload); } catch (error) { console.error("Dashboard map callback failed.", error); }
        });
      }
      layerHandlerKey(name, layerId) { return String(name) + "|" + String(layerId); }
      attachLayerHandler(entry, name, callback) {
        if (name !== "click") return;
        entry.overlays.forEach(function(item) {
          if (!item.overlay || typeof item.overlay.addListener !== "function" || !item.feature) return;
          var listener = item.overlay.addListener("click", function(event) {
            var position = event && event.latLng;
            var lat = position && typeof position.lat === "function" ? position.lat() : Number(position && position.lat);
            var lng = position && typeof position.lng === "function" ? position.lng() : Number(position && position.lng);
            callback({
              type: "click",
              target: entry,
              features: [item.feature],
              lngLat: { lng: lng, lat: lat },
              originalEvent: event && (event.domEvent || event)
            });
          });
          entry.listeners.push(listener);
        });
      }
      on(name, layerIdOrCallback, callback) {
        if (typeof layerIdOrCallback === "function") {
          var callbacks = this.handlers.get(name) || [];
          callbacks.push(layerIdOrCallback);
          this.handlers.set(name, callbacks);
          if (name === "load" && this.isLoaded) Promise.resolve().then(layerIdOrCallback);
          return this;
        }
        var layerId = String(layerIdOrCallback);
        var key = this.layerHandlerKey(name, layerId);
        var layerCallbacks = this.layerHandlers.get(key) || [];
        layerCallbacks.push(callback);
        this.layerHandlers.set(key, layerCallbacks);
        var entry = this.layers.get(layerId);
        if (entry) this.attachLayerHandler(entry, name, callback);
        return this;
      }
      addControl() { /* Native Google Maps controls are enabled in the map options. */ }
      addSource(id, source) { this.sources.set(id, source); return this; }
      getSource(id) { return this.sources.get(id); }
      removeSource(id) { this.sources.delete(id); return this; }
      getLayer(id) { return this.layers.get(id); }
      removeLayer(id) {
        var layer = this.layers.get(id);
        if (!layer) return this;
        layer.listeners.forEach(function(listener) {
          if (listener && typeof listener.remove === "function") listener.remove();
        });
        layer.overlays.forEach(function(item) {
          if (item.overlay && typeof item.overlay.setMap === "function") item.overlay.setMap(null);
        });
        this.layers.delete(id);
        return this;
      }
      addLayer(spec) {
        var source = this.sources.get(spec.source);
        if (!source) throw new Error("Dashboard layer source is missing: " + spec.source);
        var paint = spec.paint || {};
        var visible = !(spec.layout && spec.layout.visibility === "none");
        var overlays = [];
        if (spec.type === "raster") {
          var imageBounds = boundsForCorners(source.coordinates);
          if (imageBounds && source.url) {
            overlays.push({
              overlay: new google.maps.GroundOverlay(source.url, imageBounds, {
                opacity: Number.isFinite(Number(paint["raster-opacity"])) ? Number(paint["raster-opacity"]) : 0.82,
                clickable: false
              }),
              feature: null
            });
          }
        } else {
          featuresFor(source.data).forEach(function(feature) {
            if (!matchesFilter(spec.filter, feature)) return;
            if (spec.type === "fill" || spec.type === "line") {
              polygonPathGroups(feature.geometry).forEach(function(paths) {
                var fillOpacity = Number(paint["fill-opacity"]);
                var strokeWeight = Number(paint["line-width"]);
                var overlay = new google.maps.Polygon({
                  paths: paths,
                  clickable: spec.type === "fill",
                  geodesic: false,
                  fillColor: paintValue(paint["fill-color"], feature, "#66e2d3"),
                  fillOpacity: Number.isFinite(fillOpacity) ? fillOpacity : (spec.type === "fill" ? 0.08 : 0),
                  strokeColor: paintValue(paint["line-color"], feature, "#66e2d3"),
                  strokeOpacity: spec.type === "line" ? 0.95 : 0,
                  strokeWeight: Number.isFinite(strokeWeight) ? strokeWeight : (spec.type === "line" ? 2 : 0),
                  zIndex: spec.type === "line" ? 30 : 10
                });
                overlays.push({ overlay: overlay, feature: feature });
              });
            } else if (spec.type === "circle") {
              var point = pointFor(feature.geometry);
              if (point && Number.isFinite(point.lat) && Number.isFinite(point.lng)) {
                overlays.push({
                  overlay: new google.maps.Marker({
                    position: point,
                    clickable: false,
                    zIndex: 50,
                    icon: {
                      path: google.maps.SymbolPath.CIRCLE,
                      scale: Math.max(3, Number(paint["circle-radius"]) || 6),
                      fillColor: paintValue(paint["circle-color"], feature, "#f7c76b"),
                      fillOpacity: 1,
                      strokeColor: paintValue(paint["circle-stroke-color"], feature, "#ffffff"),
                      strokeWeight: Number(paint["circle-stroke-width"]) || 1.5
                    }
                  }),
                  feature: feature
                });
              }
            }
          });
        }
        var entry = { id: spec.id, spec: spec, source: source, overlays: overlays, listeners: [], visible: visible };
        this.layers.set(spec.id, entry);
        entry.overlays.forEach(function(item) {
          if (item.overlay && typeof item.overlay.setMap === "function") item.overlay.setMap(visible ? this.map : null);
        }, this);
        var self = this;
        (this.layerHandlers.get(this.layerHandlerKey("click", spec.id)) || []).forEach(function(handler) {
          self.attachLayerHandler(entry, "click", handler);
        });
        return entry;
      }
      setLayoutProperty(id, property, value) {
        var layer = this.layers.get(id);
        if (!layer || property !== "visibility") return this;
        layer.visible = value !== "none";
        layer.overlays.forEach(function(item) {
          if (item.overlay && typeof item.overlay.setMap === "function") item.overlay.setMap(layer.visible ? this.map : null);
        }, this);
        return this;
      }
      setPaintProperty(id, property, value) {
        var layer = this.layers.get(id);
        if (!layer) return this;
        if (property === "raster-opacity") {
          layer.overlays.forEach(function(item) {
            if (item.overlay && typeof item.overlay.setOpacity === "function") item.overlay.setOpacity(Math.max(0, Math.min(1, Number(value))));
          });
        }
        return this;
      }
      moveLayer() {
        // Google Maps uses overlay zIndex/insertion order rather than MapLibre layer order.
        return this;
      }
      fitBounds(bounds, options) {
        if (!Array.isArray(bounds) || bounds.length < 2) return;
        var west = Number(bounds[0][0]), south = Number(bounds[0][1]);
        var east = Number(bounds[1][0]), north = Number(bounds[1][1]);
        if (![west, south, east, north].every(Number.isFinite)) return;
        this.map.fitBounds(new google.maps.LatLngBounds({ south: south, west: west, north: north, east: east }), options && options.padding !== undefined ? options.padding : 48);
        var maxZoom = options && Number(options.maxZoom);
        if (Number.isFinite(maxZoom) && maxZoom > 0) {
          var self = this;
          google.maps.event.addListenerOnce(this.map, "idle", function() {
            var zoom = self.map.getZoom();
            if (Number.isFinite(zoom) && zoom > maxZoom) self.map.setZoom(maxZoom);
          });
        }
      }
      getCenter() { return this.map.getCenter(); }
      getZoom() { return this.map.getZoom() || 2; }
      getBearing() { return this.map.getHeading() || 0; }
      getPitch() { return this.map.getTilt() || 0; }
      loaded() { return this.isLoaded; }
      isStyleLoaded() { return this.isLoaded; }
      jumpTo(options) {
        options = options || {};
        var center = options.center;
        if (Array.isArray(center)) center = { lat: Number(center[1]), lng: Number(center[0]) };
        var current = this.map.getCenter();
        var changed = false;
        if (center && current) {
          var lat = typeof center.lat === "function" ? center.lat() : Number(center.lat);
          var lng = typeof center.lng === "function" ? center.lng() : Number(center.lng);
          if (Number.isFinite(lat) && Number.isFinite(lng) && (Math.abs(current.lat() - lat) > 1e-6 || Math.abs(current.lng() - lng) > 1e-6)) changed = true;
          if (changed) options.center = { lat: lat, lng: lng };
        }
        if (Number.isFinite(Number(options.zoom)) && Math.abs((this.map.getZoom() || 0) - Number(options.zoom)) > 1e-3) changed = true;
        if (changed) {
          var settings = {};
          if (options.center) settings.center = options.center;
          if (Number.isFinite(Number(options.zoom))) settings.zoom = Number(options.zoom);
          this.map.setOptions(settings);
        }
      }
      resize() {
        var center = this.map.getCenter();
        google.maps.event.trigger(this.map, "resize");
        if (center) this.map.setCenter(center);
      }
    }
    class GoogleMapsPopupCompatibilityAdapter {
      constructor(options) {
        this.options = options || {};
        this.infoWindow = new google.maps.InfoWindow(
          this.options.maxWidth ? { maxWidth: Number(this.options.maxWidth) } : {}
        );
        this.position = null;
      }
      setLngLat(position) {
        var lat = Array.isArray(position) ? Number(position[1])
          : (position && typeof position.lat === "function" ? position.lat() : Number(position && position.lat));
        var lng = Array.isArray(position) ? Number(position[0])
          : (position && typeof position.lng === "function" ? position.lng() : Number(position && position.lng));
        if (Number.isFinite(lat) && Number.isFinite(lng)) this.position = { lat: lat, lng: lng };
        return this;
      }
      setHTML(html) {
        this.infoWindow.setContent(String(html == null ? "" : html));
        return this;
      }
      addTo(mapAdapter) {
        var targetMap = mapAdapter && mapAdapter.map ? mapAdapter.map : mapAdapter;
        if (targetMap && this.position) {
          this.infoWindow.setPosition(this.position);
          this.infoWindow.open({ map: targetMap });
        }
        return this;
      }
      remove() {
        this.infoWindow.close();
        return this;
      }
    }
    resolve({ Map: GoogleMapCompatibilityAdapter, NavigationControl: function() {}, Popup: GoogleMapsPopupCompatibilityAdapter });
  };
  script.onerror = function() {
    unavailable(window.SARGUARDIAN_GOOGLE_MAPS_ERROR
      || "Google Maps script request failed. Check network access, CSP, Maps JavaScript API enablement, API-key restrictions, and allowed website referrers.");
  };
  document.head.appendChild(script);
});
</script>"""



def _apply_google_maps_dashboard(html: str, google_maps_api_key: str) -> str:
    """Use the AOI workspace's Google Maps API while retaining georeferenced science overlays."""
    static_import = "import * as maplibregl from 'https://unpkg.com/maplibre-gl@6.16.0/dist/maplibre-gl.mjs';"
    dynamic_import = re.compile(
        r"let maplibregl = null;\s*try\s*\{\s*"
        r"maplibregl = await import\('https://unpkg\.com/maplibre-gl@6\.16\.0/dist/maplibre-gl\.mjs'\);\s*"
        r"\}\s*catch\s*\(error\)\s*\{\s*"
        r"console\.warn\('SARGuardian map library unavailable; science panels remain available\.', error\);\s*\}\s*"
    )
    adapter_import = "const maplibregl = await window.SARGUARDIAN_GOOGLE_MAPS_ADAPTER;"

    if static_import in html:
        html = html.replace(static_import, adapter_import, 1)
    else:
        html, replaced = dynamic_import.subn(adapter_import + "\n", html, count=1)
        if replaced != 1:
            raise ValueError("Interactive dashboard does not match the supported Google Maps migration contract.")

    html = html.replace('<link rel="stylesheet" href="https://unpkg.com/maplibre-gl@6.16.0/dist/maplibre-gl.css">', "")
    html = html.replace("https://tile.openstreetmap.org/{z}/{x}/{y}.png", "")
    html = html.replace("© OpenStreetMap contributors.", "Google Maps basemap.")
    html = html.replace(
        "The processing report and georeferenced assets remain in this bundle; the basemap/MapLibre library requires an internet connection.",
        "Google Maps could not load. Check API access and allowed referrers; the processing report and georeferenced assets remain available.",
    )
    html = html.replace(
        "Basemap tiles are blocked or unavailable. The georeferenced AOI and scientific raster overlays remain available on the map canvas.",
        "Google Maps is unavailable. The georeferenced AOI and scientific raster overlays remain available on the map canvas.",
    )
    html = html.replace('content="strict-origin-when-cross-origin"', 'content="origin"')
    html = html.replace(
        "script-src 'unsafe-inline' https://unpkg.com;",
        "script-src 'unsafe-inline' 'unsafe-eval' https://unpkg.com https://*.googleapis.com https://*.gstatic.com *.google.com https://*.ggpht.com *.googleusercontent.com blob:;",
    )
    html = html.replace(
        "style-src 'unsafe-inline' https://unpkg.com;",
        "style-src 'unsafe-inline' https://unpkg.com https://fonts.googleapis.com https://*.googleapis.com https://*.gstatic.com;",
    )
    if "https://maps.googleapis.com/maps/api/js?key=" not in html:
        module_marker = '<script type="module">'
        if module_marker not in html:
            raise ValueError("Interactive dashboard has no JavaScript module entry point.")
        adapter_script = DASHBOARD_GOOGLE_MAPS_ADAPTER_SCRIPT.replace("__GOOGLE_MAPS_API_KEY__", google_maps_api_key)
        html = html.replace(module_marker, adapter_script + "\n" + module_marker, 1)
    # Surface a safe loader/CSP diagnostic in the dashboard instead of hiding it
    # behind the original static MapLibre fallback text.
    old_fallback = "function addFallback(){ $('mapFallback').classList.remove('hidden'); }"
    new_fallback = (
        "function addFallback(){var fallback=$('mapFallback');fallback.classList.remove('hidden');"
        "var detail=fallback.querySelector('p');"
        "if(detail&&window.SARGUARDIAN_GOOGLE_MAPS_ERROR)"
        "detail.textContent=window.SARGUARDIAN_GOOGLE_MAPS_ERROR;}"
    )
    html = html.replace(old_fallback, new_fallback, 1)
    return html


@app.get("/api/displacement-jobs/{job_id}/dashboard")
async def get_displacement_dashboard(
    job_id: UUID,
    google_maps_api_key: str | None = Header(default=None, alias="X-SARGuardian-Google-Maps-Key"),
    user: dict = Depends(get_current_user),
) -> Response:
    """Return the verified owner's interactive dashboard as self-contained HTML.

    The private Drive ZIP is verified against the signed callback inventory before
    reading its viewer assets. Only two exact in-archive files are read; no archive
    paths are extracted to disk and no Drive ID is disclosed to the browser.
    """
    try:
        record = _require_store().get_displacement_job(_required_owner_uid(user), job_id)
    except PersistenceError:
        raise HTTPException(status_code=503, detail="Could not read the requested interactive dashboard.") from None
    if not record:
        raise HTTPException(status_code=404, detail="Processing job was not found for this account.")
    if record.get("state") != JobState.SUCCEEDED.value:
        raise HTTPException(status_code=409, detail="The interactive dashboard is available only after processing succeeds.")

    result_summary = record.get("result_summary")
    dashboard_status = result_summary.get("web_dashboard") if isinstance(result_summary, dict) else None
    if not isinstance(dashboard_status, dict) or dashboard_status.get("status") != "PASS":
        raise HTTPException(status_code=404, detail="This result package does not include a validated interactive dashboard. Re-run processing after dashboard support is enabled.")

    artifacts = result_summary.get("artifacts") if isinstance(result_summary, dict) else None
    if not _valid_private_artifacts(artifacts):
        raise HTTPException(status_code=404, detail="A verified dashboard result bundle was not found.")
    matches = [item for item in artifacts if item.get("path") == "aoi-derived-results.zip"]
    if len(matches) != 1:
        raise HTTPException(status_code=404, detail="A unique verified dashboard result bundle was not found.")
    artifact = matches[0]
    if artifact["bytes"] > MAX_DASHBOARD_ARCHIVE_BYTES:
        raise HTTPException(status_code=413, detail="This dashboard bundle is too large to open in the interactive viewer. Download the result package instead.")
    if not isinstance(google_maps_api_key, str):
        raise HTTPException(
            status_code=503,
            detail="Google Maps is not configured. Check VITE_GOOGLE_MAPS_API_KEY in the frontend deployment.",
        )
    google_maps_api_key = google_maps_api_key.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,256}", google_maps_api_key):
        raise HTTPException(
            status_code=503,
            detail="Google Maps is not configured. Check VITE_GOOGLE_MAPS_API_KEY in the frontend deployment.",
        )
    try:
        received, data = download_private_result(artifact["drive_file_id"])
    except DriveUploadError as exc:
        # The Drive helper intentionally sanitizes provider failures. Log that
        # safe reason on the server so an operator can distinguish missing
        # runtime credentials from OAuth, permissions, and file retrieval errors.
        # Never log the Drive file ID, access token, refresh token, or client secret.
        logger.warning("Private dashboard Drive retrieval failed for job %s: %s", job_id, exc)
        raise HTTPException(status_code=503, detail="Private dashboard storage is unavailable. Retry in a moment.") from None
    if received.get("bytes") != artifact["bytes"] or len(data) != artifact["bytes"] or hashlib.sha256(data).hexdigest() != artifact["sha256"]:
        raise HTTPException(status_code=409, detail="Stored dashboard bundle no longer matches its verified checksum inventory.")

    try:
        with ZipFile(BytesIO(data), "r") as archive:
            names = archive.namelist()
            if len(names) > 20_000:
                raise HTTPException(status_code=413, detail="Dashboard bundle contains too many files.")
            for name in names:
                item_path = PurePosixPath(name)
                if name.startswith("/") or "\\" in name or ".." in item_path.parts or "\x00" in name:
                    raise HTTPException(status_code=409, detail="Dashboard bundle contains an unsafe archive path.")
            if names.count(DASHBOARD_VIEWER_PATH) != 1 or names.count(DASHBOARD_DATA_PATH) != 1:
                raise HTTPException(status_code=404, detail="The result package does not contain the interactive dashboard assets.")
            html_info = archive.getinfo(DASHBOARD_VIEWER_PATH)
            data_info = archive.getinfo(DASHBOARD_DATA_PATH)
            if html_info.file_size > MAX_DASHBOARD_HTML_BYTES or data_info.file_size > MAX_DASHBOARD_DATA_BYTES:
                raise HTTPException(status_code=413, detail="Interactive dashboard assets exceed the safe browser-viewing size.")
            html = archive.read(html_info).decode("utf-8")
            dashboard_data = archive.read(data_info).decode("utf-8")
    except HTTPException:
        raise
    except (BadZipFile, KeyError, UnicodeDecodeError, OSError):
        raise HTTPException(status_code=409, detail="Stored result bundle does not contain a readable dashboard.") from None

    data_marker = '<script src="./dashboard-data.js"></script>'
    if html.count(data_marker) != 1 or "</script" in dashboard_data.lower():
        raise HTTPException(status_code=409, detail="Dashboard viewer does not match the supported embedded-data contract.")
    html = html.replace(data_marker, "<script>\n" + dashboard_data + "\n</script>", 1)
    html = html.replace(
        '<a href="../index.html" style="color:var(--aqua);padding:9px">Processing report ↗</a>',
        '<span style="color:var(--aqua);padding:9px">Verified research dashboard</span>',
    )
    csp = "default-src 'self' https: data: blob:; script-src 'unsafe-inline' 'unsafe-eval' https://unpkg.com https://*.googleapis.com https://*.gstatic.com *.google.com https://*.ggpht.com *.googleusercontent.com blob:; style-src 'unsafe-inline' https://unpkg.com https://fonts.googleapis.com https://*.googleapis.com https://*.gstatic.com; img-src data: https://*.googleapis.com https://*.gstatic.com *.google.com *.googleusercontent.com https: blob:; connect-src https: data: blob:; frame-src *.google.com; worker-src blob:; font-src https://fonts.gstatic.com https://fonts.googleapis.com data:;"
    meta = '<meta http-equiv="Content-Security-Policy" content="' + csp + '">'
    if "<head>" not in html:
        raise HTTPException(status_code=409, detail="Dashboard viewer has no HTML head section.")
    html = html.replace("<head>", "<head>" + meta, 1)
    html = _apply_dashboard_presentation(html)
    html = _apply_google_maps_dashboard(html, google_maps_api_key)
    return Response(content=html, media_type="text/html", headers={
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "origin",
    })


@app.get("/api/displacement-jobs/{job_id}/artifacts/{artifact_index}")
async def download_displacement_artifact(job_id: UUID, artifact_index: int, user: dict = Depends(get_current_user)) -> Response:
    """Stream one private, verified Drive artifact to its authenticated owner."""
    if artifact_index < 0 or artifact_index > 10_000:
        raise HTTPException(status_code=404, detail="Result artifact was not found.")
    try:
        record = _require_store().get_displacement_job(str(user["uid"]), job_id)
    except PersistenceError:
        raise HTTPException(status_code=503, detail="Could not read the requested result artifact.") from None
    summary = record.get("result_summary") if record else None
    artifacts = summary.get("artifacts") if isinstance(summary, dict) else None
    if not _valid_private_artifacts(artifacts) or artifact_index >= len(artifacts):
        raise HTTPException(status_code=404, detail="Result artifact was not found.")
    artifact = artifacts[artifact_index]
    try:
        received, data = download_private_result(artifact["drive_file_id"])
    except DriveUploadError:
        raise HTTPException(status_code=503, detail="Private result storage is unavailable. Retry the download.") from None
    import hashlib
    if received["bytes"] != artifact["bytes"] or hashlib.sha256(data).hexdigest() != artifact["sha256"]:
        raise HTTPException(status_code=409, detail="Stored result artifact no longer matches the validated inventory.")
    filename = artifact["path"].rsplit("/", 1)[-1]
    return Response(content=data, media_type="application/octet-stream", headers={
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "private, no-store",
    })


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=get_settings().port)
