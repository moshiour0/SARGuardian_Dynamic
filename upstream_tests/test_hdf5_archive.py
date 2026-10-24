from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import h5py
import numpy as np
import pytest

TOOL = Path(__file__).resolve().parents[1] / "tools" / "sarguardian_hdf5_archive.py"
spec = importlib.util.spec_from_file_location("sarguardian_hdf5_archive", TOOL)
archive_tool = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(archive_tool)


def _add_dataset(group, name, value, attrs=None):
    ds = group.create_dataset(name, data=value)
    for k, v in (attrs or {}).items():
        ds.attrs[k] = v
    return ds


def make_goff(path: Path):
    rng = np.arange(42, dtype=np.float32).reshape(6, 7) / 100.0
    with h5py.File(path, "w") as f:
        f.attrs["productVersion"] = "test-1"
        ident = f.require_group("science/LSAR/identification")
        _add_dataset(ident, "lookDirection", np.bytes_("Right"))
        po = f.require_group("science/LSAR/GOFF/grids/frequencyA/pixelOffsets")
        for pol in ("HH", "VV"):
            for layer in ("layer1", "layer2", "layer3"):
                g = po.require_group(f"{pol}/{layer}")
                g.attrs["windowSize"] = layer
                _add_dataset(g, "slantRangeOffset", rng + (0 if pol == "HH" else 1), {"units": "meters"})
                _add_dataset(g, "alongTrackOffset", rng + 2, {"units": "meters"})
                _add_dataset(g, "correlationSurfacePeak", np.full(rng.shape, .8, dtype=np.float32))
                _add_dataset(g, "snr", np.full(rng.shape, 5, dtype=np.float32))
                _add_dataset(g, "slantRangeOffsetVariance", np.full(rng.shape, .01, dtype=np.float32))
                _add_dataset(g, "alongTrackOffsetVariance", np.full(rng.shape, .02, dtype=np.float32))
                _add_dataset(g, "xCoordinates", np.arange(rng.shape[1], dtype=np.float64))
                _add_dataset(g, "yCoordinates", np.arange(rng.shape[0], dtype=np.float64))
                pr = _add_dataset(g, "projection", np.array(32645, dtype=np.int32))
                pr.attrs["epsg_code"] = 32645
        _add_dataset(f.require_group("science/LSAR/GOFF/grids/frequencyA"), "unrelatedLargeArray", np.ones((6, 7), dtype=np.float32))


def make_gunw(path: Path):
    phase = np.linspace(-1, 1, 42, dtype=np.float32).reshape(6, 7)
    with h5py.File(path, "w") as f:
        f.attrs["institution"] = "synthetic-test"
        grid = f.require_group("science/LSAR/GUNW/grids/frequencyA/unwrappedInterferogram")
        grid.attrs["grid_note"] = "copied group attr"
        hh = grid.require_group("HH")
        _add_dataset(hh, "unwrappedPhase", phase, {"units": "radians"})
        _add_dataset(hh, "coherenceMagnitude", np.full(phase.shape, .9, dtype=np.float32))
        _add_dataset(hh, "connectedComponents", np.ones(phase.shape, dtype=np.int16))
        _add_dataset(hh, "mask", np.zeros(phase.shape, dtype=np.uint8))
        _add_dataset(hh, "ionospherePhaseScreen", np.full(phase.shape, .01, dtype=np.float32))
        _add_dataset(grid, "xCoordinates", np.arange(phase.shape[1], dtype=np.float64))
        _add_dataset(grid, "yCoordinates", np.arange(phase.shape[0], dtype=np.float64))
        proj = _add_dataset(grid, "projection", np.array(32645, dtype=np.int32))
        proj.attrs["epsg_code"] = 32645
        wrapped = f.require_group("science/LSAR/GUNW/grids/frequencyA/wrappedInterferogram/HH")
        # Model the real product's dimension scales, including the valid empty
        # HDF5 scale name seen in NISAR wrapped-interferogram x/y coordinate axes.
        xw = _add_dataset(wrapped, "xCoordinates", np.arange(28, dtype=np.float64))
        yw = _add_dataset(wrapped, "yCoordinates", np.arange(24, dtype=np.float64))
        xw.make_scale("")
        yw.make_scale("")
        complex_wrapped = _add_dataset(
            wrapped, "wrappedInterferogram", np.exp(1j * np.linspace(-np.pi, np.pi, 24 * 28,
                                                                        dtype=np.float32)).reshape(24, 28).astype(np.complex64),
            {"description": "Complex wrapped interferogram between HH layers",
             "grid_mapping": "projection", "units": "1"},
        )
        # GUNW may carry useful higher-posting coherence next to the wrapped complex raster.
        coherence_wrapped = _add_dataset(wrapped, "coherenceMagnitude", np.full((24, 28), 0.91, dtype=np.float32))
        for ds in (complex_wrapped, coherence_wrapped):
            ds.dims[0].label = "y"
            ds.dims[0].attach_scale(yw)
            ds.dims[1].label = "x"
            ds.dims[1].attach_scale(xw)
        meta = f.require_group("science/LSAR/GUNW/metadata/radarGrid")
        _add_dataset(meta, "xCoordinates", np.arange(7, dtype=np.float64))
        _add_dataset(meta, "yCoordinates", np.arange(6, dtype=np.float64))
        pp = _add_dataset(meta, "projection", np.array(32645, dtype=np.int32))
        pp.attrs["epsg_code"] = 32645
        for n in ("losUnitVectorX", "losUnitVectorY", "incidenceAngle"):
            _add_dataset(meta, n, np.ones((1, 6, 7), dtype=np.float32))
        _add_dataset(f.require_group("science/LSAR/identification"), "lookDirection", np.bytes_("Left"))
        _add_dataset(f.require_group("science/LSAR/GUNW/metadata/processingInformation"), "centerFrequency", np.array(1.2575e9, dtype=np.float64))
        gpo = f.require_group("science/LSAR/GUNW/grids/frequencyA/pixelOffsets/HH/layer1")
        _add_dataset(gpo, "slantRangeOffset", np.full((6, 7), 0.25, dtype=np.float32))
        _add_dataset(gpo, "correlationSurfacePeak", np.full((6, 7), 0.91, dtype=np.float32))
        _add_dataset(f.require_group("science/LSAR/GUNW/grids/frequencyA"), "someOtherBigGrid", np.ones((8, 8), dtype=np.float32))


@pytest.mark.parametrize("family", ["GOFF", "GUNW"])
def test_archive_preserves_declared_dependencies_and_never_deletes_source(tmp_path, family):
    source = tmp_path / ("NISAR_L2_PR_" + family + "_test.h5")
    archive = tmp_path / (family + ".science.h5")
    manifest = tmp_path / (family + ".manifest.json")
    (make_goff if family == "GOFF" else make_gunw)(source)

    result = archive_tool.build_archive(source, archive, manifest, family, include_wrapped=False)
    assert source.exists(), "archive tool must never delete raw source"
    assert archive.exists() and manifest.exists()
    assert result["verification"]["local_archive_verification"] == "PASS"
    assert result["policy"]["deletion_performed"] is False
    assert archive_tool.verify_archive(source, archive, manifest)["status"] == "PASS"

    doc = json.loads(manifest.read_text(encoding="utf-8"))
    retained_paths = {item["path"] for item in doc["retained_datasets"]}
    excluded_paths = {item["path"] for item in doc["excluded_datasets"]}
    with h5py.File(source, "r") as src, h5py.File(archive, "r") as out:
        for path in retained_paths:
            assert path in out
            assert src[path].shape == out[path].shape
            assert src[path].dtype == out[path].dtype
            # small fixtures: direct element-wise equality catches shape/type/value mistakes
            assert archive_tool._value_equal(src[path][()], out[path][()])
        if family == "GOFF":
            assert any("pixelOffsets/HH/layer1/slantRangeOffset" in p for p in retained_paths)
            assert any("pixelOffsets/VV/layer3/alongTrackOffsetVariance" in p for p in retained_paths)
            assert any("unrelatedLargeArray" in p for p in excluded_paths), \
                "engine-ready profile excludes non-engine multidimensional GOFF arrays"
        else:
            assert any(p.endswith("/unwrappedPhase") for p in retained_paths)
            assert any(p.endswith("/ionospherePhaseScreen") for p in retained_paths)
            assert any(p.endswith("/losUnitVectorX") for p in retained_paths)
            assert any(p.endswith("/losUnitVectorY") for p in retained_paths)
            assert any(p.endswith("/incidenceAngle") for p in retained_paths)
            assert any(p.endswith("/lookDirection") for p in retained_paths)
            assert any("GUNW/grids/frequencyA/pixelOffsets/HH/layer1/slantRangeOffset" in p for p in retained_paths)
            assert any("GUNW/grids/frequencyA/pixelOffsets/HH/layer1/correlationSurfacePeak" in p for p in retained_paths)
            assert any(p.endswith("/wrappedInterferogram/HH/wrappedInterferogram") for p in excluded_paths)
            assert any(p.endswith("/wrappedInterferogram/HH/coherenceMagnitude") for p in excluded_paths), \
                "engine-ready excludes high-posting wrapped-grid coherence that no pinned engine path reads"
            assert doc["derived_datasets"] == [], "wrappedPhase derivation is explicit opt-in, not part of every engine-ready archive"
            assert any("someOtherBigGrid" in p for p in excluded_paths), \
                "non-engine multidimensional auxiliary grids are excluded explicitly from engine-ready profile"


def test_include_wrapped_is_explicit_opt_in(tmp_path):
    source = tmp_path / "NISAR_L2_PR_GUNW_test.h5"
    (make_gunw)(source)
    archive = tmp_path / "with-wrapped.h5"
    manifest = tmp_path / "with-wrapped.json"
    doc = archive_tool.build_archive(source, archive, manifest, "GUNW", include_wrapped=True, derive_wrapped_phase=True)
    assert doc["policy"]["include_wrapped_interferogram"] is True
    with h5py.File(archive, "r") as f:
        assert "science/LSAR/GUNW/grids/frequencyA/wrappedInterferogram/HH/wrappedInterferogram" in f
        assert "science/LSAR/GUNW/grids/frequencyA/wrappedInterferogram/HH/coherenceMagnitude" in f
        # Phase derivation is explicit opt-in; when requested it sits beside
        # the retained complex array and is verified against its exact samples.
        assert "science/LSAR/GUNW/grids/frequencyA/wrappedInterferogram/HH/wrappedPhase" in f


def test_corrupted_archive_is_rejected(tmp_path):
    source = tmp_path / "NISAR_L2_PR_GOFF_test.h5"
    (make_goff)(source)
    archive = tmp_path / "GOFF.science.h5"
    manifest = tmp_path / "GOFF.manifest.json"
    archive_tool.build_archive(source, archive, manifest, "GOFF")
    with h5py.File(archive, "r+") as f:
        ds = f["science/LSAR/GOFF/grids/frequencyA/pixelOffsets/HH/layer1/slantRangeOffset"]
        ds[0, 0] = 999.0
    with pytest.raises(RuntimeError, match="verification failed"):
        archive_tool.verify_archive(source, archive, manifest)


def test_dataset_hash_does_not_depend_on_hdf5_chunk_layout(tmp_path):
    data = np.arange(1024 * 2048, dtype=np.float64).reshape(1024, 2048)
    a_path, b_path = tmp_path / "a.h5", tmp_path / "b.h5"
    with h5py.File(a_path, "w") as f:
        f.create_dataset("values", data=data, chunks=(64, 256))
    with h5py.File(b_path, "w") as f:
        f.create_dataset("values", data=data, chunks=(512, 1024), compression="gzip", compression_opts=1)
    with h5py.File(a_path, "r") as a, h5py.File(b_path, "r") as b:
        assert archive_tool.dataset_digest(a["values"]) == archive_tool.dataset_digest(b["values"])


def test_reference_dataset_fails_closed_without_deleting_source(tmp_path):
    source = tmp_path / "NISAR_L2_PR_GOFF_001_reference.h5"
    archive = tmp_path / "reference.archive.h5"
    manifest = tmp_path / "reference.manifest.json"
    with h5py.File(source, "w") as f:
        target = f.create_dataset("target", data=np.asarray([1, 2, 3], dtype="int16"))
        refs = f.create_dataset("science/LSAR/GOFF/metadata/referenceLinks", shape=(1,), dtype=h5py.ref_dtype)
        refs[0] = target.ref
        layer = f.require_group("science/LSAR/GOFF/grids/frequencyA/pixelOffsets/HH/layer2")
        layer.create_dataset("slantRangeOffset", data=np.ones((2, 2), dtype="float32"))
    with pytest.raises(ValueError, match="HDF5 object/region references"):
        archive_tool.build_archive(source, archive, manifest, "GOFF")
    assert source.exists()
    assert not archive.exists()
    assert not manifest.exists()


def test_soft_links_are_inventoried_and_archive_fails_closed(tmp_path):
    source = tmp_path / "NISAR_L2_PR_GOFF_softlink.h5"
    archive = tmp_path / "softlink.archive.h5"
    manifest = tmp_path / "softlink.manifest.json"
    with h5py.File(source, "w") as f:
        g = f.require_group("science/LSAR/GOFF/grids/frequencyA/pixelOffsets/HH/layer2")
        g.create_dataset("slantRangeOffset", data=np.ones((4, 4), dtype="float32"))
        g["offsetAlias"] = h5py.SoftLink("/science/LSAR/GOFF/grids/frequencyA/pixelOffsets/HH/layer2/slantRangeOffset")
    with h5py.File(source, "r") as f:
        links = archive_tool.discover_noncanonical_links(f)
    assert any(link["type"] == "soft_link" and link["path"].endswith("offsetAlias") for link in links)
    with pytest.raises(ValueError, match="Non-canonical HDF5 links/aliases"):
        archive_tool.build_archive(source, archive, manifest, "GOFF")
    assert source.exists()
    assert not archive.exists()
    assert not manifest.exists()


def test_unknown_reference_attribute_fails_closed_without_deleting_source(tmp_path):
    source = tmp_path / "NISAR_L2_PR_GOFF_attribute_reference.h5"
    archive = tmp_path / "attribute-reference.archive.h5"
    manifest = tmp_path / "attribute-reference.manifest.json"
    with h5py.File(source, "w") as f:
        target = f.create_dataset("target", data=np.asarray([1, 2, 3], dtype="int16"))
        g = f.require_group("science/LSAR/GOFF/grids/frequencyA/pixelOffsets/HH/layer2")
        ds = g.create_dataset("slantRangeOffset", data=np.ones((2, 2), dtype="float32"))
        ds.attrs["custom_target_ref"] = target.ref
    with pytest.raises(ValueError, match="Non-standard HDF5 reference attribute"):
        archive_tool.build_archive(source, archive, manifest, "GOFF")
    assert source.exists()
    assert not archive.exists()
    assert not manifest.exists()


def test_dimension_scales_are_rebuilt_in_curated_archive(tmp_path):
    source = tmp_path / "NISAR_L2_PR_GOFF_scales.h5"
    archive = tmp_path / "scales.archive.h5"
    manifest = tmp_path / "scales.manifest.json"
    with h5py.File(source, "w") as f:
        g = f.require_group("science/LSAR/GOFF/grids/frequencyA/pixelOffsets/HH/layer2")
        x = g.create_dataset("xCoordinates", data=np.arange(4, dtype="float64"))
        y = g.create_dataset("yCoordinates", data=np.arange(3, dtype="float64"))
        x.make_scale("xCoordinates")
        y.make_scale("yCoordinates")
        ds = g.create_dataset("slantRangeOffset", data=np.ones((3, 4), dtype="float32"))
        ds.dims[0].label = "y"
        ds.dims[0].attach_scale(y)
        ds.dims[1].label = "x"
        ds.dims[1].attach_scale(x)
    archive_tool.build_archive(source, archive, manifest, "GOFF")
    with h5py.File(archive, "r") as f:
        ds = f["science/LSAR/GOFF/grids/frequencyA/pixelOffsets/HH/layer2/slantRangeOffset"]
        assert ds.dims[0].label == "y"
        assert ds.dims[1].label == "x"
        assert [scale.name.rsplit("/", 1)[-1] for _, scale in ds.dims[0].items()] == ["yCoordinates"]
        assert [scale.name.rsplit("/", 1)[-1] for _, scale in ds.dims[1].items()] == ["xCoordinates"]


def test_dataset_fillvalue_and_extendible_maxshape_are_preserved(tmp_path):
    source = tmp_path / "NISAR_L2_PR_GOFF_fillvalue.h5"
    archive = tmp_path / "fillvalue.archive.h5"
    manifest = tmp_path / "fillvalue.manifest.json"
    with h5py.File(source, "w") as f:
        layer = f.require_group("science/LSAR/GOFF/grids/frequencyA/pixelOffsets/HH/layer2")
        layer.create_dataset(
            "slantRangeOffset", data=np.ones((3, 4), dtype="float32"),
            chunks=(2, 4), maxshape=(None, 4), fillvalue=np.float32(-9999.0),
        )
    archive_tool.build_archive(source, archive, manifest, "GOFF")
    path = "science/LSAR/GOFF/grids/frequencyA/pixelOffsets/HH/layer2/slantRangeOffset"
    with h5py.File(source, "r") as raw, h5py.File(archive, "r") as kept:
        assert raw[path].maxshape == kept[path].maxshape == (None, 4)
        assert raw[path].fillvalue == kept[path].fillvalue == np.float32(-9999.0)
    doc = json.loads(manifest.read_text(encoding="utf-8"))
    rec = next(item for item in doc["retained_datasets"] if item["path"] == "/" + path)
    assert rec["dataset_creation_properties"]["maxshape"] == [None, 4]
    assert rec["dataset_creation_properties"]["fillvalue"] == -9999.0


def test_hdf5_userblock_fails_closed_without_deleting_source(tmp_path):
    source = tmp_path / "NISAR_L2_PR_GOFF_userblock.h5"
    archive = tmp_path / "userblock.archive.h5"
    manifest = tmp_path / "userblock.manifest.json"
    with h5py.File(source, "w", userblock_size=512) as f:
        layer = f.require_group("science/LSAR/GOFF/grids/frequencyA/pixelOffsets/HH/layer2")
        layer.create_dataset("slantRangeOffset", data=np.ones((2, 2), dtype="float32"))
    with pytest.raises(ValueError, match="user block outside the dataset hierarchy"):
        archive_tool.build_archive(source, archive, manifest, "GOFF")
    assert source.exists()
    assert not archive.exists()
    assert not manifest.exists()


def test_full_lossless_profile_preserves_all_source_datasets(tmp_path):
    source = tmp_path / "NISAR_L2_PR_GUNW_full.h5"
    archive = tmp_path / "full-lossless.science.h5"
    manifest = tmp_path / "full-lossless.manifest.json"
    make_gunw(source)
    result = archive_tool.build_archive(
        source, archive, manifest, "GUNW", retention_profile="full-lossless"
    )
    doc = json.loads(manifest.read_text(encoding="utf-8"))
    with h5py.File(source, "r") as raw, h5py.File(archive, "r") as kept:
        expected = set()
        raw.visititems(lambda name, obj: expected.add("/" + name) if isinstance(obj, h5py.Dataset) else None)
        assert all(path in kept for path in expected)
        assert all(archive_tool._value_equal(raw[path][()], kept[path][()]) for path in expected)
    assert not doc["excluded_datasets"]
    assert not doc["derived_datasets"]
    assert result["policy"]["retention_profile"] == "full-lossless"
    assert result["policy"]["include_wrapped_interferogram"] is True


def test_h5ocopy_preserves_source_chunking_and_lossless_filters(tmp_path):
    source = tmp_path / "NISAR_L2_PR_GOFF_filters.h5"
    archive = tmp_path / "filters.science.h5"
    manifest = tmp_path / "filters.manifest.json"
    values = np.arange(128 * 64, dtype=np.float32).reshape(128, 64)
    with h5py.File(source, "w") as raw:
        group = raw.require_group("science/LSAR/GOFF/grids/frequencyA/pixelOffsets/HH/layer2")
        ds = group.create_dataset(
            "slantRangeOffset", data=values, chunks=(16, 64),
            compression="gzip", compression_opts=4, shuffle=True, fletcher32=True,
            fillvalue=np.float32(-9999.0),
        )
        ds.attrs["units"] = "meters"
        group.create_dataset("xCoordinates", data=np.arange(64, dtype=np.float64))
        group.create_dataset("yCoordinates", data=np.arange(128, dtype=np.float64))
        p = group.create_dataset("projection", data=np.array(32645, dtype=np.uint32))
        p.attrs["epsg_code"] = 32645
    archive_tool.build_archive(source, archive, manifest, "GOFF")
    with h5py.File(source, "r") as raw, h5py.File(archive, "r") as kept:
        a = raw["science/LSAR/GOFF/grids/frequencyA/pixelOffsets/HH/layer2/slantRangeOffset"]
        b = kept[a.name]
        assert a.chunks == b.chunks == (16, 64)
        assert a.compression == b.compression == "gzip"
        assert a.compression_opts == b.compression_opts == 4
        assert a.shuffle == b.shuffle is True
        assert a.fletcher32 == b.fletcher32 is True
        assert a.fillvalue == b.fillvalue == np.float32(-9999.0)
        assert np.array_equal(a[()], b[()])


def test_engine_ready_exclusion_reasons_are_dataset_specific():
    wrapped = "/science/LSAR/GUNW/grids/frequencyA/wrappedInterferogram/HH/coherenceMagnitude"
    radar = "/science/LSAR/GUNW/metadata/radarGrid/someOtherCube"
    auxiliary = "/science/LSAR/GUNW/grids/frequencyA/someOtherBigGrid"
    assert archive_tool.selection_reason(wrapped, 2, "GUNW", False, "engine-ready") is None
    assert "high-posting wrappedInterferogram-grid" in archive_tool.exclusion_reason(
        wrapped, "GUNW", "engine-ready", False
    )
    assert "LOS unit vector X/Y and incidenceAngle" in archive_tool.exclusion_reason(
        radar, "GUNW", "engine-ready", False
    )
    assert "no reader or downstream processing path" in archive_tool.exclusion_reason(
        auxiliary, "GUNW", "engine-ready", False
    )
    assert archive_tool.selection_reason(wrapped, 2, "GUNW", False, "full-lossless") == "full_lossless_source_dataset"
    assert archive_tool.selection_reason(wrapped, 2, "GUNW", True, "engine-ready") == "wrapped_grid_retained_by_explicit_include_wrapped"
