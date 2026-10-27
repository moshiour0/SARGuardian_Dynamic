from __future__ import annotations

import hashlib
from io import BytesIO
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from uuid import uuid4

from app import main as main_module
from app.asf_catalog import ASFAdapterError, ASFSearchResult
from app.auth import get_current_user
from app.main import app
from app.persistence import PersistenceError

client = TestClient(app)


def setup_function() -> None:
    app.dependency_overrides.clear()


def teardown_function() -> None:
    app.dependency_overrides.clear()


def authenticated_client() -> TestClient:
    app.dependency_overrides[get_current_user] = lambda: {"uid": "test-user"}
    return client


def valid_payload() -> dict:
    return {
        "coordinates": [[85.3, 27.7], [85.301, 27.7], [85.301, 27.701], [85.3, 27.7]],
        "start_date": "2026-08-01",
        "end_date": "2026-08-15",
        "product_type": "GUNW",
    }


def test_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_catalog_requires_firebase_bearer_token() -> None:
    response = client.post("/api/catalog/search", json=valid_payload())
    assert response.status_code == 401
    assert "Bearer token" in response.json()["detail"]


def test_catalog_with_bearer_token_reports_missing_firebase_admin_config() -> None:
    response = client.post(
        "/api/catalog/search",
        headers={"Authorization": "Bearer test-token"},
        json=valid_payload(),
    )
    assert response.status_code == 503
    assert "not configured" in response.json()["detail"]


def test_catalog_validates_server_side_and_returns_honest_not_configured_result(monkeypatch) -> None:
    monkeypatch.setenv("ASF_CATALOG_ENABLED", "false")
    response = authenticated_client().post("/api/catalog/search", json=valid_payload())
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "not_configured"
    assert body["product_count"] == 0
    assert body["products"] == []
    assert body["data_downloaded"] is False
    assert body["scientific_analysis_performed"] is False
    assert body["processing_eligibility"]["status"] == "undetermined"
    assert body["query"]["product_type"] == "GUNW"
    assert body["query"]["aoi_area_km2"] > 0


def test_catalog_rejects_date_and_area_limits() -> None:
    client_instance = authenticated_client()
    too_long = valid_payload() | {"end_date": "2026-11-01"}
    assert client_instance.post("/api/catalog/search", json=too_long).status_code == 422

    too_large = valid_payload() | {
        "coordinates": [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]],
    }
    assert client_instance.post("/api/catalog/search", json=too_large).status_code == 422


def test_catalog_rejects_unsupported_product_type() -> None:
    response = authenticated_client().post("/api/catalog/search", json=valid_payload() | {"product_type": "OTHER"})
    assert response.status_code == 422


def test_catalog_response_contract_maps_ready_metadata_without_downloading(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "search_asf_catalog", lambda *args: ASFSearchResult(rows=[{
        "granuleName": "NISAR_L2_GUNW_TEST",
        "productType": "GUNW",
        "startTime": "2026-08-01T00:00:00Z",
        "stopTime": "2026-08-13T00:00:00Z",
        "sizeMB": {"file.h5": {"bytes": 2_000_000}},
    }], truncated=False, query_url="https://example.invalid"))
    monkeypatch.setattr(main_module, "normalize_product_rows", lambda rows, product_type: ([{
        "granule_name": "NISAR_L2_GUNW_TEST",
        "product_type": "GUNW",
        "observation_dates": ["2026-08-01", "2026-08-13"],
        "size_mb": 2,
    }], 0, []))
    class PreflightStore:
        def save_preflight(self, owner_uid, request_snapshot, manifest_snapshot):
            return uuid4()
    monkeypatch.setattr(main_module, "get_job_store", lambda: PreflightStore())
    response = authenticated_client().post("/api/catalog/search", json=valid_payload())
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["product_count"] == 1
    assert body["products"][0]["granule_name"] == "NISAR_L2_GUNW_TEST"
    assert body["scientific_analysis_performed"] is False
    assert body["processing_eligibility"]["status"] == "undetermined"


def test_catalog_both_family_combines_gunw_and_goff_without_creating_a_processing_preflight(monkeypatch) -> None:
    captured = {}

    def search(*args):
        captured["product_types"] = args[-1]
        return ASFSearchResult(rows=[
            {"granuleName": "NISAR_L2_GUNW_TEST", "productType": "GUNW", "startTime": "2026-08-01T00:00:00Z"},
            {"granuleName": "NISAR_L2_GOFF_TEST", "productType": "GOFF", "startTime": "2026-08-02T00:00:00Z"},
        ], truncated=False, query_url="https://example.invalid")

    monkeypatch.setattr(main_module, "search_asf_catalog", search)
    response = authenticated_client().post("/api/catalog/search", json=valid_payload() | {"product_type": "BOTH"})

    assert response.status_code == 200
    assert captured["product_types"] == "GUNW,GOFF"
    assert response.json()["query"]["product_type"] == "BOTH"
    assert {product["product_type"] for product in response.json()["products"]} == {"GUNW", "GOFF"}
    assert response.json()["preflight_id"] is None


def test_catalog_upstream_failure_is_not_reported_as_no_data(monkeypatch) -> None:
    def fail(*args):
        raise ASFAdapterError("ASF catalogue request timed out or could not be reached.")

    monkeypatch.setattr(main_module, "search_asf_catalog", fail)
    response = authenticated_client().post("/api/catalog/search", json=valid_payload())
    assert response.status_code == 502
    assert response.json()["status"] == "upstream_error"


def test_catalog_persistence_failure_is_reported_without_fabricating_a_preflight(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "search_asf_catalog", lambda *args: ASFSearchResult(rows=[{
        "granuleName": "NISAR_L2_GUNW_TEST",
        "productType": "GUNW",
        "startTime": "2026-08-01T00:00:00Z",
        "stopTime": "2026-08-13T00:00:00Z",
        "sizeMB": {"file.h5": {"bytes": 2_000_000}},
    }], truncated=False, query_url="https://example.invalid"))
    monkeypatch.setattr(main_module, "normalize_product_rows", lambda rows, product_type: ([{
        "granule_name": "NISAR_L2_GUNW_TEST",
        "product_type": "GUNW",
        "observation_dates": ["2026-08-01", "2026-08-13"],
        "size_mb": 2,
    }], 0, []))

    class FailingPreflightStore:
        def save_preflight(self, owner_uid, request_snapshot, manifest_snapshot):
            raise PersistenceError("database unavailable")

    monkeypatch.setattr(main_module, "get_job_store", lambda: FailingPreflightStore())
    response = authenticated_client().post("/api/catalog/search", json=valid_payload())
    assert response.status_code == 503
    assert response.json() == {
        "status": "persistence_error",
        "message": "The preflight could not be persisted for job confirmation.",
    }


def test_catalog_rejects_open_polygon_malformed_json_and_oversized_body() -> None:
    client_instance = authenticated_client()
    open_polygon = valid_payload() | {
        "coordinates": [[85.3, 27.7], [85.301, 27.7], [85.301, 27.701]],
    }
    assert client_instance.post("/api/catalog/search", json=open_polygon).status_code == 422
    malformed = client_instance.post("/api/catalog/search", content=b"{not-json", headers={"Content-Type": "application/json"})
    assert malformed.status_code == 422
    oversized = client_instance.post(
        "/api/catalog/search",
        content=b"{}",
        headers={"Content-Type": "application/json", "Content-Length": "1000001"},
    )
    assert oversized.status_code == 413


def test_unexpected_errors_use_a_safe_response() -> None:
    def explode() -> dict[str, str]:
        raise RuntimeError("internal test failure")

    app.dependency_overrides[get_current_user] = explode
    response = TestClient(app, raise_server_exceptions=False).post("/api/catalog/search", json=valid_payload())
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error."}


def test_production_requires_explicit_cors_origins(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import main as main_module

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    with pytest.raises(RuntimeError, match="CORS_ORIGINS"):
        main_module._cors_origins()


def _dashboard_zip() -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr(
            "web-dashboard/index.html",
            '<!doctype html><html><head><link rel="stylesheet" href="https://unpkg.com/maplibre-gl@6.16.0/dist/maplibre-gl.css"></head><body>'
            '<main><div class="cards" id="summaryCards"></div><div class="mapbox"><div id="map"></div></div></main>'
            '<script src="./dashboard-data.js"></script>'
            '<script type="module">import * as maplibregl from \'https://unpkg.com/maplibre-gl@6.16.0/dist/maplibre-gl.mjs\'; const tiles = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";</script>'
            '<a href="../index.html" style="color:var(--aqua);padding:9px">Processing report ↗</a></body></html>',
        )
        archive.writestr("web-dashboard/dashboard-data.js", "window.SARGUARDIAN_DASHBOARD={safe:true};")
    return buffer.getvalue()


def test_interactive_dashboard_is_returned_only_for_verified_owner_and_inlines_bundle(monkeypatch) -> None:
    from uuid import uuid4

    job_id = uuid4()
    payload = _dashboard_zip()
    artifact = {
        "path": "aoi-derived-results.zip",
        "kind": "aoi_derived_result_bundle",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
        "drive_file_id": "private-drive-file-id",
    }

    class Store:
        def get_displacement_job(self, owner_uid, requested_job_id):
            assert requested_job_id == job_id
            if owner_uid != "test-user":
                return None
            return {
                "id": str(job_id),
                "owner_uid": owner_uid,
                "state": "succeeded",
                "result_summary": {
                    "web_dashboard": {"status": "PASS", "schema_version": "sarguardian-web-dashboard/1.0"},
                    "artifacts": [artifact],
                },
            }

    monkeypatch.setattr(main_module, "get_job_store", lambda: Store())
    monkeypatch.setattr(
        main_module, "download_private_result",
        lambda drive_id: ({"bytes": len(payload), "id": drive_id}, payload),
    )

    # The result viewer keeps its native MapLibre/OpenStreetMap path. No Google Maps
    # key is necessary to open the owner-verified scientific dashboard.
    response = authenticated_client().get(f"/api/displacement-jobs/{job_id}/dashboard")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "window.SARGUARDIAN_DASHBOARD={safe:true};" in response.text
    assert 'src="./dashboard-data.js"' not in response.text
    assert "Content-Security-Policy" in response.text
    assert "https://unpkg.com/maplibre-gl@6.16.0/dist/maplibre-gl.mjs" in response.text
    assert "let maplibregl = null;" in response.text
    assert "maplibregl = await import(" in response.text
    assert "https://tile.openstreetmap.org/{z}/{x}/{y}.png" in response.text
    assert "maps.googleapis.com/maps/api/js?key=" not in response.text
    assert "SARGuardian responsive science dashboard" in response.text
    assert 'id="sg-dashboard-readiness"' in response.text
    assert "Not measured is not the same as zero motion." in response.text
    assert "private-drive-file-id" not in response.text
    assert "Processing report ↗" not in response.text


def test_interactive_dashboard_does_not_require_google_maps_key(monkeypatch) -> None:
    from uuid import uuid4

    job_id = uuid4()
    payload = _dashboard_zip()
    artifact = {
        "path": "aoi-derived-results.zip",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
        "drive_file_id": "private-drive-file-id",
    }

    class Store:
        def get_displacement_job(self, owner_uid, requested_job_id):
            return {
                "id": str(job_id),
                "owner_uid": owner_uid,
                "state": "succeeded",
                "result_summary": {
                    "web_dashboard": {"status": "PASS"},
                    "artifacts": [artifact],
                },
            }

    called = []
    monkeypatch.setattr(main_module, "get_job_store", lambda: Store())

    def download_verified_bundle(*_args):
        called.append(True)
        return {"bytes": len(payload)}, payload

    monkeypatch.setattr(main_module, "download_private_result", download_verified_bundle)
    response = authenticated_client().get(f"/api/displacement-jobs/{job_id}/dashboard")

    assert response.status_code == 200
    assert called == [True]
    assert "https://unpkg.com/maplibre-gl@6.16.0/dist/maplibre-gl.mjs" in response.text
    assert "maps.googleapis.com/maps/api/js?key=" not in response.text


def test_interactive_dashboard_rejects_unavailable_old_bundle_without_drive_download(monkeypatch) -> None:
    from uuid import uuid4

    job_id = uuid4()

    class Store:
        def get_displacement_job(self, owner_uid, requested_job_id):
            return {
                "id": str(job_id),
                "state": "succeeded",
                "result_summary": {
                    "web_dashboard": {"status": "NOT_AVAILABLE"},
                    "artifacts": [],
                },
            }

    called = []
    monkeypatch.setattr(main_module, "get_job_store", lambda: Store())
    monkeypatch.setattr(main_module, "download_private_result", lambda *_args: called.append(True))

    response = authenticated_client().get(f"/api/displacement-jobs/{job_id}/dashboard")

    assert response.status_code == 404
    assert called == []


def test_interactive_dashboard_rejects_drive_checksum_mismatch(monkeypatch) -> None:
    from uuid import uuid4

    job_id = uuid4()
    payload = _dashboard_zip()
    wrong_payload = b"not-the-verified-bundle"
    artifact = {
        "path": "aoi-derived-results.zip",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
        "drive_file_id": "private-drive-file-id",
    }

    class Store:
        def get_displacement_job(self, owner_uid, requested_job_id):
            return {
                "id": str(job_id),
                "state": "succeeded",
                "result_summary": {
                    "web_dashboard": {"status": "PASS"},
                    "artifacts": [artifact],
                },
            }

    monkeypatch.setattr(main_module, "get_job_store", lambda: Store())
    monkeypatch.setattr(
        main_module, "download_private_result",
        lambda _drive_id: ({"bytes": len(wrong_payload)}, wrong_payload),
    )

    response = authenticated_client().get(f"/api/displacement-jobs/{job_id}/dashboard")

    assert response.status_code == 409
    assert "checksum" in response.json()["detail"]


def test_interactive_dashboard_requires_authentication() -> None:
    response = client.get(f"/api/displacement-jobs/{uuid4()}/dashboard")
    assert response.status_code == 401
