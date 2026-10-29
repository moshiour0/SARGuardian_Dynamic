"""Contract tests for safe bounded AOI product-worker concurrency."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from prepare_aoi_drive_folders import prepare_drive_folders  # noqa: E402

WORKFLOW_PATH = ROOT / ".github" / "workflows" / "sarguardian-aoi-wkt-dashboard.yml"
WORKER_PATH = ROOT / ".github" / "workflows" / "sarguardian-aoi-product-worker.yml"
BUILDER_SHA = "7239cb3ec3a0f9a19edb89b0dae79cc2c25d2e44"
SCIENCE_ENGINE_SHA = "10639a916c9829850775410b3ed7de62e527770d"


def _job_block(workflow: str, job_id: str) -> str:
    match = re.search(
        rf"(?ms)^  {re.escape(job_id)}:\n(.*?)(?=^  [A-Za-z0-9_-]+:\s*$|\Z)",
        workflow,
    )
    assert match is not None, f"workflow job missing: {job_id}"
    return match.group(1)


def test_initial_and_retry_product_pools_are_bounded_to_fifteen_workers() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    for job_id in ("process-products", "retry-products"):
        block = _job_block(workflow, job_id)
        assert re.search(r"(?m)^\s{6}max-parallel:\s*15\s*$", block), job_id
        assert "uses: ./.github/workflows/sarguardian-aoi-product-worker.yml" in block


def test_drive_folders_are_prepared_before_the_parallel_matrix_starts() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    preflight = workflow.index("Prepare shared Drive folder hierarchy before parallel product workers")
    publish_manifest = workflow.index("Publish the selected-product manifest")
    worker_matrix = workflow.index("  process-products:")
    assert preflight < publish_manifest < worker_matrix
    assert "python tools/prepare_aoi_drive_folders.py" in workflow
    assert "DRIVE_VERSION_TAG: ${{ github.run_id }}-${{ github.run_attempt }}" in workflow
    assert "GOOGLE_DRIVE_PARENT_FOLDER_ID: ${{ secrets.GOOGLE_DRIVE_PARENT_FOLDER_ID || secrets.GOOGLE_DRIVE_FOLDER_ID }}" in workflow


class FakeDrive:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.folder_ids: dict[tuple[str, str], str] = {}

    def ensure_folder(self, parent: str, name: str) -> str:
        key = (parent, name)
        self.calls.append(key)
        if key not in self.folder_ids:
            self.folder_ids[key] = f"folder-{len(self.folder_ids) + 1}"
        return self.folder_ids[key]


def _manifest(products: list[dict]) -> dict:
    return {"schema_version": "sarguardian-aoi-run-manifest/1.0", "products": products}


def _product(product_type: str, granule: str, product_id: str, maturity: str = "PROVISIONAL") -> dict:
    return {
        "id": product_id,
        "product_type": product_type,
        "maturity": maturity,
        "granule_name": granule,
    }


def test_drive_folder_preflight_serializes_shared_ancestors_and_unique_product_paths() -> None:
    products = [
        _product("GUNW", "NISAR_L2_PR_GUNW_PAIR_A", "GUNW-001"),
        _product("GUNW", "NISAR_L2_PR_GUNW_PAIR_B", "GUNW-002"),
        _product("GOFF", "NISAR_L2_PR_GOFF_PAIR_A", "GOFF-001"),
    ]
    drive = FakeDrive()

    count = prepare_drive_folders(
        _manifest(products), drive, "parent-root", "job-123", "40792194644-1"
    )

    assert count == 3
    assert len(drive.calls) == len(set(drive.calls))
    assert sum(name == "job-123" for _parent, name in drive.calls) == 1
    assert sum(name == "Products" for _parent, name in drive.calls) == 1
    assert sum(name == "GUNW" for _parent, name in drive.calls) == 1
    assert sum(name == "GOFF" for _parent, name in drive.calls) == 1
    assert all(name == "40792194644-1" for _parent, name in drive.calls if name == "40792194644-1")


def test_duplicate_manifest_product_is_rejected_before_any_drive_folder_mutation() -> None:
    duplicate = _product("GUNW", "NISAR_L2_PR_GUNW_PAIR_A", "GUNW-001")
    drive = FakeDrive()

    with pytest.raises(ValueError, match="DUPLICATE_OR_INCOMPLETE_PRODUCT"):
        prepare_drive_folders(
            _manifest([duplicate, duplicate]), drive, "parent-root", "job-123", "40792194644-1"
        )

    assert drive.calls == []


def test_one_product_per_runner_and_cleanup_stays_gated_on_verified_retention() -> None:
    worker = WORKER_PATH.read_text(encoding="utf-8")
    assert "Download exactly one raw NISAR granule" in worker
    assert "Delete local raw/archive only after successful remote retention" in worker
    assert "ERROR=REFUSING_RAW_DELETION_WITHOUT_REMOTE_VERIFICATION" in worker
    assert "REMOTE_DERIVED_OUTPUTS_AND_RETENTION_POLICY_VERIFIED=PASS" in worker
    assert "REMOTE_RETENTION_FOLDER_ID_MISMATCH" in worker
    assert "runner_raw_hdf5_deleted" in worker


def test_reviewed_dashboard_builder_pin_and_exact_verification_remain() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert workflow.count(BUILDER_SHA) == 2
    assert 'test "$actual" = "$expected"' in workflow
    assert SCIENCE_ENGINE_SHA in workflow
