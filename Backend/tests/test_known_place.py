from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app import known_place


AOI = "POLYGON ((85.50 28.25, 85.54 28.25, 85.54 28.29, 85.50 28.29, 85.50 28.25))"
REQUEST = {
    "aoi_wkt": AOI,
    "start_date": "2026-07-01",
    "end_date": "2026-07-20",
    "hazard_mode": "ice_rock_avalanche",
    "analysis_mode": "exploratory",
    "polarization": "HH",
    "track": "ANY",
    "direction": "A",
    "coherence_neighbours": "5",
    "execute_downloads": False,
}


def settings() -> SimpleNamespace:
    return SimpleNamespace(displacement_actions_enabled=True, github_actions_token="test-token")


def test_known_place_request_canonicalizes_wkt_and_defaults_to_no_raw_downloads() -> None:
    request = known_place.KnownPlaceAnalysisRequest(**REQUEST)
    assert request.aoi_wkt.startswith("POLYGON((")
    assert request.execute_downloads is False
    assert request.confirm_raw_downloads is False


def test_known_place_request_requires_explicit_consent_for_raw_downloads() -> None:
    with pytest.raises(ValidationError, match="EXPLICIT_RAW_DOWNLOAD_CONSENT_REQUIRED"):
        known_place.KnownPlaceAnalysisRequest(**(REQUEST | {"execute_downloads": True}))


def test_known_place_request_rejects_bad_track_and_invalid_polygon() -> None:
    with pytest.raises(ValidationError, match="TRACK_MUST_BE_ANY_OR_THREE_DIGITS"):
        known_place.KnownPlaceAnalysisRequest(**(REQUEST | {"track": "98"}))
    with pytest.raises(ValidationError, match="AOI_WKT_INVALID"):
        known_place.KnownPlaceAnalysisRequest(**(REQUEST | {"aoi_wkt": "POLYGON ((0 0, 1 0, 1 1))"}))


def test_dry_run_dispatch_does_not_include_raw_download_consent(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: dict = {}

    def fake_request(_settings, method, path, body=None):
        observed.update({"method": method, "path": path, "body": body})
        return 204, {}

    monkeypatch.setattr(known_place, "_github_request", fake_request)
    request = known_place.KnownPlaceAnalysisRequest(**REQUEST)
    response = known_place.dispatch_known_place_analysis(settings(), request)

    assert response.status == "dispatching"
    assert response.execute_downloads is False
    assert observed["method"] == "POST"
    assert observed["body"]["ref"] == "main"
    inputs = observed["body"]["inputs"]
    assert inputs["request_id"] == response.request_id
    assert inputs["execute_downloads"] is False
    assert inputs["consent_marker"] == ""


def test_raw_analysis_dispatch_needs_confirmed_checkbox_and_sets_exact_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: dict = {}

    def fake_request(_settings, _method, _path, body=None):
        observed.update(body or {})
        return 204, {}

    monkeypatch.setattr(known_place, "_github_request", fake_request)
    request = known_place.KnownPlaceAnalysisRequest(**(
        REQUEST | {"execute_downloads": True, "confirm_raw_downloads": True}
    ))
    response = known_place.dispatch_known_place_analysis(settings(), request)

    assert response.execute_downloads is True
    assert observed["inputs"]["execute_downloads"] is True
    assert observed["inputs"]["consent_marker"] == "[run-know-place]"


def test_status_is_matched_by_random_request_id_and_workflow_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    request_id = "c19a7630-9f19-4b38-ae0e-88d53ad7c8f2"

    def fake_request(_settings, method, path, body=None):
        assert method == "GET"
        assert "known-place-analysis.yml/runs" in path
        return 200, {
            "workflow_runs": [
                {
                    "id": 12345,
                    "display_title": f"Known Place Analysis {request_id}",
                    "path": ".github/workflows/known-place-analysis.yml",
                    "head_branch": "main",
                    "event": "workflow_dispatch",
                    "status": "completed",
                    "conclusion": "success",
                    "html_url": "https://github.com/moshiour0/SARGuardian_Dynamic/actions/runs/12345",
                    "created_at": "2026-10-28T00:00:00Z",
                    "updated_at": "2026-10-28T00:01:00Z",
                },
                {
                    "id": 12344,
                    "display_title": "Unrelated workflow",
                    "path": ".github/workflows/sarguardian-displacement.yml",
                    "head_branch": "main",
                    "event": "workflow_dispatch",
                    "status": "completed",
                    "conclusion": "success",
                },
            ]
        }

    monkeypatch.setattr(known_place, "_github_request", fake_request)
    response = known_place.get_known_place_analysis_status(settings(), request_id)
    assert response.status == "completed"
    assert response.github_run_id == "12345"
    assert response.conclusion == "success"
    assert response.github_run_url.endswith("/12345")


def test_status_with_unknown_request_id_remains_dispatching(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(known_place, "_github_request", lambda *_args, **_kwargs: (200, {"workflow_runs": []}))
    response = known_place.get_known_place_analysis_status(
        settings(), "c19a7630-9f19-4b38-ae0e-88d53ad7c8f2"
    )
    assert response.status == "dispatching"
    assert response.github_run_id is None
