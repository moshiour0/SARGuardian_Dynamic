"""Contract tests for safe bounded AOI product-worker concurrency."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from prepare_aoi_drive_folders import prepare_drive_folders  # noqa: E402
from write_no_valid_pixel_stats import write_no_valid_pixel_stats  # noqa: E402

WORKFLOW_PATH = ROOT / ".github" / "workflows" / "sarguardian-aoi-wkt-dashboard.yml"
WORKER_PATH = ROOT / ".github" / "workflows" / "sarguardian-aoi-product-worker.yml"
BUILDER_SHA = "ecd125f68ed61c276406f73a1d77eb27979e204e"
SCIENCE_ENGINE_SHA = "f038e4dfdb53c8d9793aaaefe173570a1032026c"


def _job_block(workflow: str, job_id: str) -> str:
    match = re.search(
        rf"(?ms)^  {re.escape(job_id)}:\n(.*?)(?=^  [A-Za-z0-9_-]+:\s*$|\Z)",
        workflow,
    )
    assert match is not None, f"workflow job missing: {job_id}"
    return match.group(1)


def test_initial_product_pool_is_bounded_and_automatic_redownload_is_disabled() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    block = _job_block(workflow, "process-products")
    assert re.search(r"(?m)^\s{6}max-parallel:\s*15\s*$", block), "process-products"
    assert "uses: ./.github/workflows/sarguardian-aoi-product-worker.yml" in block
    assert "  retry-plan:\n" in workflow
    assert "sarguardian-aoi-retry-plan" in workflow
    assert "  retry-products:\n" not in workflow
    assert "needs: [manifest, process-products, retry-plan]" in workflow
    assert "needs: [manifest, process-products, retry-plan]" in workflow


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
    # Product-specific folders are unique, so workers create these themselves;
    # preflight is intentionally limited to shared ancestors to avoid excess API calls.
    assert not any(name.startswith("NISAR_L2_") for _parent, name in drive.calls)
    assert not any(name == "Runs" for _parent, name in drive.calls)
    assert not any(name == "40792194644-1" for _parent, name in drive.calls)


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
    assert workflow.count(BUILDER_SHA) == 4
    assert workflow.count('test "$actual" = "$expected"') == 2
    assert SCIENCE_ENGINE_SHA in workflow


def test_manifest_stage_checks_out_the_pinned_retention_dependencies_before_install() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    checkout = workflow.index("Checkout pinned dashboard retention dependencies for the manifest stage")
    verification = workflow.index("Verify manifest-stage retention dependency revision")
    install = workflow.index("Install pinned discovery and processing dependencies")
    manifest_job = _job_block(workflow, "manifest")
    assert checkout < verification < install
    assert "repository: moshiour0/SARGuardian-backend" in manifest_job
    assert "ref: " + BUILDER_SHA in manifest_job
    assert "path: dashboard-source" in manifest_job
    assert 'git -C dashboard-source rev-parse HEAD' in manifest_job
    assert 'test "$actual" = "$expected"' in manifest_job



def test_worker_captures_reader_log_and_fails_closed_for_no_valid_pixel_diagnostic() -> None:
    worker = WORKER_PATH.read_text(encoding="utf-8")
    start = worker.index("Run the original single-product reader with dynamic WKT-AOI settings")
    end = worker.index("Measure pair-local noise floor at the selected in-AOI target", start)
    reader = worker[start:end]
    assert 'reader_log="$CURATED_DIR/derived/science-reader.log"' in reader
    assert '2>&1 | tee "$reader_log"' in reader
    assert 'if [[ ! -s "$stats" ]]' in reader
    assert "tools/write_no_valid_pixel_stats.py" in reader
    assert '--reader-log "$reader_log"' in reader
    assert '--raw-product "$RAW_PATH" --product-type "$PRODUCT_TYPE"' in reader
    assert reader.index("tools/write_no_valid_pixel_stats.py") < reader.index('test -s "$stats"')


def test_no_valid_pixels_diagnostic_row_contains_no_fabricated_measurement(tmp_path: Path) -> None:
    raw = tmp_path / "NISAR_L2_PR_GOFF_EXAMPLE.h5"
    raw.write_bytes(b"identity-only test fixture")
    log = tmp_path / "reader.log"
    log.write_text(
        "=== product ===\n"
        "--- HH/layer2 ---\nfinite 1000\ninside AOI 0\nNO VALID PIXELS\n"
        "--- VV/layer2 ---\nfinite 2000\ninside AOI 0\nNO VALID PIXELS\n",
        encoding="utf-8",
    )
    out = tmp_path / "derived" / "stats.csv"
    result = write_no_valid_pixel_stats(log, out, raw, "GOFF")

    import csv
    with out.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert result["status"] == "NO_VALID_PIXELS_AFTER_AOI_AND_QUALITY_GATES"
    assert result["valid_pixels"] == 0
    assert result["measurement_reported"] is False
    assert len(rows) == 1
    assert rows[0]["file"] == raw.name
    assert rows[0]["layer"] == "layer2 (no valid pixels)"
    assert rows[0]["valid_px"] == "0"
    assert rows[0]["maturity"] == "UNKNOWN"
    assert rows[0]["science_status"] == "NO_VALID_PIXELS_AFTER_AOI_AND_QUALITY_GATES"
    assert "median" not in ",".join(rows[0].keys()).lower()
    assert "velocity" not in ",".join(rows[0].keys()).lower()


def test_no_valid_pixels_helper_rejects_mixed_or_ambiguous_reader_layers(tmp_path: Path) -> None:
    raw = tmp_path / "NISAR_L2_PR_GOFF_EXAMPLE.h5"
    raw.write_bytes(b"test")
    log = tmp_path / "reader.log"
    log.write_text(
        "--- HH/layer2 ---\nNO VALID PIXELS\n"
        "--- VV/layer2 ---\nrange offset median 23 mm\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="NO_VALID_PIXEL_MARKER_NOT_CONFIRMED"):
        write_no_valid_pixel_stats(log, tmp_path / "stats.csv", raw, "GOFF")
    assert not (tmp_path / "stats.csv").exists()


def test_no_valid_pixels_helper_refuses_to_overwrite_existing_reader_stats(tmp_path: Path) -> None:
    raw = tmp_path / "NISAR_L2_PR_GOFF_EXAMPLE.h5"
    raw.write_bytes(b"test")
    log = tmp_path / "reader.log"
    log.write_text("--- HH/layer2 ---\nNO VALID PIXELS\n", encoding="utf-8")
    out = tmp_path / "stats.csv"
    out.write_text("file,layer\nexisting,layer2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="REFUSING_TO_REPLACE_EXISTING_READER_STATS"):
        write_no_valid_pixel_stats(log, out, raw, "GOFF")
    assert out.read_text(encoding="utf-8").startswith("file,layer")


def test_no_valid_pixels_helper_supports_gunw_without_fabricated_metrics(tmp_path: Path) -> None:
    raw = tmp_path / "NISAR_L2_PR_GUNW_EXAMPLE.h5"
    raw.write_bytes(b"test")
    log = tmp_path / "reader.log"
    log.write_text("--- unwrappedInterferogram/HH ---\nNO VALID PIXELS\n", encoding="utf-8")
    out = tmp_path / "gunw-stats.csv"
    result = write_no_valid_pixel_stats(log, out, raw, "GUNW")
    assert result["valid_pixels"] == 0
    assert "median" not in out.read_text(encoding="utf-8").lower()



def test_no_valid_pixels_helper_replaces_only_a_zero_byte_reader_csv(tmp_path: Path) -> None:
    raw = tmp_path / "NISAR_L2_PR_GOFF_EXAMPLE.h5"
    raw.write_bytes(b"test")
    log = tmp_path / "reader.log"
    log.write_text("--- HH/layer2 ---\nNO VALID PIXELS\n", encoding="utf-8")
    out = tmp_path / "stats.csv"
    out.touch()

    result = write_no_valid_pixel_stats(log, out, raw, "GOFF")

    assert result["measurement_reported"] is False
    assert out.stat().st_size > 0
    assert "NO_VALID_PIXELS_AFTER_AOI_AND_QUALITY_GATES" in out.read_text(encoding="utf-8")
