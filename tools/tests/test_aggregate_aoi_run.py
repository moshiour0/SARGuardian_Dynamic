from pathlib import Path

from tools.aggregate_aoi_run import assess_maturity_consistency, matching_product_stats_rows


def test_matching_catalog_and_reader_maturity_is_passable():
    state, warnings, conflict = assess_maturity_consistency("PROVISIONAL", "PROVISIONAL")
    assert state == "MATCH"
    assert warnings == []
    assert not conflict


def test_unmapped_local_crid_defers_to_asf_but_remains_visible_warning():
    state, warnings, conflict = assess_maturity_consistency("PROVISIONAL", "UNKNOWN")
    assert state == "CATALOG_CLASSIFIED_LOCAL_CRID_MAP_UNKNOWN"
    assert len(warnings) == 1
    assert "local CRID classifier reports UNKNOWN" in warnings[0]
    assert not conflict


def test_known_reader_catalog_maturity_disagreement_is_hard_conflict():
    state, warnings, conflict = assess_maturity_consistency("PROVISIONAL", "BETA")
    assert state == "CONFLICT"
    assert warnings == []
    assert conflict


def test_missing_reader_maturity_never_silently_counts_as_matched():
    state, warnings, conflict = assess_maturity_consistency("PROVISIONAL", "")
    assert state == "READER_MATURITY_NOT_REPORTED"
    assert warnings
    assert not conflict


def test_dashboard_chart_groups_metric_by_family_and_exact_column():
    source = (Path(__file__).resolve().parents[1] / "aggregate_aoi_run.py").read_text(encoding="utf-8")
    assert 'id="metricSeries"' in source
    assert "function seriesKey(p)" in source
    assert "vals=all.filter(function(p){return seriesKey(p)===key})" in source
    assert "Maturity (ASF / reader)" in source
    assert '"science_maturity","maturity_validation"' in source


def test_gunw_stats_row_matches_manifest_name_without_h5_suffix():
    rows = [{"file": "NISAR_L2_PR_GUNW_EXAMPLE.h5", "layer": ""}]

    assert matching_product_stats_rows(rows, "NISAR_L2_PR_GUNW_EXAMPLE", "GUNW") == rows


def test_goff_stats_match_granule_without_suffix_and_keep_only_layer2():
    rows = [
        {"file": "NISAR_L2_PR_GOFF_EXAMPLE.h5", "layer": "HH/layer1"},
        {"file": "NISAR_L2_PR_GOFF_EXAMPLE.h5", "layer": "HH/layer2"},
        {"file": "NISAR_L2_PR_GOFF_OTHER.h5", "layer": "HH/layer2"},
    ]

    assert matching_product_stats_rows(rows, "NISAR_L2_PR_GOFF_EXAMPLE", "GOFF") == [rows[1]]
