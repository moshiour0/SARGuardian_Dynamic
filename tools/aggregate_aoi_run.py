#!/usr/bin/env python3
"""Assemble dynamic-WKT product artifacts and build a self-contained results dashboard."""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import re
import shutil
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

def safe_child(root: Path, rel: str) -> Path:
    candidate = (root / rel).resolve()
    candidate.relative_to(root.resolve())
    return candidate

def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if value is None or isinstance(value, (bool, int, str)):
        if isinstance(value, str):
            text = value.strip()
            try:
                number = float(text)
                if math.isfinite(number) and text and re.fullmatch(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?", text):
                    return number
            except (TypeError, ValueError, OverflowError):
                pass
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return str(value)

def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise ValueError("CSV_HEADER_MISSING:" + path.name)
        return list(reader.fieldnames), list(reader)

def canonical_granule_name(value: Any) -> str:
    """Compare archive granule identifiers consistently with reader CSV paths."""
    name = Path(str(value or "").strip()).name
    return name[:-3] if name.lower().endswith(".h5") else name


def matching_product_stats_rows(
    stats_rows: list[dict[str, str]], granule_name: str, family: str
) -> list[dict[str, str]]:
    """Match one product row when the manifest omits the reader's .h5 suffix."""
    expected = canonical_granule_name(granule_name)
    matching = [
        row for row in stats_rows
        if canonical_granule_name(row.get("file")) == expected
    ]
    if family.upper() == "GOFF":
        matching = [row for row in matching if "layer2" in (row.get("layer") or "").lower()]
    return matching


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

def assess_maturity_consistency(
    catalog_maturity: str | None, reader_maturity: str | None
) -> tuple[str, list[str], bool]:
    """Reconcile archive maturity with the science reader's CRID classifier.

    Return (validation label, user-visible warnings, hard-conflict flag). An
    UNKNOWN local CRID mapping may defer to verified ASF dataMaturity metadata,
    but must never be silently shown as a fully matched maturity assessment.
    """
    catalog = str(catalog_maturity or "").strip().upper()
    reader = str(reader_maturity or "").strip().upper()
    supported = {"BETA", "PROVISIONAL"}
    if catalog in supported and reader == catalog:
        return "MATCH", [], False
    if catalog in supported and reader == "UNKNOWN":
        return (
            "CATALOG_CLASSIFIED_LOCAL_CRID_MAP_UNKNOWN",
            [
                "ASF catalog class is " + catalog + ", but the pinned local CRID classifier reports UNKNOWN; review maturity_evidence before scientific comparison."
            ],
            False,
        )
    if catalog in supported and reader in supported and reader != catalog:
        return "CONFLICT", [], True
    if not reader:
        return (
            "READER_MATURITY_NOT_REPORTED",
            ["Reader CSV does not report maturity; use archived catalog classification and CRID evidence."],
            False,
        )
    if reader in supported and not catalog:
        return (
            "CATALOG_MATURITY_NOT_VERIFIED",
            ["Reader CRID classifier reports " + reader + ", but the manifest has no authoritative catalog maturity label."],
            False,
        )
    return (
        "MATURITY_UNRESOLVED",
        ["Catalog and reader maturity could not be reconciled; do not combine this product into maturity-sensitive comparisons."],
        False,
    )


def numeric(row: dict[str, Any], candidates: tuple[str, ...]) -> tuple[str | None, float | None]:
    lowmap = {str(k).lower(): k for k in row}
    for candidate in candidates:
        key = lowmap.get(candidate.lower())
        if key is not None:
            try:
                value = float(row[key])
                if math.isfinite(value):
                    return str(key), value
            except (TypeError, ValueError, OverflowError):
                pass
    for key, raw in row.items():
        low = str(key).lower()
        if "median" not in low or "coherence" in low or "corr" in low:
            continue
        try:
            value = float(raw)
            if math.isfinite(value):
                return str(key), value
        except (TypeError, ValueError, OverflowError):
            continue
    return None, None

def make_quicklook(raster: Path, target: Path) -> dict[str, Any] | None:
    try:
        import numpy as np
        import rasterio
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None
    try:
        with rasterio.open(raster) as ds:
            band = ds.read(1, out_shape=(min(700, ds.height), min(700, ds.width)),
                           masked=True, resampling=rasterio.enums.Resampling.average)
            data = np.asarray(band.filled(np.nan), dtype="float32")
            valid = data[np.isfinite(data)]
            info = {"source": raster.name, "quicklook": target.name,
                    "valid_pixels_preview": int(valid.size),
                    "scale_policy": "per-image symmetric 98th percentile"}
            if not valid.size:
                fig, ax = plt.subplots(figsize=(6, 4))
                ax.text(0.5, 0.5, "No valid science pixels", ha="center", va="center")
                ax.set_axis_off()
            else:
                limit = float(np.percentile(np.abs(valid), 98))
                if not math.isfinite(limit) or limit <= 0:
                    limit = float(np.max(np.abs(valid))) or 1.0
                fig, ax = plt.subplots(figsize=(6, 5))
                im = ax.imshow(data, cmap="RdBu_r", vmin=-limit, vmax=limit, interpolation="nearest")
                fig.colorbar(im, ax=ax, shrink=0.78, label="Band 1 (product units)")
                ax.set_title(raster.name, fontsize=8)
                ax.set_xticks([]); ax.set_yticks([])
            fig.tight_layout()
            target.parent.mkdir(parents=True, exist_ok=True)
            fig.savefig(target, dpi=110)
            plt.close(fig)
            return info
    except Exception as exc:
        return {"source": raster.name, "error": type(exc).__name__ + ":" + str(exc)[:180]}

def polygon_svg(ring: list[tuple[float, float]]) -> str:
    xs = [p[0] for p in ring]; ys = [p[1] for p in ring]
    xmin, xmax = min(xs), max(xs); ymin, ymax = min(ys), max(ys)
    dx = max(xmax-xmin, 1e-10); dy = max(ymax-ymin, 1e-10)
    coords = []
    for x, y in ring:
        coords.append("{:.2f},{:.2f}".format(16 + 368*(x-xmin)/dx, 184 - 168*(y-ymin)/dy))
    grid = "".join('<path d="M {} 12 V 190"/>'.format(x) for x in (80,160,240,320))
    grid += "".join('<path d="M 12 {} H 388"/>'.format(y) for y in (54,96,138,180))
    return ('<svg viewBox="0 0 400 205" role="img" aria-label="Schematic AOI polygon">'
            '<rect x="1" y="1" width="398" height="203" rx="8" fill="#0f172a" stroke="#334155"/>'
            '<g stroke="#334155" stroke-width=".6">' + grid + '</g>'
            '<polygon points="' + " ".join(coords) + '" fill="#38bdf8" fill-opacity=".25" stroke="#7dd3fc" stroke-width="2"/>'
            '<text x="10" y="198" font-size="8" fill="#94a3b8">Outline only · not a basemap</text></svg>')

def write_html(out: Path, summary: dict[str, Any], product_rows: list[dict[str, Any]], svg: str) -> None:
    dataset = json.dumps({"summary": summary, "products": product_rows}, ensure_ascii=False, separators=(",", ":"))
    dataset = dataset.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    doc = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SARGuardian · AOI Processing Dashboard</title>
<style>
:root{color-scheme:dark;--bg:#080d18;--panel:#101827;--edge:#273449;--muted:#9aa9bd;--ink:#edf4ff;--blue:#7dd3fc;--gold:#fbbf24}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at top,#12213a 0,#080d18 48%);color:var(--ink);font:14px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1420px;margin:auto;padding:25px}header{display:flex;justify-content:space-between;align-items:flex-start;gap:20px;flex-wrap:wrap;border-bottom:1px solid var(--edge);padding-bottom:20px}.kicker{letter-spacing:.18em;text-transform:uppercase;color:var(--blue);font-size:11px;font-weight:700}h1{font-size:clamp(26px,3vw,39px);margin:9px 0 12px;letter-spacing:-.035em;line-height:1.1}.lede{color:var(--muted);max-width:800px;margin:0}.status{border:1px solid var(--edge);background:#0c1422;padding:10px 14px;border-radius:999px;font-size:12px;font-weight:700}
.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:18px 0}.card,.panel{background:linear-gradient(145deg,#111d2e,#0d1421);border:1px solid var(--edge);border-radius:14px;padding:16px;min-width:0}.label,.fine{font-size:11px;color:var(--muted)}.label{text-transform:uppercase;letter-spacing:.07em}.metric{font-size:clamp(22px,2.4vw,31px);font-weight:750;margin-top:7px;overflow-wrap:anywhere}.sub{color:var(--muted);font-size:12px;margin-top:3px}
.layout{display:grid;grid-template-columns:minmax(0,1.2fr) minmax(270px,.8fr);gap:14px}.panel h2{margin:0 0 13px;font-size:16px}.meta{display:grid;grid-template-columns:145px 1fr;gap:7px 12px;overflow-wrap:anywhere}.key{color:var(--muted)}.wkt{font:11px/1.45 ui-monospace,Consolas,monospace;overflow-wrap:anywhere;max-height:130px;overflow:auto;color:#d3e3f9;background:#09101c;border:1px solid var(--edge);padding:10px;border-radius:8px;margin-top:10px}
.notice{border-left:3px solid var(--gold);padding:10px 13px;background:#241d11;color:#f9e7bb;border-radius:0 8px 8px 0;margin:14px 0}.tools{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0}input,select{background:#0b1220;color:var(--ink);border:1px solid #34445b;padding:9px 11px;border-radius:8px;min-width:150px}.btn{display:inline-block;border:1px solid #37516e;border-radius:8px;padding:9px 12px;color:var(--ink);text-decoration:none;background:#10213a;font-weight:650}
.wrap{overflow:auto;border:1px solid var(--edge);border-radius:10px}table{border-collapse:collapse;width:100%;min-width:1000px;background:#0b1320}th,td{border-bottom:1px solid #233247;padding:10px;text-align:left;vertical-align:top}th{font-size:11px;color:#bac8da;text-transform:uppercase;letter-spacing:.07em;background:#111d2e;position:sticky;top:0}td{font-size:12px}.mono{font-family:ui-monospace,Consolas,monospace;overflow-wrap:anywhere}.pill{display:inline-block;border:1px solid #40536d;border-radius:999px;padding:2px 7px;font-size:10px}.warn{color:#fcd34d}.bad{color:#fca5a5}.preview{width:165px;height:110px;object-fit:contain;background:#080d18;border:1px solid var(--edge);border-radius:7px}.section{margin-top:23px}.chart{height:210px;width:100%;background:#0a1220;border:1px solid var(--edge);border-radius:10px}.chart svg{width:100%;height:100%}footer{padding:25px 0;color:var(--muted);font-size:11px}
@media(max-width:920px){.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.layout{grid-template-columns:1fr}}@media(max-width:520px){main{padding:13px}.grid{grid-template-columns:1fr 1fr}.metric{font-size:22px}.meta{grid-template-columns:1fr;gap:3px}.key{margin-top:7px}}
</style></head><body><main>
<header><div><div class="kicker">SARGuardian · reproducible processing run</div><h1>AOI WKT Processing Dashboard</h1><p class="lede">NISAR L2 GUNW displacement and GOFF offset products discovered against the submitted polygon and inclusive secondary-date window. The dashboard records processing, provenance and pixel support; it does not certify a geophysical hazard claim.</p></div><div id="status" class="status">Preparing report</div></header>
<section class="grid" id="metrics"></section>
<section class="layout"><div class="panel"><h2>Run identity &amp; source policy</h2><div id="runmeta" class="meta"></div><div class="wkt" id="wkt"></div><div class="tools"><a class="btn" href="products.csv">Product table CSV</a><a class="btn" href="aoi-run-summary.json">Summary JSON</a><a class="btn" href="aoi-derived-results.zip">Derived bundle ZIP</a></div></div>
<div class="panel"><h2>Submitted AOI geometry</h2>__POLYGON_SVG__<p class="fine">Schematic polygon outline only; not a basemap and not a product footprint overlay.</p><h2 style="margin-top:16px">Processing caveats</h2><div id="caveats" class="fine"></div></div></section>
<section class="section panel"><h2>Reader metrics (one series at a time; not a common-datum time series)</h2><div class="notice">Pairwise medians may use different valid pixels, atmospheric conditions and reference support. GUNW and GOFF metrics can be different quantities or units, so this chart never mixes product families or metric columns. Do not interpret these values as a displacement time series without an independently verified common-reference/network inversion. Quicklook scales are per image.</div><div class="tools"><label for="metricSeries">Metric series:</label><select id="metricSeries" aria-label="Metric series to chart"></select></div><div class="chart" id="metricChart"></div><p class="fine">The selected series is grouped by product family and exact scientific-reader output column.</p></section>
<section class="section panel"><h2>Discovered and processed products</h2><div class="tools"><input id="search" placeholder="Filter granule or ID"><select id="family"><option value="">All families</option><option>GUNW</option><option>GOFF</option></select><select id="state"><option value="">All statuses</option><option>PASS</option><option>WARNING</option><option>FAIL</option></select></div><div class="wrap"><table><thead><tr><th>Product / pair</th><th>Family</th><th>Maturity (ASF / reader)</th><th>Reader median</th><th>Valid pixels</th><th>Grid support</th><th>Quicklook</th><th>Structural status</th><th>Source SHA-256</th></tr></thead><tbody id="rows"></tbody></table></div><p id="empty" class="fine"></p></section>
<footer>SARGuardian automated output · generated __GENERATED_AT__. Structural success means acquisition, requested-AOI masking, provenance and raster checks passed; it is not independent geophysical validation. Raw HDF5 is excluded from this dashboard bundle.</footer>
</main><script id="data" type="application/json">__DATASET__</script>
<script>
(function(){
var payload=JSON.parse(document.getElementById('data').textContent), S=payload.summary, P=payload.products;
function byId(id){return document.getElementById(id)}
function fmt(v){return v===null||v===undefined||v===''?'—':(typeof v==='number'?v.toLocaleString(undefined,{maximumFractionDigits:3}):String(v))}
function node(tag,cls,txt){var n=document.createElement(tag);if(cls)n.className=cls;if(txt!==undefined)n.textContent=txt;return n}
byId('status').textContent=S.status==='PASS'?'RUN COMPLETE · STRUCTURAL CHECKS PASS':(S.status==='PARTIAL'?'PARTIAL · INSPECT WARNINGS':'PROCESSING FAILED');
var cards=[['Products selected',S.products_expected,S.gunw_count+' GUNW · '+S.goff_count+' GOFF'],['Artifacts validated',S.products_validated,S.products_missing+' missing or invalid'],['Products with valid pixels',S.products_with_science_pixels,'At least one finite band-1 pixel'],['AOI WKT SHA-256',S.aoi_wkt_sha256.slice(0,12),S.date_start+' → '+S.date_end]];var D=S.science_diagnostics||{};cards.push(['Per-pixel stack',(D.pixel_stack||{}).status||'NOT RUN',((D.pixel_stack||{}).interpretation||'Inspect science-diagnostics.json and the pixel-stack log.')]);cards.push(['Candidate screening',(D.candidate_checks||{}).status||'NOT RUN',((D.candidate_checks||{}).detail||'Candidate screening is not a general detector or an alert.')]);
cards.forEach(function(c){var d=node('div','card');d.append(node('div','label',c[0]),node('div','metric',fmt(c[1])),node('div','sub',c[2]));byId('metrics').append(d)});
[['Run ID',S.run_id],['Science commit',S.science_commit],['Backend tools commit',S.orchestration_sha],['Dispatcher commit',S.dispatcher_workflow_sha],['AOI CRS',S.aoi_crs],['WKT SHA-256',S.aoi_wkt_sha256],['Run profile',S.run_profile],['Retention',S.retention_profile],['Catalog window','2024-01-01 → '+S.date_end],['Baseline comparison','Disabled for custom AOI']].forEach(function(pair){byId('runmeta').append(node('div','key',pair[0]),node('div','mono',fmt(pair[1])))});
byId('wkt').textContent=S.aoi_wkt;
['ASF catalog searched GUNW and GOFF using the exact WKT polygon, not its bounding box.',
'Each selected product has a separate grid-intersection report; pixels outside the polygon are excluded.',
'Product maturity, known release limitations, atmosphere and reference support still matter.',
'Quicklook colour limits are calculated separately for each image.',
'No-valid-pixel products are warnings, not a measured zero.'].forEach(function(t){var p=node('p','',t);byId('caveats').append(p)});
function statusOf(p){if(p.status==='FAIL')return'FAIL';if((p.warnings||[]).length||!p.science_pixel_support)return'WARNING';return'PASS'}
function medianOf(p){return p.metric_value===null||p.metric_value===undefined?'—':fmt(p.metric_value)+' '+(p.metric_name||'')}
function seriesKey(p){return String(p.product_type||'UNKNOWN')+'::'+String(p.metric_name||'UNKNOWN_METRIC')}
function seriesLabel(p){return String(p.product_type||'UNKNOWN')+' — '+String(p.metric_name||'UNKNOWN_METRIC')}
function updateMetricSeries(products){var select=byId('metricSeries'),previous=select.value,seen={},series=[];products.forEach(function(p){if(p.metric_value===null||p.metric_value===undefined||!Number.isFinite(Number(p.metric_value))||!p.metric_name)return;var key=seriesKey(p);if(!seen[key]){seen[key]=true;series.push({key:key,label:seriesLabel(p)})}});select.replaceChildren();series.forEach(function(s){var option=node('option','',s.label);option.value=s.key;select.append(option)});if(series.some(function(s){return s.key===previous}))select.value=previous;else if(series.length)select.value=series[0].key;return select.value}
function drawChart(products){var box=byId('metricChart');box.replaceChildren();var width=Math.max(580,box.clientWidth||880),height=210,svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 '+width+' '+height);var all=products.filter(function(p){return p.metric_value!==null&&p.metric_value!==undefined&&Number.isFinite(Number(p.metric_value))&&p.metric_name}),key=byId('metricSeries').value,vals=all.filter(function(p){return seriesKey(p)===key});if(!vals.length){var t=document.createElementNS('http://www.w3.org/2000/svg','text');t.setAttribute('x',width/2);t.setAttribute('y',105);t.setAttribute('text-anchor','middle');t.setAttribute('fill','#9aa9bd');t.textContent='No numeric values for this metric series';svg.append(t);box.append(svg);return}var low=Math.min.apply(null,vals.map(function(p){return Number(p.metric_value)})),high=Math.max.apply(null,vals.map(function(p){return Number(p.metric_value)})),span=(high-low)||1,pad=35,base=height-35,top=18;for(var i=0;i<5;i++){var y=top+i*(base-top)/4,l=document.createElementNS('http://www.w3.org/2000/svg','line');l.setAttribute('x1',pad);l.setAttribute('x2',width-15);l.setAttribute('y1',y);l.setAttribute('y2',y);l.setAttribute('stroke','#29384e');svg.append(l)}var step=(width-pad-20)/vals.length;vals.forEach(function(p,i){var value=Number(p.metric_value),x=pad+i*step+step*.16,bw=Math.max(2,step*.68),h=(value-low)/span*(base-top-10)+2,y=base-h,rect=document.createElementNS('http://www.w3.org/2000/svg','rect');rect.setAttribute('x',x);rect.setAttribute('y',y);rect.setAttribute('width',bw);rect.setAttribute('height',h);rect.setAttribute('rx',2);rect.setAttribute('fill',p.product_type==='GUNW'?'#7dd3fc':'#fbbf24');var title=document.createElementNS('http://www.w3.org/2000/svg','title');title.textContent=p.id+' · '+p.metric_name+': '+value;rect.append(title);svg.append(rect);if(vals.length<25){var tx=document.createElementNS('http://www.w3.org/2000/svg','text');tx.setAttribute('x',x+bw/2);tx.setAttribute('y',base+14);tx.setAttribute('text-anchor','middle');tx.setAttribute('font-size','8');tx.setAttribute('fill','#9aa9bd');tx.textContent=p.id;svg.append(tx)}});box.append(svg)}
function render(){var q=byId('search').value.toLowerCase(),family=byId('family').value,state=byId('state').value,list=P.filter(function(p){return(!q||(p.granule_name+' '+p.id).toLowerCase().indexOf(q)>=0)&&(!family||p.product_type===family)&&(!state||statusOf(p)===state)});byId('rows').replaceChildren();list.forEach(function(p){var tr=node('tr');var pairNote=p.boundary_pair_included_by_secondary_date_policy?' · REFERENCE PRE-DATES WINDOW':(p.reference_date_outside_window?' · REFERENCE OUTSIDE WINDOW':'');var maturity=(p.maturity||'—')+' / '+(p.science_maturity||'—')+(p.maturity_validation&&p.maturity_validation!=='MATCH'?' · '+p.maturity_validation:'');var cells=[p.id+' | '+(p.reference_date||'—')+' → '+(p.secondary_date||'—')+pairNote+' | '+p.granule_name,p.product_type,maturity,medianOf(p),fmt(p.valid_pixels),fmt(p.grid_pixels_inside_aoi)+' grid px · '+fmt(p.export_count)+' raster(s)'];cells.forEach(function(v,i){tr.append(node('td',i===0||i===2?'mono':'',v))});var qd=node('td');if(p.quicklook){var img=node('img','preview');img.src=p.quicklook;img.alt='Per-product quicklook';img.loading='lazy';qd.append(img)}tr.append(qd);tr.append(node('td',statusOf(p)==='WARNING'?'warn':(statusOf(p)==='FAIL'?'bad':''),statusOf(p)));tr.append(node('td','mono',p.source_sha256||'—'));byId('rows').append(tr)});byId('empty').textContent=list.length?list.length+' product(s) shown.':'No products match this filter.';updateMetricSeries(list);drawChart(list)}
['search','family','state'].forEach(function(id){byId(id).addEventListener('input',render)});byId('metricSeries').addEventListener('change',function(){var q=byId('search').value.toLowerCase(),family=byId('family').value,state=byId('state').value,list=P.filter(function(p){return(!q||(p.granule_name+' '+p.id).toLowerCase().indexOf(q)>=0)&&(!family||p.product_type===family)&&(!state||statusOf(p)===state)});drawChart(list)});window.addEventListener('resize',render);render();
})();
</script></body></html>"""
    doc = doc.replace("__POLYGON_SVG__", svg)
    doc = doc.replace("__GENERATED_AT__", html.escape(summary["generated_at_utc"]))
    doc = doc.replace("__DATASET__", dataset)
    (out / "index.html").write_text(doc, encoding="utf-8")

def aggregate(args: argparse.Namespace) -> dict[str, Any]:
    root = args.artifact_root.resolve()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    out = args.out_root.resolve()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    (out / "quicklooks").mkdir()
    (out / "product-data").mkdir()
    sys.path.insert(0, str(args.science_root.resolve() / "src"))
    from aoi_wkt import parse_polygon_wkt

    products = manifest.get("products", [])
    errors: list[dict[str, Any]] = []
    result_rows: list[dict[str, Any]] = []
    for product in products:
        pid = str(product.get("id", "")); family = str(product.get("product_type", "")); name = str(product.get("granule_name", ""))
        record: dict[str, Any] = {
            "id": pid, "product_type": family, "granule_name": name,
            "processing_type": product.get("processing_type"), "crid": product.get("crid"),
            "maturity": product.get("maturity"), "maturity_source": product.get("maturity_source"),
            "reference_date": product.get("reference_date"), "secondary_date": product.get("secondary_date"),
            "date_filter_mode": product.get("date_filter_mode"),
            "reference_date_in_window": product.get("reference_date_in_window"),
            "secondary_date_in_window": product.get("secondary_date_in_window"),
            "reference_date_outside_window": product.get("reference_date_outside_window"),
            "boundary_pair_included_by_secondary_date_policy": product.get("boundary_pair_included_by_secondary_date_policy"),
            "status": "FAIL", "warnings": [],
            "science_maturity": None, "maturity_validation": None,
            "science_pixel_support": False, "valid_pixels": None, "metric_name": None, "metric_value": None,
            "quicklook": None, "source_sha256": None, "export_count": 0, "grid_pixels_inside_aoi": None,
            "stats": {}, "exports": [],
        }
        artifact = root / ("sarguardian-granule-" + pid)
        if not artifact.is_dir():
            record["errors"] = ["PRODUCT_ARTIFACT_MISSING"]
            errors.append({"product_id": pid, "problem": "PRODUCT_ARTIFACT_MISSING"})
            result_rows.append(record); continue
        try:
            meta = json.loads((artifact / "product-processing.json").read_text(encoding="utf-8"))
            validation = json.loads((artifact / "derived-output-validation.json").read_text(encoding="utf-8"))
            preflight = json.loads((artifact / "preflight-report.json").read_text(encoding="utf-8"))
            download = json.loads((artifact / "download-manifest.json").read_text(encoding="utf-8"))
            for key, expected in (("id",pid),("granule_name",name),("product_type",family),("maturity",product.get("maturity")),
                                  ("aoi",manifest.get("aoi_name")),("science_commit",manifest.get("science_commit")),
                                  ("orchestration_branch",manifest.get("orchestration_branch")),
                                  ("orchestration_sha",manifest.get("orchestration_sha")),
                                  ("retention_profile",manifest.get("retention_profile")),
                                  ("derived_output_validation_status","PASS"),("drive_remote_verification","PASS")):
                if meta.get(key) != expected:
                    raise ValueError("PRODUCT_METADATA_MISMATCH field=" + key)
            if validation.get("status") != "PASS":
                raise ValueError("STRUCTURAL_VALIDATION_NOT_PASS")
            if preflight.get("status") != "PASS" or preflight.get("aoi_name") != manifest.get("aoi_name"):
                raise ValueError("EXACT_AOI_PREFLIGHT_NOT_PASS")
            if not isinstance(download.get("source_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}",download["source_sha256"]):
                raise ValueError("SOURCE_SHA256_MISSING_OR_INVALID")
            stats_path = safe_child(artifact, str(meta["stats_csv"]))
            _fields, stats_rows = read_csv(stats_path)
            # Catalog/manifest identifiers omit .h5; the scientific reader CSV includes it.
            matching = matching_product_stats_rows(stats_rows, name, family)
            if len(matching) != 1:
                raise ValueError("EXPECTED_ONE_PRODUCT_STATS_ROW got=" + str(len(matching)))
            stats = matching[0]
            science_maturity = str(stats.get("maturity") or "").strip().upper()
            catalog_maturity = str(product.get("maturity") or "").strip().upper()
            maturity_validation, maturity_warnings, maturity_conflict = assess_maturity_consistency(
                catalog_maturity, science_maturity
            )
            if maturity_conflict:
                raise ValueError("SCIENCE_AND_CATALOG_MATURITY_CONFLICT catalog=" + catalog_maturity + " reader=" + science_maturity)
            metric_key, metric_val = numeric(stats, ("median","median_mm","range_median_mm","median_range_mm"))
            exports = []; valid_pixels = None
            validation_exports = validation.get("exports", [])
            raw_valid_counts = [int(x.get("valid_band1_pixels", 0)) for x in validation_exports
                                if isinstance(x, dict) and str(x.get("valid_band1_pixels", "")).lstrip("-").isdigit()]
            valid_pixels = max(raw_valid_counts) if raw_valid_counts else 0
            record["science_pixel_support"] = any(n > 0 for n in raw_valid_counts)
            for item in validation_exports:
                if not isinstance(item, dict): continue
                source = safe_child(artifact, str(meta.get("export_dir", "derived")) + "/" + str(item.get("name","")))
                if not source.is_file(): raise ValueError("EXPORT_MISSING name=" + str(item.get("name")))
                dest = out / "product-data" / pid / source.name
                dest.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(source, dest)
                preview_target = out / "quicklooks" / (pid + "-" + source.stem + ".png")
                preview = make_quicklook(dest, preview_target)
                preview_rel = preview_target.relative_to(out).as_posix() if preview and not preview.get("error") else None
                exports.append({"name": source.name,"relative_path":dest.relative_to(out).as_posix(),
                                "bytes":dest.stat().st_size,"quicklook":preview_rel,"quicklook_meta":preview})
            if not exports: raise ValueError("NO_STRUCTURALLY_VALIDATED_EXPORTS")
            grid_counts = []
            for item in preflight.get("grid_checks", []):
                try:
                    if item.get("grid_pixels_inside_aoi") is not None: grid_counts.append(int(item["grid_pixels_inside_aoi"]))
                except (TypeError, ValueError): pass
            record.update({
                "status":"PASS" if record["science_pixel_support"] and not maturity_warnings and not (validation.get("warnings") or []) else "WARNING",
                "warnings":record["warnings"] + list(validation.get("warnings") or []) + maturity_warnings,
                "science_maturity":science_maturity or None,"maturity_validation":maturity_validation,
                "source_sha256":download["source_sha256"],"source_bytes":download.get("source_bytes"),
                "metric_name":metric_key,"metric_value":metric_val,"valid_pixels":valid_pixels,
                "grid_pixels_inside_aoi":min(grid_counts) if grid_counts else None,
                "export_count":len(exports),"exports":exports,"quicklook":next((x["quicklook"] for x in exports if x["quicklook"]),None),
                "stats":json_safe(stats),"preflight_report":json_safe(preflight),
                "validation_scope":validation.get("validation_scope"),
            })
            (out / "product-data" / pid / "product-record.json").write_text(
                json.dumps(json_safe(record),indent=2,ensure_ascii=False,allow_nan=False)+"\n",encoding="utf-8")
        except Exception as exc:
            record["status"]="FAIL"; record["errors"]=[type(exc).__name__+":"+str(exc)[:250]]
            errors.append({"product_id":pid,"problem":"PRODUCT_AGGREGATION_FAILED","detail":record["errors"][0]})
        result_rows.append(record)

    # Run the unchanged per-pixel stack against the exact requested-WKT
    # exports. Candidate screening is invoked in policy mode too, so arbitrary
    # WKT runs record an explicit unsupported-AOI skip rather than misusing the
    # source/langtang/lhende polygon assumptions in candidate_check.py.
    try:
        try:
            from run_export_science_diagnostics import run_diagnostics
        except ImportError:
            from tools.run_export_science_diagnostics import run_diagnostics
        science_diagnostics = run_diagnostics(
            args.science_root,
            out / "product-data",
            out / "science-diagnostics",
            expected_science_commit=str(manifest.get("science_commit") or ""),
            aoi="custom",
            run_candidate_check=True,
        )
        if science_diagnostics.get("status") == "FAIL":
            errors.append({
                "problem": "AOI_EXPORT_SCIENCE_DIAGNOSTICS_FAILED",
                "detail": str(science_diagnostics.get("errors", []))[:800],
            })
    except Exception as exc:
        science_diagnostics = {
            "status": "FAIL",
            "errors": [{"problem": "AOI_EXPORT_SCIENCE_DIAGNOSTICS_EXCEPTION",
                        "type": type(exc).__name__, "detail": str(exc)[:400]}],
        }
        errors.append({
            "problem": "AOI_EXPORT_SCIENCE_DIAGNOSTICS_EXCEPTION",
            "detail": type(exc).__name__ + ":" + str(exc)[:400],
        })

    ring = parse_polygon_wkt(str(manifest["aoi_wkt"]))
    expected = len(products); validated = sum(x["status"] in {"PASS","WARNING"} for x in result_rows); missing = expected-validated
    summary = {
        "schema_version":"sarguardian-aoi-dashboard-summary/1.0",
        "generated_at_utc":datetime.now(timezone.utc).isoformat(),
        "status":"PASS" if validated==expected and not errors else ("PARTIAL" if validated else "FAIL"),
        "run_id":manifest.get("run_id"),"science_commit":manifest.get("science_commit"),
        "workflow_ref":manifest.get("workflow_ref"),"workflow_sha":manifest.get("workflow_sha"),
        "orchestration_branch":manifest.get("orchestration_branch"),"orchestration_sha":manifest.get("orchestration_sha"),
        "dispatcher_workflow_ref":manifest.get("dispatcher_workflow_ref"),
        "dispatcher_workflow_sha":manifest.get("dispatcher_workflow_sha"),
        "aoi_name":manifest.get("aoi_name"),"aoi_crs":manifest.get("aoi_crs"),
        "aoi_wkt":manifest.get("aoi_wkt"),"aoi_wkt_sha256":manifest.get("aoi_wkt_sha256"),
        "date_start":manifest.get("time_window",{}).get("start_date"),"date_end":manifest.get("time_window",{}).get("end_date"),
        "date_basis":manifest.get("time_window",{}).get("date_basis"),
        "date_filter_mode":manifest.get("time_window",{}).get("date_filter_mode"),
        "catalog_products_matching_date_policy_before_maturity":manifest.get("time_window",{}).get("catalog_products_matching_date_policy_before_maturity"),
        "supported_product_variants_after_maturity_policy":manifest.get("time_window",{}).get("supported_product_variants_after_maturity_policy"),
        "unique_acquisition_pair_products_in_window":manifest.get("time_window",{}).get("unique_acquisition_pair_products_in_window"),
        "products_with_reference_outside_window":manifest.get("time_window",{}).get("reference_outside_window_unique_pairs"),
        "boundary_pair_products_included_by_secondary_policy":manifest.get("time_window",{}).get("boundary_pairs_included_by_secondary_policy"),
        "duplicate_variants_excluded":len(manifest.get("processing_variant_selection",{}).get("excluded_duplicate_variants",[])),
        "maturity_policy":manifest.get("catalog_query",{}).get("maturity_policy"),
        "processing_variant_policy":manifest.get("processing_variant_selection",{}).get("policy"),
        "run_profile":manifest.get("run_profile"),
        "retention_profile":manifest.get("retention_profile"),"catalog_search_start":manifest.get("time_window",{}).get("catalog_search_start"),
        "catalog_result_truncated":manifest.get("catalog_query",{}).get("results_truncated"),
        "products_expected":expected,"products_validated":validated,"products_missing":missing,
        "products_with_science_pixels":sum(bool(x.get("science_pixel_support")) for x in result_rows),
        "science_diagnostics":science_diagnostics,
        "gunw_count":sum(x.get("product_type")=="GUNW" for x in result_rows),
        "goff_count":sum(x.get("product_type")=="GOFF" for x in result_rows),
        "retention_profile_note":manifest.get("archive_note"),"baseline_policy":manifest.get("baseline_policy"),
        "science_claim_limit":manifest.get("scientific_claim_limit"),"errors":errors,
    }
    (out/"aoi-run-summary.json").write_text(json.dumps(json_safe(summary),indent=2,ensure_ascii=False,allow_nan=False)+"\n",encoding="utf-8")
    fields=["id","product_type","processing_type","crid","reference_date","secondary_date","date_filter_mode","reference_date_in_window","secondary_date_in_window","reference_date_outside_window","boundary_pair_included_by_secondary_date_policy","maturity","maturity_source","science_maturity","maturity_validation","granule_name","status","valid_pixels","metric_name","metric_value","source_sha256","source_bytes","export_count","grid_pixels_inside_aoi"]
    with (out/"products.csv").open("w",newline="",encoding="utf-8") as fh:
        writer=csv.DictWriter(fh,fieldnames=fields,extrasaction="ignore");writer.writeheader()
        writer.writerows([{k:x.get(k) for k in fields} for x in result_rows])
    write_html(out,summary,result_rows,polygon_svg(ring))
    report={"summary":summary,"products":result_rows,"processing_report":{
        "schema_version":"sarguardian-aoi-processing-report/1.0","aoi_wkt_sha256":manifest.get("aoi_wkt_sha256"),
        "science_commit":manifest.get("science_commit"),"manifest_sha256":sha256_file(args.manifest),
        "status":summary["status"],"errors":errors,
        "warning_note":"PASS indicates requested-AOI processing and structural checks passed; it is not independent geophysical validation."}}
    (out/"aoi-processing-report.json").write_text(json.dumps(json_safe(report),indent=2,ensure_ascii=False,allow_nan=False)+"\n",encoding="utf-8")
    zip_path=out/"aoi-derived-results.zip"
    with zipfile.ZipFile(zip_path,"w",compression=zipfile.ZIP_DEFLATED,compresslevel=6) as zf:
        for path in sorted(out.rglob("*")):
            if path.is_file() and path!=zip_path: zf.write(path,path.relative_to(out).as_posix())
        for x in result_rows:
            folder=root/("sarguardian-granule-"+str(x.get("id","")))
            for rel in ("product-processing.json","preflight-report.json","derived-output-validation.json","download-manifest.json"):
                src=folder/rel
                if src.is_file(): zf.write(src,"provenance/"+str(x.get("id",""))+"/"+rel)
    return summary

def main() -> int:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--manifest",required=True,type=Path);p.add_argument("--artifact-root",required=True,type=Path)
    p.add_argument("--science-root",required=True,type=Path);p.add_argument("--out-root",required=True,type=Path)
    args=p.parse_args()
    try: summary=aggregate(args)
    except Exception as exc:
        print("DASHBOARD_AGGREGATION_ERROR="+type(exc).__name__+":"+str(exc)[:350],file=sys.stderr);return 2
    print("AOI_DASHBOARD_STATUS="+str(summary["status"]))
    print("AOI_PRODUCTS_VALIDATED={}/{}".format(summary["products_validated"],summary["products_expected"]))
    print("AOI_PRODUCTS_WITH_SCIENCE_PIXELS="+str(summary["products_with_science_pixels"]))
    print("AOI_DASHBOARD_HTML="+str(args.out_root.resolve()/"index.html"))
    print("AOI_RESULTS_BUNDLE="+str(args.out_root.resolve()/"aoi-derived-results.zip"))
    return 0 if summary["status"]=="PASS" else 1

if __name__=="__main__":
    raise SystemExit(main())
