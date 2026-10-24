from __future__ import annotations

import csv
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from validate_aoi_derived_outputs import canonical_granule_name, validate


def test_canonical_granule_name_accepts_reader_filename_with_h5_suffix():
    base = (
        "NISAR_L2_PR_GUNW_022_098_A_016_023_4000_SH_"
        "20260608T233922_20260608T233956_20260620T233921_"
        "20260620T233956_X05026_N_F_J_001"
    )

    assert canonical_granule_name(base) == base
    assert canonical_granule_name(base + ".h5") == base
    assert canonical_granule_name("/tmp/source/" + base + ".h5") == base
    assert canonical_granule_name(base + ".H5") == base


def test_canonical_granule_name_does_not_match_different_product():
    assert canonical_granule_name("NISAR_L2_PR_GUNW_PRODUCT_A.h5") != canonical_granule_name(
        "NISAR_L2_PR_GUNW_PRODUCT_B"
    )


def test_validator_matches_reader_csv_when_only_h5_suffix_differs(tmp_path):
    granule = "NISAR_L2_PR_GUNW_PRODUCT_SUFFIX_REGRESSION"
    stats_csv = tmp_path / "stats.csv"
    with stats_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["file", "layer"])
        writer.writeheader()
        writer.writerow({"file": granule + ".h5", "layer": "frequencyA"})

    report = validate(SimpleNamespace(
        stats_csv=stats_csv,
        granule_name=granule,
        product_type="GUNW",
        export_dir=tmp_path / "empty_exports",
        product_id="GUNW-TEST",
        aoi_name="custom",
    ))

    assert report["product_rows"] == 1
    assert "EXPECTED_ONE_READER_ROW_FOR_PRODUCT" not in {
        error["problem"] for error in report["errors"]
    }
