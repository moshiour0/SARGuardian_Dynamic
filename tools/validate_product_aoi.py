#!/usr/bin/env python3
"""Fail early if a NISAR product grid does not overlap the requested AOI."""
from __future__ import annotations
import argparse, importlib, json, sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import h5py
import numpy as np


def _grid(f, datasets, engine, group, parent, label):
    def exact_axis(name):
        for base in (group, parent):
            if base:
                for p, (shape, _dtype) in datasets.items():
                    if p.lower() == f"{base}/{name}".lower() and len(shape) == 1:
                        return p
        return None
    xp, yp = exact_axis("xCoordinates"), exact_axis("yCoordinates")
    epsg = None
    for base in (group, parent):
        if base:
            pp = next((p for p in datasets if p.lower() == f"{base}/projection".lower()), None)
            if pp:
                try:
                    epsg = int(f[pp].attrs.get("epsg_code", np.ravel(f[pp][()])[0]))
                    break
                except Exception:
                    pass
    result = {"grid": label, "group": group, "x_path": xp, "y_path": yp,
              "epsg": epsg, "intersects_aoi": False, "grid_pixels_inside_aoi": 0,
              "shape": None, "sample_points_wgs84": []}
    if not xp or not yp:
        result["error"] = "GRID_COORDINATES_NOT_FOUND"
        return result
    xs = np.asarray(f[xp][()], dtype=np.float64)
    ys = np.asarray(f[yp][()], dtype=np.float64)
    result["shape"] = [int(ys.size), int(xs.size)]
    mask = engine.build_aoi_mask(xs, ys, engine.AOI_RING, epsg)
    if mask is None:
        result["error"] = "AOI_GRID_TRANSFORM_UNAVAILABLE"
        return result
    indices = np.argwhere(mask)
    result["grid_pixels_inside_aoi"] = int(indices.shape[0])
    result["intersects_aoi"] = bool(indices.shape[0])
    if indices.size:
        try:
            from pyproj import Transformer
            inverse = Transformer.from_crs(epsg or 4326, 4326, always_xy=True)
            for pos in np.linspace(0, len(indices)-1, min(3, len(indices)), dtype=int):
                i,j=(int(v) for v in indices[pos])
                lon,lat=inverse.transform(float(xs[j]),float(ys[i]))
                result["sample_points_wgs84"].append({"lat":float(lat),"lon":float(lon),"row":i,"col":j})
        except Exception as exc:
            result["sample_points_error"] = type(exc).__name__
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input",required=True,type=Path)
    p.add_argument("--product-type",required=True,choices=["GUNW","GOFF"])
    p.add_argument("--aoi",required=True,choices=["source","langtang","lhende","custom"])
    p.add_argument("--science-root",required=True,type=Path)
    p.add_argument("--science-commit",required=True)
    p.add_argument("--out",required=True,type=Path)
    a=p.parse_args()
    report={"schema_version":"sarguardian-aoi-preflight/1.0",
            "checked_at_utc":datetime.now(timezone.utc).isoformat(),
            "source_filename":a.input.name,"product_type":a.product_type,"aoi_name":a.aoi,
            "science_source_commit":a.science_commit,"grid_checks":[],"status":"FAIL","errors":[]}
    src=a.science_root/"src"
    if not a.input.is_file():
        report["errors"].append("SOURCE_HDF5_NOT_FOUND")
    elif not (src/"gunw_reader.py").is_file() or not (src/"goff_reader.py").is_file():
        report["errors"].append("SCIENCE_READER_SOURCE_NOT_FOUND")
    else:
        sys.path.insert(0,str(src.resolve()))
        try:
            engine=importlib.import_module("gunw_reader")
            engine.set_aoi(a.aoi)
            with h5py.File(a.input,"r") as f:
                ds=engine.walk(f)
                if a.product_type=="GUNW":
                    phase=engine.find(ds,"unwrappedphase",ndim=2)
                    if not phase:
                        report["errors"].append("GUNW_UNWRAPPED_PHASE_NOT_FOUND")
                    else:
                        group=phase.rsplit("/",1)[0]
                        report["grid_checks"].append(_grid(f,ds,engine,group,group.rsplit("/",1)[0],"GUNW_unwrappedInterferogram"))
                    root="science/LSAR/GUNW/metadata/radarGrid"
                    if f"{root}/losUnitVectorX" not in f:
                        report["errors"].append("GUNW_RADAR_GRID_LOS_GEOMETRY_NOT_FOUND")
                    else:
                        report["grid_checks"].append(_grid(f,ds,engine,root,None,"GUNW_geometry_merge_radarGrid"))
                else:
                    groups=sorted({path.rsplit("/",1)[0] for path,(shape,_dtype) in ds.items()
                                   if path.lower().endswith("/slantrangeoffset") and len(shape)==2})
                    if not groups:
                        report["errors"].append("GOFF_SLANT_RANGE_OFFSET_NOT_FOUND")
                    for group in groups:
                        report["grid_checks"].append(_grid(f,ds,engine,group,None,"GOFF_pixelOffsets"))
        except Exception as exc:
            report["errors"].append(f"PREFLIGHT_EXCEPTION_{type(exc).__name__}")
        finally:
            if str(src.resolve()) in sys.path: sys.path.remove(str(src.resolve()))
    if not report["errors"]:
        required=report["grid_checks"]
        if a.product_type=="GUNW":
            required=[g for g in required if g["grid"] in {"GUNW_unwrappedInterferogram","GUNW_geometry_merge_radarGrid"}]
        missing=[g for g in required if not g.get("intersects_aoi",False)]
        if not required: report["errors"].append("NO_SPATIAL_GRID_WAS_VALIDATED")
        elif missing:
            report["errors"].append("AOI_DOES_NOT_INTERSECT_PRODUCT_GRID")
            report["non_intersecting_grids"]=[{"grid":g["grid"],"group":g["group"],"shape":g.get("shape"),
                                               "epsg":g.get("epsg"),"pixels":g.get("grid_pixels_inside_aoi",0)} for g in missing]
        else: report["status"]="PASS"
    a.out.parent.mkdir(parents=True,exist_ok=True)
    a.out.write_text(json.dumps(report,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    if report["status"]!="PASS":
        err="AOI_DOES_NOT_INTERSECT_PRODUCT_GRID" if "AOI_DOES_NOT_INTERSECT_PRODUCT_GRID" in report["errors"] else report["errors"][0]
        print(f"ERROR={err}",file=sys.stderr)
        print(f"PREFLIGHT_REPORT={a.out}")
        return 2
    print(f"AOI_PREFLIGHT=PASS GRIDS_CHECKED={len(report['grid_checks'])}")
    for g in report["grid_checks"]:
        print(f"GRID_INTERSECTION={g['grid']} PIXELS={g['grid_pixels_inside_aoi']} EPSG={g['epsg']}")
    print(f"PREFLIGHT_REPORT={a.out}")
    return 0

if __name__=="__main__":
    raise SystemExit(main())
