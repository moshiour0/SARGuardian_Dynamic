from __future__ import annotations

import hashlib
import json
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError

import pytest

from science_adapter.dynamic_callback import (
    CallbackError, callback_payload, discovery_summary, send_callback, success_summary,
)


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_discovery_summary_reports_eligible_and_selected_family_counts(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    write_json(manifest, {
        "baseline_product_counts": {"GUNW": 8, "GOFF": 5},
        "products": [{"product_type": "GUNW"}, {"product_type": "GOFF"}],
    })
    assert discovery_summary(manifest) == {
        "schema_version": 2,
        "catalog_granule_counts": {"GUNW": 8, "GOFF": 5},
        "selected_granule_counts": {"GUNW": 1, "GOFF": 1},
    }


def test_upstream_dashboard_and_drive_receipt_build_version_two_result(tmp_path: Path) -> None:
    dashboard = tmp_path / "dashboard"
    write_json(dashboard / "aoi-run-summary.json", {
        "status": "PASS", "products_expected": 2, "gunw_count": 1, "goff_count": 1,
        "science_commit": "10639a916c9829850775410b3ed7de62e527770d",
        "orchestration_sha": "a" * 40, "aoi_wkt_sha256": "b" * 64,
        "science_diagnostics": {"pixel_stack": {"status": "PASS"}},
        "web_dashboard": {
            "schema_version": "sarguardian-web-dashboard/1.0",
            "status": "PASS", "viewer": "web-dashboard/index.html",
            "observations": 2, "layers": 4,
        },
    })
    write_json(dashboard / "web-dashboard" / "manifest.json", {
        "schema_version": "sarguardian-web-dashboard/1.0",
        "validation": {"visual_assets_status": "PASS"},
    })
    (dashboard / "web-dashboard" / "index.html").write_text("<html>viewer</html>", encoding="utf-8")
    (dashboard / "web-dashboard" / "dashboard-data.js").write_text("window.DATA={};", encoding="utf-8")
    products = [
        {"product_type": "GUNW", "granule_name": "NISAR_GUNW", "status": "PASS"},
        {"product_type": "GOFF", "granule_name": "NISAR_GOFF", "status": "PASS"},
    ]
    write_json(dashboard / "aoi-processing-report.json", {
        "products": products,
        "processing_report": {
            "status": "PASS", "aoi_wkt_sha256": "b" * 64,
            "science_commit": "10639a916c9829850775410b3ed7de62e527770d",
        },
    })
    write_json(tmp_path / "manifest" / "manifest.json", {
        "aoi_wkt_sha256": "b" * 64,
        "science_commit": "10639a916c9829850775410b3ed7de62e527770d",
        "orchestration_sha": "a" * 40,
        "baseline_product_counts": {"GUNW": 8, "GOFF": 5},
        "products": products,
    })
    content = b"verified bundle"
    write_json(tmp_path / "storage" / "drive-dashboard-upload-receipt.json", {
        "remote_verification": "PASS", "remote_folder_id": "private-folder-id",
        "files": [
            {"id": f"private-file-id-{index}", "name": name,
             "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
            for index, name in enumerate(("aoi-derived-results.zip", "aoi-run-summary.json", "products.csv"))
        ],
    })

    result = success_summary(tmp_path, "12345")

    assert result["schema_version"] == 2
    assert result["validation"]["status"] == "PASS"
    assert result["storage_verification"]["status"] == "PASS"
    assert result["products"] == products
    assert result["artifacts"][0]["drive_file_id"] == "private-file-id-0"
    assert result["web_dashboard"] == {
        "status": "PASS", "schema_version": "sarguardian-web-dashboard/1.0",
        "viewer": "web-dashboard/index.html", "observations": 2, "layers": 4,
    }
    assert result["catalog_granule_counts"] == {"GUNW": 8, "GOFF": 5}
    assert result["selected_granule_counts"] == {"GUNW": 1, "GOFF": 1}
    assert result["provenance"]["github_run_id"] == "12345"


def test_callback_payload_is_deterministic_and_bound_to_the_job() -> None:
    payload = callback_payload(
        state="running", stage="products_discovered", sequence=2, github_run_id="123",
        github_run_url="https://github.test/run/123", job_id="11111111-1111-1111-1111-111111111111",
        request_fingerprint="a" * 64, result_summary={"schema_version": 2},
    )
    decoded = json.loads(payload)
    assert decoded["contract_version"] == 2
    assert decoded["job_id"] == "11111111-1111-1111-1111-111111111111"
    assert decoded["result_summary"] == {"schema_version": 2}



def test_send_callback_retries_transient_http_503_with_same_signed_payload(monkeypatch) -> None:
    captured = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_urlopen(request, timeout):
        headers = {name.lower(): value for name, value in request.header_items()}
        captured.append({
            "url": request.full_url,
            "method": request.get_method(),
            "body": request.data,
            "signature": headers.get("x-sarguardian-signature"),
            "timeout": timeout,
        })
        if len(captured) == 1:
            raise HTTPError(
                request.full_url, 503, "Service Unavailable", hdrs=None,
                fp=BytesIO(b"temporary service error"),
            )
        return Response()

    monkeypatch.setattr("science_adapter.dynamic_callback.urlopen", fake_urlopen)
    monkeypatch.setattr("science_adapter.dynamic_callback.time.sleep", lambda _delay: None)

    send_callback(
        "https://api.example.test", "callback-secret",
        "11111111-1111-1111-1111-111111111111", b'{"state":"running"}',
    )

    assert len(captured) == 2
    assert captured[0] == captured[1]
    assert captured[0]["method"] == "POST"
    assert captured[0]["timeout"] == 30
    assert captured[0]["signature"] and captured[0]["signature"].startswith("sha256=")


def test_send_callback_does_not_retry_a_nontransient_contract_conflict(monkeypatch) -> None:
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        raise HTTPError(
            request.full_url, 409, "Conflict", hdrs=None, fp=BytesIO(b"sequence conflict"),
        )

    monkeypatch.setattr("science_adapter.dynamic_callback.urlopen", fake_urlopen)
    monkeypatch.setattr("science_adapter.dynamic_callback.time.sleep", lambda _delay: None)

    with pytest.raises(CallbackError, match="Could not deliver the signed worker callback"):
        send_callback(
            "https://api.example.test", "callback-secret",
            "11111111-1111-1111-1111-111111111111", b'{"state":"running"}',
        )

    assert len(calls) == 1
