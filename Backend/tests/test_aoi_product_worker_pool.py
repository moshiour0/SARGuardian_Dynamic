"""Contract tests for safe bounded AOI product-worker concurrency."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "sarguardian-aoi-wkt-dashboard.yml"
WORKER_PATH = ROOT / ".github" / "workflows" / "sarguardian-aoi-product-worker.yml"
BUILDER_SHA = "7239cb3ec3a0f9a19edb89b0dae79cc2c25d2e44"


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
    assert "10639a916c9829850775410b3ed7de62e527770d" in workflow
