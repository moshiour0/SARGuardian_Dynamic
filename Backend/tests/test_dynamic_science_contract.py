from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from science_adapter import catalog
from science_adapter.geometry import processing_fingerprint, validate_wkt
from science_adapter.run_displacement import AdapterError, cleanup_after_verified_storage


WKT = "POLYGON((85 27, 85.01 27, 85 27.01, 85 27))"
NAME = "NISAR_L2_PR_GUNW_032_098_A_015_033_4000_SH_20261001T000000_20261001T000100_20261013T000000_20261013T000100_P05023_N_F_J_001"


def test_aoi_contract_requires_closure_topology_and_canonical_fingerprint() -> None:
    first = validate_wkt(WKT)
    reversed_ring = validate_wkt("POLYGON((85 27, 85 27.01, 85.01 27, 85 27))")
    assert first.sha256 == reversed_ring.sha256
    assert processing_fingerprint(aoi_wkt=WKT, start_date="2026-10-01", end_date="2026-10-22") == processing_fingerprint(
        aoi_wkt="POLYGON((85 27,85 27.01,85.01 27,85 27))", start_date="2026-10-01", end_date="2026-10-22")
    with pytest.raises(ValueError, match="closed"):
        validate_wkt("POLYGON((85 27, 85.01 27, 85 27.01))")
    with pytest.raises(ValueError, match="self-intersect"):
        validate_wkt("POLYGON((85 27, 85.01 27.01, 85.01 27, 85 27.01, 85 27))")


def test_catalog_identity_date_policy_and_variant_release_selection() -> None:
    identity = catalog.parse_identity(NAME)
    assert identity["reference"] == date(2026, 10, 1)
    assert identity["secondary"] == date(2026, 10, 13)
    assert identity["crid_maturity"] == "PROVISIONAL"
    assert catalog.in_window(identity, date(2026, 10, 2), date(2026, 10, 13), catalog.SECONDARY)
    assert not catalog.in_window(identity, date(2026, 10, 2), date(2026, 10, 13), catalog.BOTH)
    older = identity | {"granule": NAME.replace("P05023", "P05022"), "crid": "P05022", "maturity": "BETA"}
    newer = identity | {"maturity": "PROVISIONAL"}
    selected, excluded = catalog.deduplicate([older, newer])
    assert [item["granule"] for item in selected] == [NAME]
    assert excluded[0]["reason"] == "SUPERSEDED_PROCESSING_RELEASE"


def test_catalog_identity_supplies_the_pinned_downloader_contract() -> None:
    identity = catalog.parse_identity(NAME)
    # The upstream downloader logs this legacy ``path`` field before calling
    # ASF.  A dynamic record must carry it as well as the clearer ``track``.
    assert identity["path"] == identity["track"] == "098"


def test_catalog_rejects_incomplete_result_sets() -> None:
    with pytest.raises(catalog.CatalogError, match="cap"):
        catalog.normalize_payload([{}] * catalog.CATALOG_LIMIT)
    with pytest.raises(catalog.CatalogError, match="paginated"):
        catalog.normalize_payload({"results": [], "nextPageToken": "more"})


def test_cleanup_never_deletes_raw_without_complete_remote_receipts(tmp_path: Path) -> None:
    science_root = tmp_path / "science"
    raw = science_root / "data" / "nisar_l2" / "_incoming" / (NAME + ".h5")
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"raw-HDF5-bytes")
    work = tmp_path / "work"
    work.mkdir()
    output = work / "outputs" / "x" / "result.tif"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"derived")
    artifact = {"path": "outputs/x/result.tif", "sha256": hashlib.sha256(output.read_bytes()).hexdigest(), "bytes": output.stat().st_size}
    manifest = {"storage_verification": {"status": "PASS"}, "validated_outputs": [artifact],
                "cleanup": {"raw_files": [{"raw_path": str(raw), "sha256": hashlib.sha256(raw.read_bytes()).hexdigest(), "bytes": raw.stat().st_size}]}}
    (work / "result-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(AdapterError, match="remote verification"):
        cleanup_after_verified_storage(work, science_root)
    assert raw.exists()
    artifact.update({"drive_file_id": "abcdefghijk", "remote_verification": {"status": "PASS"}})
    (work / "result-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    completed = cleanup_after_verified_storage(work, science_root)
    assert not raw.exists()
    assert completed["cleanup"]["status"] == "PASS"
